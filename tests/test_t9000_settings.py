"""HomeBase S1 metadata controls, structured schedules and explicit audio actions."""

# ruff: noqa: ANN002, ANN201, D101, D102, D103, INP001, PT009, PT027
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import voluptuous as vol
from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from test_property_entities import Entry, Registry

from custom_components.eufy_sdk.api import EufySdkApiClient, EufySdkApiClientError
from custom_components.eufy_sdk.button import (
    EufyQuickResponseRefreshButton,
    QuickResponseButtons,
    async_setup_entry,
)
from custom_components.eufy_sdk.entity import classify
from custom_components.eufy_sdk.light_schedule import (
    register_schedule_service,
    t9000_attached,
    validate_entries,
)
from custom_components.eufy_sdk.manifest_reads import normalize_properties
from custom_components.eufy_sdk.number import EufySdkNumber
from custom_components.eufy_sdk.property_entities import (
    _presentation_decisions,
    async_reconcile_property_entities,
)
from custom_components.eufy_sdk.select import EufySdkSelect
from custom_components.eufy_sdk.sensor import EufyLightScheduleSensor


def coordinator():
    obj = Mock(
        data={
            "camera": {
                "stationSn": "station",
                "capabilities": ["doorbell"],
                "state": {},
            },
            "station": {"model": "T9000"},
        }
    )
    obj.config_entry.entry_id = "fixture"
    client = obj.config_entry.runtime_data.client
    client.quick_responses = AsyncMock(
        return_value=[{"voiceId": 2, "name": "Leave it here"}]
    )
    client.play_quick_response = AsyncMock()
    return obj


def properties(*reads):
    return normalize_properties(
        {
            "properties": [],
            "decodedProperties": {
                "bound": True,
                "details": [{"accessor": "light", "reads": list(reads)}],
            },
        }
    )


class MetadataTests(unittest.TestCase):
    def test_bound_sdk_fixtures_cover_model_changes(self):
        fixture = json.loads(
            (
                Path(__file__).parent / "fixtures" / "t9000_settings_manifests.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            {case["model"] for case in fixture["cases"]},
            {"T8425", "T8416", "T8170", "T8214", "T8134", "T8171", "T8426"},
        )
        for case in fixture["cases"]:
            before = {spec["name"]: classify(spec) for spec in case["previous"]}
            after = {spec["name"]: classify(spec) for spec in case["next"]}
            changes = [
                {"name": name, "before": before.get(name), "after": after.get(name)}
                for name in sorted(before.keys() | after.keys())
                if before.get(name) != after.get(name)
            ]
            self.assertEqual(changes, case["changes"])
            quality = next(
                spec for spec in case["next"] if spec["name"] == "recordingQuality"
            )
            self.assertEqual(
                quality["enumValues"],
                {
                    "1": "720P",
                    "2": "1080P",
                    "3": "2K" if case["model"] == "T8416" else "3K",
                },
            )
            for name, platform in after.items():
                spec = next(spec for spec in case["next"] if spec["name"] == name)
                if spec.get("writable") is False:
                    self.assertNotIn(platform, ("switch", "number", "select"))

    def test_writable_flags_route_controls_and_readonly_fallbacks(self):
        for name, kind, value_type, platform in [
            ("enabled", "boolean", "bool", "switch"),
            ("statusLed", "boolean", "bool", "switch"),
            ("motionDetection", "boolean", "bool", "switch"),
            ("imageFlipped", "boolean", "bool", "switch"),
            ("microphone", "boolean", "bool", "switch"),
            ("speaker", "boolean", "bool", "switch"),
            ("recordAutoStop", "boolean", "bool", "switch"),
            ("speakerVolume", "percent", "number", "number"),
            ("recordDuration", "seconds", "number", "number"),
            ("recordInterval", "seconds", "number", "number"),
            ("motionLightEnabled", "boolean", "bool", "switch"),
            ("motionLightDuration", "seconds", "number", "number"),
            ("motionLightBrightness", "percent", "number", "number"),
            ("motionLightMode", "scalar", "number", "number"),
        ]:
            for writable in (True, False):
                spec = properties(
                    {
                        "property": name,
                        "accessor": name,
                        "kind": kind,
                        "type": value_type,
                        "writable": writable,
                    }
                )[0]
                self.assertEqual(
                    classify(spec),
                    platform
                    if writable
                    else "binary_sensor"
                    if value_type == "bool"
                    else "sensor",
                )
        for name in ("privacy", "light"):
            self.assertEqual(
                classify(
                    properties(
                        {
                            "property": name,
                            "accessor": name,
                            "type": "bool",
                            "writable": False,
                        }
                    )[0]
                ),
                "binary_sensor",
            )

    def test_existing_numeric_domains_remain_unchanged(self):
        original = {
            "name": "recordDuration",
            "type": "number",
            "kind": "seconds",
            "writable": True,
            "min": 5,
            "max": 60,
            "step": 1,
        }
        spec = normalize_properties(
            {
                "properties": [original],
                "decodedProperties": {
                    "details": [
                        {
                            "accessor": "battery",
                            "reads": [
                                {
                                    "property": "recordDuration",
                                    "accessor": "recordDuration",
                                    "type": "number",
                                    "kind": "seconds",
                                    "writable": True,
                                    "min": 10,
                                    "max": 120,
                                    "step": 1,
                                }
                            ],
                        }
                    ]
                },
            }
        )[0]
        self.assertEqual((spec["min"], spec["max"], spec["step"]), (5, 60, 1))

    def test_motion_ranges_from_described_borrowed_reads(self):
        for name, kind, low, high in [
            ("motionLightDuration", "seconds", 1, 86400),
            ("motionLightBrightness", "percent", 1, 100),
            ("motionLightMode", "scalar", 0, 65535),
        ]:
            read = {
                "property": name,
                "accessor": name,
                "type": "number",
                "kind": kind,
                "writable": True,
                "min": low,
                "step": 1,
            }
            if name == "motionLightBrightness":
                read["max"] = high
            spec = properties(read)[0]
            entity = EufySdkNumber(coordinator(), "camera", spec)
            self.assertEqual(
                (entity.native_min_value, entity.native_max_value, entity.native_step),
                (low, high, 1),
            )
            self.assertEqual(entity.unique_id, f"camera_{name}")
            self.assertEqual(
                entity.native_unit_of_measurement,
                {"seconds": "s", "percent": "%"}.get(kind),
            )

    def test_schedule_count_and_private_attributes(self):
        obj = coordinator()
        obj.data["camera"]["state"]["lightSchedule"] = json.dumps(
            [
                {
                    "day": 1,
                    "latitude": "private",
                    "nested": {"longtitude": "private", "start": 2},
                }
            ]
        )
        entity = EufyLightScheduleSensor(obj, "camera", {"name": "lightSchedule"})
        self.assertEqual(entity.native_value, 1)
        self.assertEqual(
            entity.extra_state_attributes["entries"],
            [{"day": 1, "nested": {"start": 2}}],
        )
        self.assertNotIn("private", json.dumps(entity.extra_state_attributes))
        obj.data["camera"]["state"]["lightSchedule"] = "bad"
        self.assertIsNone(entity.native_value)
        self.assertEqual(entity.unique_id, "camera_lightSchedule")
        # HA refuses sensors in the config category.
        self.assertEqual(entity.entity_category, EntityCategory.DIAGNOSTIC)

    def test_schedule_validation_without_assumed_entry_schema(self):
        for value in ([], [{"unknown": [1, 2]}], [1, "entry"]):
            self.assertEqual(validate_entries(value), value)
        for value in (None, "[]", {}, [float("nan")], [object()], ["x" * 65536]):
            with self.assertRaises(vol.Invalid):
                validate_entries(value)

    def test_readonly_migration_and_existing_control_identity(self):
        obj = coordinator()
        entry = SimpleNamespace(
            runtime_data=SimpleNamespace(
                coordinator=obj,
                properties={
                    "camera": [
                        {"name": "light", "type": "bool", "writable": False},
                        {"name": "enabled", "type": "bool", "writable": True},
                    ]
                },
            )
        )
        decisions = _presentation_decisions(entry)
        self.assertTrue(decisions[("switch", "camera_light")])
        self.assertTrue(decisions[("switch", "camera_privacy")])
        self.assertFalse(decisions[("binary_sensor", "camera_light")])
        self.assertFalse(decisions[("switch", "camera_enabled")])
        obj.data["station"]["model"] = "T8030"
        self.assertFalse(t9000_attached(obj, "camera"))
        self.assertFalse(_presentation_decisions(entry)[("switch", "camera_light")])

    def test_registry_retire_release_and_user_preferences(self):
        for preference in (None, "opt_out", "user_disabled"):
            with self.subTest(preference=preference):
                obj = coordinator()
                spec = {"name": "light", "type": "bool", "writable": False}
                entry = SimpleNamespace(
                    entry_id="synthetic-entry",
                    async_on_unload=Mock(),
                    runtime_data=SimpleNamespace(
                        coordinator=obj, properties={"camera": [spec]}
                    ),
                )
                original = Entry(
                    entity_id="switch.light",
                    domain="switch",
                    unique_id="camera_light",
                    disabled_by=er.RegistryEntryDisabler.USER
                    if preference == "user_disabled"
                    else None,
                    options={
                        "eufy_sdk": {"property_presentation_v2": {"opt_out": True}}
                    }
                    if preference == "opt_out"
                    else {},
                )
                registry = Registry([original])
                hass = Mock(data={})
                with (
                    patch.object(er, "async_get", return_value=registry),
                    patch.object(
                        er,
                        "async_entries_for_config_entry",
                        side_effect=lambda *_, registry=registry: list(
                            registry.entities.values()
                        ),
                    ),
                ):
                    async_reconcile_property_entities(hass, entry)
                    retired = registry.async_get(original.entity_id)
                    if preference is None:
                        self.assertEqual(
                            retired.disabled_by, er.RegistryEntryDisabler.INTEGRATION
                        )
                    else:
                        self.assertEqual(retired, original)
                    spec["writable"] = True
                    async_reconcile_property_entities(hass, entry)
                    self.assertEqual(registry.async_get(original.entity_id), original)
                    spec["writable"] = False
                    async_reconcile_property_entities(hass, entry)
                    if preference:
                        self.assertEqual(
                            registry.async_get(original.entity_id), original
                        )

    def test_registry_unknown_parent_neither_retires_nor_releases(self):
        for parent in (None, {}, {"error": "unavailable", "model": "T9000"}):
            for owned in (False, True):
                with self.subTest(parent=parent, owned=owned):
                    obj = coordinator()
                    if parent is None:
                        obj.data.pop("station")
                    else:
                        obj.data["station"] = parent
                    entry = SimpleNamespace(
                        entry_id="synthetic-entry",
                        async_on_unload=Mock(),
                        runtime_data=SimpleNamespace(
                            coordinator=obj,
                            properties={
                                "camera": [
                                    {"name": "light", "type": "bool", "writable": False}
                                ]
                            },
                        ),
                    )
                    original = Entry(
                        entity_id="switch.light",
                        domain="switch",
                        unique_id="camera_light",
                        disabled_by=er.RegistryEntryDisabler.INTEGRATION
                        if owned
                        else None,
                        options={
                            "eufy_sdk": {
                                "property_presentation_v2": {"owned_disabled": True}
                            }
                        }
                        if owned
                        else {},
                    )
                    registry = Registry([original])
                    with (
                        patch.object(er, "async_get", return_value=registry),
                        patch.object(
                            er,
                            "async_entries_for_config_entry",
                            return_value=[original],
                        ),
                    ):
                        async_reconcile_property_entities(Mock(data={}), entry)
                    self.assertEqual(registry.async_get(original.entity_id), original)


class ActionsTests(unittest.IsolatedAsyncioTestCase):
    async def test_recording_quality_option_mapping(self):
        for label in ("2K", "3K"):
            await self.check_recording_quality_option_mapping(label)

    async def check_recording_quality_option_mapping(self, maximum: str):
        obj = coordinator()
        spec = properties(
            {
                "property": "recordingQuality",
                "accessor": "recordingQuality",
                "type": "string",
                "kind": "enum",
                "values": [1, 2, 3],
                "labels": {"1": "720P", "2": "1080P", "3": maximum},
                "writable": True,
            }
        )[0]
        entity = EufySdkSelect(obj, "camera", spec)
        entity.write = AsyncMock()
        self.assertEqual(entity.options, ["720P", "1080P", maximum])
        for tier, label in [(1, "720P"), (2, "1080P"), (3, maximum)]:
            obj.data["camera"]["state"]["recordingQuality"] = tier
            self.assertEqual(entity.current_option, label)
            await entity.async_select_option(label)
            entity.write.assert_awaited_with(tier)
        self.assertEqual(entity.unique_id, "camera_recordingQuality")

    async def test_quick_buttons_setup_refresh_reload_never_play(self):
        obj = coordinator()
        added = []
        scheduled = []
        entry = SimpleNamespace(
            runtime_data=SimpleNamespace(coordinator=obj),
            async_create_background_task=Mock(
                side_effect=lambda _hass, coroutine, _name: scheduled.append(coroutine)
            ),
        )
        for _ in range(2):
            await async_setup_entry(Mock(), entry, added.extend)
        client = obj.config_entry.runtime_data.client
        client.quick_responses.assert_not_awaited()
        for task in scheduled:
            await task
        client.play_quick_response.assert_not_awaited()
        catalog = QuickResponseButtons(obj, "camera", added.extend)
        await catalog.refresh()
        await catalog.refresh()
        button = catalog.buttons[2]
        self.assertEqual(button.name, "Leave it here")
        self.assertEqual(button.unique_id, "camera_quick_response_2")
        refresh = EufyQuickResponseRefreshButton(obj, "camera", catalog)
        await refresh.async_press()
        client.play_quick_response.assert_not_awaited()
        await button.async_press()
        client.play_quick_response.assert_awaited_once_with("camera", 2)
        client.play_quick_response.side_effect = EufySdkApiClientError("failed")
        with self.assertRaisesRegex(HomeAssistantError, "may still play") as raised:
            await button.async_press()
        self.assertNotIsInstance(raised.exception, EufySdkApiClientError)
        await refresh.async_press()
        self.assertEqual(client.play_quick_response.await_count, 2)
        client.quick_responses.return_value = []
        await catalog.refresh()
        with self.assertRaises(HomeAssistantError):
            await button.async_press()
        self.assertEqual(client.play_quick_response.await_count, 2)

    async def test_quick_api_is_single_rpc_no_failure_retry(self):
        client = object.__new__(EufySdkApiClient)
        client.rpc = AsyncMock(side_effect=EufySdkApiClientError("failed"))
        with self.assertRaises(EufySdkApiClientError):
            await client.play_quick_response("camera", 2)
        client.rpc.assert_awaited_once_with(
            "doorbell.playQuickResponse", timeout=75, sn="camera", voiceId=2
        )

    async def test_background_list_retry_recovers_and_is_bounded(self):
        for recover in (True, False):
            obj = coordinator()
            client = obj.config_entry.runtime_data.client
            client.quick_responses.side_effect = [
                EufySdkApiClientError("failed"),
                [{"voiceId": 2, "name": "Leave it here"}]
                if recover
                else EufySdkApiClientError("failed"),
            ]
            added, tasks = [], []
            entry = SimpleNamespace(
                runtime_data=SimpleNamespace(coordinator=obj),
                async_create_background_task=Mock(
                    side_effect=lambda _hass, coroutine, _name, tasks=tasks: (
                        tasks.append(coroutine)
                    )
                ),
            )
            await async_setup_entry(Mock(), entry, added.extend)
            client.quick_responses.assert_not_awaited()
            with patch(
                "custom_components.eufy_sdk.button.asyncio.sleep",
                new_callable=AsyncMock,
            ) as delay:
                await tasks.pop()
            delay.assert_awaited_once_with(30)
            self.assertEqual(client.quick_responses.await_count, 2)
            catalog = next(
                button.catalogue
                for button in added
                if isinstance(button, EufyQuickResponseRefreshButton)
            )
            self.assertEqual(bool(catalog.buttons), recover)
            if recover:
                button = catalog.buttons[2]
                self.assertEqual(button.unique_id, "camera_quick_response_2")
                client.quick_responses.side_effect = None
                client.quick_responses.return_value = []
                await catalog.refresh()
                self.assertNotIn(2, catalog.responses)
                client.quick_responses.return_value = [
                    {"voiceId": 2, "name": "Updated"}
                ]
                await catalog.refresh()
                self.assertIs(catalog.buttons[2], button)
                self.assertEqual(button.name, "Updated")
            client.play_quick_response.assert_not_awaited()

    async def test_manual_list_failure_has_short_user_error(self):
        obj = coordinator()
        obj.config_entry.runtime_data.client.quick_responses.side_effect = (
            EufySdkApiClientError("private failure")
        )
        button = EufyQuickResponseRefreshButton(
            obj, "camera", QuickResponseButtons(obj, "camera", Mock())
        )
        with self.assertRaisesRegex(HomeAssistantError, "list unavailable") as raised:
            await button.async_press()
        self.assertNotIsInstance(raised.exception, EufySdkApiClientError)
        self.assertNotIn("private failure", str(raised.exception))
        obj.config_entry.runtime_data.client.play_quick_response.assert_not_awaited()

    async def test_schedule_service_target_validation_dispatch_and_unload(self):
        obj = coordinator()
        runtime = obj.config_entry.runtime_data
        runtime.coordinator = obj
        runtime.properties = {"camera": [{"name": "lightSchedule", "writable": True}]}
        runtime.client.set_light_schedule = AsyncMock()
        obj.async_request_refresh = AsyncMock()
        entry = SimpleNamespace(
            entry_id="fixture", runtime_data=runtime, async_on_unload=Mock()
        )
        hass = Mock(data={})
        register_schedule_service(hass, entry)
        args, kwargs = hass.services.async_register.call_args
        handler = args[2]
        schema = kwargs["schema"]
        with self.assertRaises(vol.Invalid):
            schema({"device_id": "fixture", "entries": "[]"})
        data = schema({"device_id": "fixture", "entries": []})
        device = SimpleNamespace(identifiers={("eufy_sdk", "camera")})
        with patch(
            "custom_components.eufy_sdk.light_schedule.dr.async_get",
            return_value=Mock(async_get=Mock(return_value=device)),
        ):
            await handler(SimpleNamespace(data=data))
            runtime.client.set_light_schedule.assert_awaited_once_with("camera", [])
            obj.async_request_refresh.assert_awaited_once()
            runtime.client.set_light_schedule.side_effect = EufySdkApiClientError(
                "private failure"
            )
            with self.assertRaisesRegex(
                HomeAssistantError, "could not be confirmed"
            ) as raised:
                await handler(SimpleNamespace(data=data))
            self.assertNotIsInstance(raised.exception, EufySdkApiClientError)
            self.assertNotIn("private failure", str(raised.exception))
            runtime.properties["camera"][0]["writable"] = False
            with self.assertRaises(HomeAssistantError):
                await handler(SimpleNamespace(data=data))
        entry.async_on_unload.call_args.args[0]()
        hass.services.async_remove.assert_called_once_with(
            "eufy_sdk", "set_light_schedule"
        )
