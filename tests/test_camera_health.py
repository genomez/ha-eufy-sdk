"""Validate and replay observe-only camera diagnostics without external I/O."""

# ruff: noqa: ANN001, ANN002, ANN003, ANN201, ANN202, D102, FBT003
# ruff: noqa: INP001, PT009, PT027, SLF001
import json
import unittest
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import test_aligned_state as aligned
import test_bounded_state_writes as bounded
import test_setup_event_guards as setup
from aiohttp import WSMsgType
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import EntityCategory

import custom_components.eufy_sdk as integration
from custom_components.eufy_sdk import binary_sensor, sensor
from custom_components.eufy_sdk.api import (
    CAMERA_CONNECTIVITY,
    CAMERA_HEALTH_BASES,
    CAMERA_HEALTH_TIMES,
    CAMERA_OUTCOMES,
    EufySdkApiClientCommunicationError,
    camera_health,
    camera_health_event,
)
from custom_components.eufy_sdk.binary_sensor import EufyCameraConnectivityBinarySensor
from custom_components.eufy_sdk.camera_health import camera_health_serials
from custom_components.eufy_sdk.sensor import EufyCameraConnectivityStatusSensor

NOW_MS = 1792680000000
FRAME = json.loads(
    (Path(__file__).parent / "fixtures" / "camera_health_v1.json").read_text(
        encoding="utf-8"
    )
)


def projection(**changes):
    """Use the synthetic frame exported by the parallel SDK/bridge implementation."""
    value = deepcopy(FRAME)
    value.pop("event")
    value.pop("sn")
    return {**value, **changes}


def event(**changes):
    """Address a synthetic station in the existing offline coordinator helper."""
    return {"event": "cameraHealth", "sn": "station", **projection(**changes)}


def replace_path(value, path, replacement):
    """Replace one nested field while preserving every other valid contract field."""
    result = deepcopy(value)
    target = result
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = replacement
    return result


def leaf_paths(value, prefix=()):
    """Enumerate every required field, including nested receipt counters."""
    for key, item in value.items():
        path = (*prefix, key)
        yield path
        if isinstance(item, dict):
            yield from leaf_paths(item, path)


class CameraHealthValidationTests(unittest.TestCase):
    """Reject whole malformed objects and preserve the exact v1 vocabulary."""

    def setUp(self):
        self.clock = patch(
            "custom_components.eufy_sdk.api.time.time", return_value=NOW_MS / 1000
        )
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def test_all_required_fields_and_container_types(self):
        value = projection()
        for path in leaf_paths(value):
            with self.subTest(missing=path):
                candidate = deepcopy(value)
                target = candidate
                for key in path[:-1]:
                    target = target[key]
                del target[path[-1]]
                self.assertIsNone(camera_health(candidate))
        for candidate in (None, [], "health", 1, True):
            with self.subTest(root=candidate):
                self.assertIsNone(camera_health(candidate))
        for path in (
            ("rssi",),
            ("rawStatus",),
            ("outcomes",),
            ("lastOutcome",),
            *(("outcomes", state) for state in CAMERA_CONNECTIVITY),
        ):
            for candidate in ([], "health", 1, True):
                with self.subTest(path=path, value=candidate):
                    self.assertIsNone(
                        camera_health(replace_path(value, path, candidate))
                    )
        for key in ("rssi", "rawStatus", "outcomes"):
            self.assertIsNone(camera_health({**value, key: None}))
        self.assertIsNotNone(camera_health({**value, "lastOutcome": None}))

    def test_version_incarnation_and_boolean_enforcement(self):
        value = projection()
        cases = {
            "version": (None, True, False, 0, 2, 1.0, "1"),
            "incarnation": (None, True, 2, "", "a" * 65, "a b", "a.b", "a\n", "é"),
            "commandsBlocked": (None, True, 0, 1, "false"),
            "enforcement": (None, True, "enabled", "", []),
        }
        for key, invalid in cases.items():
            for item in invalid:
                with self.subTest(field=key, value=item):
                    self.assertIsNone(camera_health({**value, key: item}))
        for incarnation in ("a", "a" * 64, "A_z-019"):
            self.assertIsNotNone(camera_health({**value, "incarnation": incarnation}))

    def test_all_enum_values_and_wrong_types(self):
        value = projection()
        enums = {
            ("connectivity",): CAMERA_CONNECTIVITY,
            ("basis",): CAMERA_HEALTH_BASES,
            ("evidence",): ("vendor_verified", "hub_reported", "unqualified", "none"),
            ("lastContactSource",): ("push", "media", None),
            ("parentTransport",): ("connected", "disconnected", "unknown"),
            ("lastOutcome", "connectivity"): CAMERA_CONNECTIVITY,
            ("lastOutcome", "status"): CAMERA_OUTCOMES,
        }
        for path, options in enums.items():
            for item in options:
                with self.subTest(path=path, valid=item):
                    self.assertIsNotNone(camera_health(replace_path(value, path, item)))
            for item in ("invalid", "ONLINE", "", True, 1, [], {}):
                with self.subTest(path=path, invalid=item):
                    self.assertIsNone(camera_health(replace_path(value, path, item)))
            if None not in options:
                self.assertIsNone(camera_health(replace_path(value, path, None)))

    def test_safe_integer_counters_and_revision(self):
        value = projection()
        paths = [("revision",), ("hubOfflineCount",)]
        paths.extend(
            ("outcomes", state, outcome)
            for state in CAMERA_CONNECTIVITY
            for outcome in CAMERA_OUTCOMES
        )
        for path in paths:
            for item in (0, 2**53 - 1):
                with self.subTest(path=path, valid=item):
                    self.assertIsNotNone(camera_health(replace_path(value, path, item)))
            for item in (
                None,
                True,
                False,
                -1,
                2**53,
                1.0,
                "1",
                float("nan"),
                float("inf"),
            ):
                with self.subTest(path=path, invalid=item):
                    self.assertIsNone(camera_health(replace_path(value, path, item)))

    def test_every_timestamp_including_future_boundary(self):
        value = projection()
        paths = [
            *((key,) for key in CAMERA_HEALTH_TIMES),
            ("rssi", "lastValidMs"),
            ("rawStatus", "observedMs"),
            ("lastOutcome", "atMs"),
        ]
        for path in paths:
            for item in (0, NOW_MS, NOW_MS + 600000):
                with self.subTest(path=path, valid=item):
                    self.assertIsNotNone(camera_health(replace_path(value, path, item)))
            for item in (
                True,
                False,
                -1,
                1.0,
                "1",
                NOW_MS + 600001,
                2**53,
                float("inf"),
                float("nan"),
            ):
                with self.subTest(path=path, invalid=item):
                    self.assertIsNone(camera_health(replace_path(value, path, item)))
            candidate = replace_path(value, path, None)
            if path == ("lastOutcome", "atMs"):
                self.assertIsNone(camera_health(candidate))
            else:
                self.assertIsNotNone(camera_health(candidate))

    def test_telemetry_and_outcome_code_types(self):
        value = projection()
        for path in (("rssi", "value"), ("lastOutcome", "code")):
            for item in (None, -109, 0, 2**53):
                self.assertIsNotNone(camera_health(replace_path(value, path, item)))
            for item in (True, False, 1.5, "-109", [], float("inf"), float("nan")):
                with self.subTest(path=path, invalid=item):
                    self.assertIsNone(camera_health(replace_path(value, path, item)))
        for item in (True, False):
            self.assertIsNotNone(
                camera_health(replace_path(value, ("rssi", "valid"), item))
            )
        for item in (None, 0, 1, "false", []):
            self.assertIsNone(
                camera_health(replace_path(value, ("rssi", "valid"), item))
            )
        for path in (("rawStatus", "p1131"), ("rawStatus", "p1182")):
            for item in (None, True, False, -1, 0, 1.25):
                self.assertIsNotNone(camera_health(replace_path(value, path, item)))
            for item in ("true", [], {}, float("inf"), float("nan"), 10**1000):
                with self.subTest(path=path, invalid=item):
                    self.assertIsNone(camera_health(replace_path(value, path, item)))

    def test_projection_strips_extras_and_detaches_all_nested_objects(self):
        value = projection()
        original = deepcopy(value)
        value["private"] = "drop"
        value["rssi"]["private"] = "drop"
        value["rawStatus"]["private"] = "drop"
        value["outcomes"]["private"] = "drop"
        value["outcomes"]["online"]["private"] = "drop"
        value["lastOutcome"]["private"] = "drop"
        result = camera_health(value)
        self.assertEqual(result, original)
        result["rssi"]["value"] = -50
        result["rawStatus"]["p1131"] = True
        result["outcomes"]["online"]["other"] = 3
        result["lastOutcome"]["code"] = 0
        self.assertIsNone(value["rssi"]["value"])
        self.assertIsNone(value["rawStatus"]["p1131"])
        self.assertEqual(value["outcomes"]["online"]["other"], 0)
        self.assertEqual(value["lastOutcome"]["code"], -109)

    def test_camera_scoped_event_validation(self):
        value = event()
        self.assertEqual(camera_health_event({**value, "private": "drop"}), value)
        for changes in (
            {"event": "other"},
            {"sn": None},
            {"sn": 1},
            {"sn": ""},
            {"sn": "a" * 129},
        ):
            with self.subTest(changes=changes):
                self.assertIsNone(camera_health_event({**value, **changes}))
        self.assertIsNotNone(camera_health_event({**value, "sn": "a" * 128}))


class CameraHealthProjectionTests(unittest.IsolatedAsyncioTestCase):
    """Preserve event/snapshot ordering and retire evidence across reconnection."""

    client = bounded.BoundedProtocolTests.client

    def coordinator(self, client=None):
        coordinator = aligned.coordinator_for(client or Mock(connected=True))
        coordinator._clear_fast_retry = Mock()
        coordinator.last_update_success = True
        return coordinator

    async def test_event_snapshot_order_and_new_incarnation(self):
        coordinator = self.coordinator()
        coordinator._read_snapshot = AsyncMock(
            return_value={"station": {"cameraHealth": projection()}}
        )
        coordinator.data = await coordinator._async_update_data()
        for revision in (6, 7):
            coordinator.apply_camera_health_event(
                event(revision=revision, connectivity="online")
            )
            self.assertEqual(coordinator.data["station"]["cameraHealth"], projection())
        coordinator.apply_camera_health_event(event(revision=8, connectivity="online"))
        current = projection(revision=8, connectivity="online")
        for raw in (projection(), {"revision": 9}, None):
            coordinator._read_snapshot = AsyncMock(
                return_value={"station": {"cameraHealth": raw}}
            )
            result = await coordinator._async_update_data()
            self.assertEqual(result["station"]["cameraHealth"], current)
        coordinator.apply_camera_health_event(
            event(incarnation="replacement", revision=0)
        )
        self.assertEqual(coordinator.data["station"]["cameraHealth"]["revision"], 0)
        self.assertEqual(
            coordinator.data["station"]["cameraHealth"]["incarnation"], "replacement"
        )

    async def test_event_during_snapshot_wins_without_state_or_command_health_changes(
        self,
    ):
        coordinator = self.coordinator()
        state = deepcopy(coordinator.data["station"]["state"])
        command = {"status": "healthy", "cooldownRemainingMs": 0}
        coordinator.data["station"]["commandHealth"] = command

        async def snapshot():
            coordinator.apply_camera_health_event(event(revision=8))
            return {"station": {"cameraHealth": projection(), "commandHealth": command}}

        coordinator._read_snapshot = snapshot
        result = await coordinator._async_update_data()
        self.assertEqual(result["station"]["cameraHealth"]["revision"], 8)
        self.assertEqual(coordinator.data["station"]["state"], state)
        self.assertEqual(coordinator.data["station"]["commandHealth"], command)
        self.assertEqual(result["station"]["commandHealth"], command)

    async def test_reset_rejects_inflight_snapshot_and_accepts_new_event(self):
        coordinator = self.coordinator()
        coordinator.apply_camera_health_event(event(revision=20))

        async def snapshot():
            coordinator.reset_camera_health()
            self.assertNotIn("cameraHealth", coordinator.data["station"])
            coordinator.apply_camera_health_event(event(incarnation="new", revision=0))
            return {"station": {"cameraHealth": projection(revision=20)}}

        coordinator._read_snapshot = snapshot
        result = await coordinator._async_update_data()
        self.assertEqual(
            result["station"]["cameraHealth"], projection(incarnation="new", revision=0)
        )
        self.assertIsNone(coordinator._pending_camera_health)

    async def test_reset_during_snapshot_without_new_event_drops_old_projection(self):
        coordinator = self.coordinator()

        async def snapshot():
            coordinator.reset_camera_health()
            return {"station": {"cameraHealth": projection()}}

        coordinator._read_snapshot = snapshot
        self.assertNotIn(
            "cameraHealth", (await coordinator._async_update_data())["station"]
        )

    async def test_poll_reconnect_retires_old_evidence_before_fresh_snapshot(self):
        client = Mock(connected=False)
        coordinator = self.coordinator(client)
        coordinator.apply_camera_health_event(event(revision=20))
        coordinator._read_snapshot = AsyncMock(
            return_value={"station": {"cameraHealth": projection(revision=0)}}
        )
        result = await coordinator._async_update_data()
        self.assertEqual(result["station"]["cameraHealth"]["revision"], 0)

    def test_unrelated_events_unknown_cameras_and_duplicates_do_not_notify(self):
        coordinator = self.coordinator()
        coordinator.apply_camera_health_event(event())
        original = deepcopy(coordinator.data)
        coordinator.async_update_listeners.reset_mock()
        for value in (
            {**event(), "sn": "other-camera"},
            {**event(), "version": 2},
            event(),
            {"event": "ready"},
            {"event": "bridgeDisconnected"},
            {"event": "propertyChanged"},
        ):
            coordinator.apply_camera_health_event(value)
        self.assertEqual(coordinator.data, original)
        coordinator.async_update_listeners.assert_not_called()

    def test_both_projection_maps_are_bounded_and_allow_existing_updates(self):
        client = self.client()
        coordinator = self.coordinator()
        with (
            patch("custom_components.eufy_sdk.api.MAX_CAMERA_HEALTH_TARGETS", 2),
            patch(
                "custom_components.eufy_sdk.coordinator.MAX_CAMERA_HEALTH_TARGETS", 2
            ),
        ):
            for owner in (client, coordinator):
                for serial in ("camera-a", "camera-b"):
                    self.assertIsNotNone(
                        owner._project_camera_health(serial, projection())
                    )
                self.assertIsNone(
                    owner._project_camera_health("camera-c", projection())
                )
                self.assertEqual(
                    owner._project_camera_health("camera-a", projection(revision=8))[
                        "revision"
                    ],
                    8,
                )
                self.assertEqual(len(owner._camera_health_projections), 2)

    async def test_api_orders_events_snapshots_and_clears_on_runtime_reset(self):
        client = self.client()
        seen = []
        client._on_event = seen.append
        for value in (
            event(),
            event(revision=6),
            event(),
            event(version=2),
            event(revision=8),
        ):
            client._dispatch_event(value)
        self.assertEqual([value["revision"] for value in seen], [7, 8])
        client.rpc = AsyncMock(
            return_value={"devices": [{"sn": "station", "cameraHealth": projection()}]}
        )
        self.assertEqual(
            (await client.list_devices())[0]["cameraHealth"]["revision"], 8
        )
        client._dispatch_event(event(incarnation="new", revision=0))
        self.assertEqual(seen[-1]["incarnation"], "new")
        client._dispatch_event({"event": "ready"})
        self.assertEqual(client._camera_health_projections, {})
        client._dispatch_event(event(revision=0))
        self.assertEqual(seen[-1]["revision"], 0)

    async def test_api_rejects_reply_from_retired_transport(self):
        client = self.client()

        async def reply(*_args):
            client._reset_command_health()
            return {"devices": [{"sn": "station", "cameraHealth": projection()}]}

        client.rpc = reply
        with self.assertRaises(EufySdkApiClientCommunicationError):
            await client.list_devices()
        self.assertEqual(client._camera_health_projections, {})

    async def test_replay_exported_bridge_frame_through_receive_loop_and_entities(self):
        client = self.client()
        coordinator = self.coordinator(client)
        coordinator.data = {FRAME["sn"]: {"state": {"light": True}, "available": False}}
        seen = []

        def on_event(value):
            seen.append(value)
            coordinator.apply_camera_health_event(value)

        client._on_event = on_event

        async def frames():
            for value in (
                FRAME,
                FRAME,
                {**FRAME, "revision": 6},
                {**FRAME, "commandsBlocked": True},
            ):
                yield SimpleNamespace(
                    type=WSMsgType.TEXT, json=lambda value=value: value
                )

        client._ws.__aiter__ = lambda _: frames()
        client._closing = True
        await client._receive_loop()
        self.assertEqual(seen, [FRAME])
        self.assertEqual(client._camera_health_projections, {})
        binary = EufyCameraConnectivityBinarySensor(coordinator, FRAME["sn"])
        status = EufyCameraConnectivityStatusSensor(coordinator, FRAME["sn"])
        self.assertIsNone(binary.is_on)
        self.assertEqual(status.native_value, "suspect")
        self.assertEqual(
            status.extra_state_attributes["last_outcome"]["connectivity"], "online"
        )
        self.assertEqual(
            status.extra_state_attributes["outcomes"]["online"]["rejected"], 1
        )
        self.assertEqual(status.extra_state_attributes["commands_blocked"], False)
        self.assertFalse(status.available)


class CameraHealthEntityTests(unittest.IsolatedAsyncioTestCase):
    """Show stable diagnostic facts with infrastructure-only availability."""

    def coordinator(self):
        coordinator = aligned.coordinator_for(Mock(connected=True))
        coordinator.last_update_success = True
        coordinator.solix_devices = {}
        return coordinator

    def test_all_four_states_identity_and_bridge_availability(self):
        coordinator = self.coordinator()
        binary = EufyCameraConnectivityBinarySensor(coordinator, "station")
        status = EufyCameraConnectivityStatusSensor(coordinator, "station")
        self.assertEqual(binary.unique_id, "station_connectivity")
        self.assertEqual(status.unique_id, "station_connectivity_status")
        self.assertEqual(binary.device_class, BinarySensorDeviceClass.CONNECTIVITY)
        self.assertEqual(status.device_class, SensorDeviceClass.ENUM)
        self.assertEqual(status.options, list(CAMERA_CONNECTIVITY))
        for entity in (binary, status):
            self.assertEqual(entity.entity_category, EntityCategory.DIAGNOSTIC)
        for state, expected in (
            ("online", True),
            ("offline", False),
            ("suspect", None),
            ("unknown", None),
        ):
            with self.subTest(state=state):
                coordinator.data["station"].update(
                    cameraHealth=projection(connectivity=state), available=False
                )
                self.assertIs(binary.is_on, expected)
                self.assertEqual(status.native_value, state)
                self.assertTrue(binary.available)
                self.assertTrue(status.available)
                self.assertEqual(
                    status.extra_state_attributes,
                    {
                        "basis": "hub_reported_offline",
                        "evidence": "hub_reported",
                        "since": "2026-10-22T14:40:00+00:00",
                        "last_contact": None,
                        "last_contact_source": None,
                        "last_hub_offline_reply": "2026-10-22T14:40:00+00:00",
                        "hub_offline_reply_count": 1,
                        "last_hub_report": None,
                        "last_cloud_param": None,
                        "rssi": None,
                        "rssi_valid": False,
                        "rssi_last_valid": None,
                        "raw_status_1131": None,
                        "raw_status_1182": None,
                        "parent_transport": "connected",
                        "enforcement": "observe_only",
                        "commands_blocked": False,
                        "outcomes": projection()["outcomes"],
                        "last_outcome": {
                            "atMs": "2026-10-22T14:40:00+00:00",
                            "connectivity": "online",
                            "status": "rejected",
                            "code": -109,
                        },
                        "revision": 7,
                    },
                )
        coordinator.last_update_success = False
        self.assertFalse(binary.available)
        coordinator.last_update_success = True
        coordinator.config_entry.runtime_data.client.connected = False
        self.assertFalse(status.available)
        coordinator.config_entry.runtime_data.client.connected = True
        coordinator.data.clear()
        self.assertFalse(binary.available)
        self.assertIsNone(binary.is_on)
        self.assertIsNone(status.native_value)

    def test_exact_attributes_utc_receipt_times_and_no_churn(self):
        coordinator = self.coordinator()
        health = projection()
        for key in CAMERA_HEALTH_TIMES:
            health[key] = NOW_MS
        health["rssi"] = {"value": -58, "valid": True, "lastValidMs": NOW_MS}
        health["rawStatus"] = {"p1131": True, "p1182": 2, "observedMs": NOW_MS}
        coordinator.data["station"]["cameraHealth"] = health
        status = EufyCameraConnectivityStatusSensor(coordinator, "station")
        attributes = status.extra_state_attributes
        self.assertNotIn("last_hub_offline", attributes)
        self.assertNotIn("hub_offline_count", attributes)
        self.assertEqual(
            set(attributes),
            {
                "basis",
                "evidence",
                "since",
                "last_contact",
                "last_contact_source",
                "last_hub_offline_reply",
                "hub_offline_reply_count",
                "last_hub_report",
                "last_cloud_param",
                "rssi",
                "rssi_valid",
                "rssi_last_valid",
                "raw_status_1131",
                "raw_status_1182",
                "parent_transport",
                "enforcement",
                "commands_blocked",
                "outcomes",
                "last_outcome",
                "revision",
            },
        )
        for key in (
            "since",
            "last_contact",
            "last_hub_offline_reply",
            "last_hub_report",
            "last_cloud_param",
            "rssi_last_valid",
        ):
            self.assertEqual(attributes[key], "2026-10-22T14:40:00+00:00")
            self.assertEqual(
                datetime.fromisoformat(attributes[key]).utcoffset().total_seconds(), 0
            )
        self.assertEqual(attributes["last_outcome"]["atMs"], attributes["since"])
        self.assertEqual(attributes["rssi"], -58)
        self.assertIs(attributes["rssi_valid"], True)
        self.assertIs(attributes["raw_status_1131"], True)
        self.assertEqual(attributes["raw_status_1182"], 2)
        with patch(
            "custom_components.eufy_sdk.api.time.time", return_value=NOW_MS / 1000 + 999
        ):
            coordinator.data["station"]["cameraHealth"] = deepcopy(health)
            self.assertIs(status.extra_state_attributes, attributes)
        attributes["outcomes"]["online"]["rejected"] = 100
        self.assertEqual(health["outcomes"]["online"]["rejected"], 1)
        coordinator.data["station"].pop("cameraHealth")
        self.assertEqual(status.extra_state_attributes, {})

    async def test_platforms_create_entities_only_for_valid_health_snapshots(self):
        coordinator = self.coordinator()
        coordinator.data = {
            "valid-camera": {"cameraHealth": projection()},
            "invalid-camera": {"cameraHealth": projection(version=2)},
            "legacy-camera": {},
        }
        self.assertEqual(camera_health_serials(coordinator), ["valid-camera"])
        entry = SimpleNamespace(
            data={"host": "synthetic-bridge"},
            async_on_unload=Mock(),
            runtime_data=SimpleNamespace(coordinator=coordinator, properties={}),
        )
        for platform, kind in (
            (binary_sensor, EufyCameraConnectivityBinarySensor),
            (sensor, EufyCameraConnectivityStatusSensor),
        ):
            added = []
            await platform.async_setup_entry(Mock(), entry, added.extend)
            diagnostics = [entity for entity in added if isinstance(entity, kind)]
            self.assertEqual(len(diagnostics), 1)
            self.assertEqual(diagnostics[0]._sn, "valid-camera")


class CameraHealthSetupTests(unittest.IsolatedAsyncioTestCase):
    """Exercise the real setup callback while preserving existing fixture contracts."""

    asyncSetUp = setup.SetupEventGuardTests.asyncSetUp

    def test_dispatch_only_touches_camera_health_for_its_own_event(self):
        self.coordinator.apply_camera_health_event.assert_not_called()
        self.on_event(event())
        self.coordinator.apply_camera_health_event.assert_called_once_with(event())
        self.on_event({"event": "commandHealth"})
        self.on_event({"event": "propertyChanged"})
        self.coordinator.apply_camera_health_event.assert_called_once()

    async def test_ready_then_healthless_snapshot_cannot_restore_retired_evidence(self):
        client = bounded.BoundedProtocolTests.client(self)
        coordinator = aligned.coordinator_for(client)
        coordinator._clear_fast_retry = Mock()
        coordinator.apply_camera_health_event(event(revision=20))
        client._dispatch_event(event(revision=20))
        self.coordinator.reset_camera_health.side_effect = (
            coordinator.reset_camera_health
        )
        self.hass.async_create_task.side_effect = lambda coroutine: coroutine.close()
        client._on_event = self.on_event
        client._dispatch_event({"event": "ready"})
        self.coordinator.apply_camera_health_event.assert_not_called()
        self.assertEqual(client._camera_health_projections, {})
        self.assertEqual(coordinator._camera_health_projections, {})
        coordinator._read_snapshot = AsyncMock(return_value={"station": {}})
        result = await coordinator._async_update_data()
        self.assertNotIn("cameraHealth", result["station"])
        coordinator.apply_camera_health_event(event(revision=0))
        self.assertEqual(coordinator.data["station"]["cameraHealth"]["revision"], 0)

    async def test_actual_reconnect_callback_resets_camera_projection_before_refresh(
        self,
    ):
        client = self.entry.runtime_data.client
        self.coordinator.async_config_entry_first_refresh.side_effect = None
        with (
            patch.object(integration, "async_reconcile_property_entities"),
            patch.object(
                integration,
                "EufySdkDataUpdateCoordinator",
                return_value=self.coordinator,
            ),
            patch.object(
                integration, "EufySdkApiClient", return_value=client
            ) as factory,
            patch.object(integration, "async_get_clientsession", return_value=Mock()),
            patch.object(
                integration, "async_get_loaded_integration", return_value=Mock()
            ),
            patch.object(integration, "async_call_later"),
        ):
            await integration.async_setup_entry(self.hass, self.entry)
        ordered = Mock()
        ordered.attach_mock(self.coordinator.reset_camera_health, "reset")
        ordered.attach_mock(self.hass.async_create_task, "refresh")
        self.hass.async_create_task.side_effect = lambda coroutine: coroutine.close()
        factory.call_args.kwargs["on_reconnect"]()
        self.assertEqual([call[0] for call in ordered.mock_calls], ["reset", "refresh"])
        self.coordinator.reset_command_health.assert_not_called()
