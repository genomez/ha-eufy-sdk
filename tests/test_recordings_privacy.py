# ruff: noqa: ASYNC240, ANN201, D100, D101, D102, INP001, PT009, PT027, SLF001
import json
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import test_setup_events as setup_fixture
from fixtures import recordings_v1 as fixture

import custom_components.eufy_sdk as integration
from custom_components.eufy_sdk import diagnostics
from custom_components.eufy_sdk import recordings_models as models
from custom_components.eufy_sdk.media_source import folder_id, navigation_path

CANARY = "synthetic-private-canary"


class PrivacyTests(unittest.IsolatedAsyncioTestCase):
    async def test_setup_gates_runtime_option_capability_and_private_notifications(
        self,
    ):
        for enabled, supported in ((True, True), (False, True), (True, False)):
            with self.subTest(enabled=enabled, supported=supported):
                client = fixture.client()
                client.set_poll_ms = AsyncMock()
                if not supported:
                    client.recordings_capability = None
                hass = Mock(data={})
                hass.config_entries.async_forward_entry_setups = AsyncMock()
                entry = Mock(
                    data={"host": "bridge.invalid", "port": 3000},
                    options={"recordings_enabled": enabled},
                    domain="eufy_sdk",
                )
                coordinator = Mock(data={})
                coordinator.async_config_entry_first_refresh = AsyncMock()
                with (
                    patch.object(integration, "async_reconcile_property_entities"),
                    patch.object(
                        integration,
                        "EufySdkDataUpdateCoordinator",
                        return_value=coordinator,
                    ),
                    patch.object(
                        integration, "EufySdkApiClient", return_value=client
                    ) as constructor,
                    patch.object(
                        integration, "async_get_clientsession", return_value=Mock()
                    ),
                    patch.object(
                        integration, "async_get_loaded_integration", return_value=Mock()
                    ),
                    patch.object(
                        integration, "async_register_view", new_callable=AsyncMock
                    ) as register,
                ):
                    await integration.async_setup_entry(hass, entry)
                runtime = entry.runtime_data.recordings
                self.assertEqual(runtime is not None, enabled and supported)
                if enabled:
                    # Register the authenticated route even before enrollment so
                    # a later capability event can enable it without a reload.
                    register.assert_awaited_once_with(hass)
                else:
                    register.assert_not_awaited()
                if runtime is not None:
                    runtime.changed = Mock()
                notification = {"event": "recordings.changed", "raw": CANARY}
                constructor.call_args.kwargs["on_event"](notification)
                hass.bus.async_fire.assert_not_called()
                client.rpc.assert_not_awaited()
                if runtime is not None:
                    runtime.changed.assert_called_once_with(notification)
                    runtime.close()

    async def test_unknown_private_fields_and_vendor_strings_are_not_retained(self):
        row = {
            **fixture.event(),
            "raw": CANARY,
            "serial": CANARY,
            "sourceKey": CANARY,
            "eventType": {"code": 7, "label": CANARY},
        }
        page = {**fixture.page([row]), "extra": CANARY}
        result = models.catalog(page, fixture.SCOPE, fixture.CAMERA)
        self.assertNotIn(CANARY, json.dumps(result))
        ready = models.cover(
            {**fixture.cover(), "extra": CANARY},
            fixture.CAMERA,
            fixture.EVENT,
        )
        self.assertNotIn(CANARY, json.dumps(ready))
        for key in ("video_path", "thumbPath", "coverSelector", "url", "transfer"):
            with (
                self.subTest(key=key),
                self.assertRaises(models.RecordingsError) as caught,
            ):
                models.catalog(
                    {**fixture.page(), key: CANARY}, fixture.SCOPE, fixture.CAMERA
                )
            self.assertNotIn(CANARY, str(caught.exception))

    async def test_safe_errors_suppress_wire_messages_and_request_values(self):
        client = fixture.client()
        client.rpc.side_effect = RuntimeError(CANARY)
        with self.assertRaises(models.RecordingsError) as caught:
            await client.recordings_cameras()
        self.assertNotIn(CANARY, str(caught.exception))
        for value in (CANARY, "https://source.invalid/" + CANARY):
            with self.assertRaises(models.RecordingsError) as caught:
                await client.recordings_catalog(value)
            self.assertNotIn(CANARY, str(caught.exception))

    async def test_changed_events_never_fetch_and_old_scope_cannot_mutate(self):
        runtime = await fixture.runtime()
        self.addCleanup(runtime.close)
        notification = {
            "event": "recordings.changed",
            "version": 1,
            "scopeId": fixture.SCOPE,
            "cameraId": fixture.CAMERA,
            "revision": "revision_2",
            "reason": "catalog",
            "raw": CANARY,
        }
        before = deepcopy(runtime.pages)
        runtime.changed(notification)
        runtime.changed({**notification, "scopeId": "s_" + "Z" * 22})
        self.assertEqual(runtime.pages, before)
        runtime.client.rpc.assert_not_awaited()
        runtime.client.recordings_capability = {"scopeId": "s_" + "Z" * 22}
        runtime.changed(notification)
        self.assertEqual(runtime.pages, {})
        self.assertEqual(runtime.interests, {})

    async def test_diagnostics_expose_opaque_folder_links_without_names_or_catalogs(
        self,
    ):
        runtime = await fixture.runtime()
        self.addCleanup(runtime.close)
        coordinator = Mock(data={}, last_update_success=True, solix_devices={})
        coordinator._light_safety_controllers = ()
        client = runtime.client
        client.auth_status = AsyncMock(return_value={"state": "ok"})
        data = SimpleNamespace(
            client=client, coordinator=coordinator, properties={}, recordings=runtime
        )
        result = await diagnostics.async_get_config_entry_diagnostics(
            Mock(), SimpleNamespace(runtime_data=data)
        )
        links = result["recordings"]
        self.assertEqual(len(links), 5)
        self.assertEqual(
            links[0],
            {
                "folder_media_id": folder_id(fixture.SCOPE, fixture.CAMERA),
                "navigation_path": navigation_path(fixture.SCOPE, fixture.CAMERA),
            },
        )
        self.assertNotIn("Synthetic camera", json.dumps(links))
        self.assertNotIn("eventId", json.dumps(links))

    async def test_recordings_event_dispatch_precedes_bus_and_entities_unchanged(self):
        setup = setup_fixture.SetupEventTests()
        await setup.asyncSetUp()
        for event in ("recordings.changed", "motion"):
            setup.on_event({"event": event, "raw": CANARY})
        self.assertEqual(setup.bus.async_fire.call_count, 1)
        self.assertEqual(setup.bus.async_fire.call_args.args[1]["event"], "motion")
        source = Path(integration.__file__).read_text()
        self.assertLess(
            source.index('if evt.get("event") == "recordings.changed"'),
            source.index("hass.bus.async_fire(EVENT_TYPE, evt)"),
        )
        for name in ("camera.py", "image.py", "event.py", "event_image.py"):
            contents = Path(integration.__file__).with_name(name).read_text()
            self.assertNotIn("recordings", contents)
        manifest = json.loads(
            Path(integration.__file__).with_name("manifest.json").read_text()
        )
        self.assertIn("media_source", manifest["dependencies"])
