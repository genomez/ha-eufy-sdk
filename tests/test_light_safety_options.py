"""Exercise floodlight options with real offline HA entries and flow selectors."""

# ruff: noqa: ANN001, ANN003, ANN201, ANN202, D102, FBT003, INP001, PT009, SLF001

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import MappingProxyType
from unittest.mock import AsyncMock, Mock, patch

from homeassistant import config_entries, loader
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry, entity_registry, frame, selector

import custom_components.eufy_sdk as integration
from custom_components.eufy_sdk.config_flow import EufySdkOptionsFlow
from custom_components.eufy_sdk.const import (
    CONF_HOST,
    CONF_LEGACY_DOORBELL_PRESSED,
    CONF_LIGHT_CORRECTION,
    CONF_LIGHT_CORRECTION_PAUSE,
    CONF_LIGHT_SAFETY_INTENT_ENTITIES,
    CONF_LIGHT_SAFETY_NOTIFY_SERVICE,
    CONF_POLL_INTERVAL,
    CONF_PORT,
    CONF_SOC_REFRESH,
    DOMAIN,
)

DEVICES = {
    "synthetic-station": {"model": "T9000", "name": "HomeBase"},
    "synthetic-front": {
        "model": "T8425",
        "name": "Front",
        "stationSn": "synthetic-station",
    },
    "synthetic-side": {
        "model": "T8426",
        "name": "Side",
        "stationSn": "synthetic-station",
    },
}


class LightSafetyOptionsTests(unittest.IsolatedAsyncioTestCase):
    """Mock only the bridge and platform loading, keeping HA flow/update behavior."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.tmp.name)
        frame.async_setup(self.hass)
        loader.async_setup(self.hass)
        device_registry.async_setup(self.hass)
        self.hass.config_entries = config_entries.ConfigEntries(self.hass, {})
        await self.hass.config_entries.async_initialize()
        await device_registry.async_load(self.hass)
        await entity_registry.async_load(self.hass)
        self.entry = config_entries.ConfigEntry(
            version=1,
            minor_version=1,
            domain=DOMAIN,
            title="Synthetic bridge",
            data={CONF_HOST: "synthetic.invalid", CONF_PORT: 3000},
            options={},
            source=config_entries.SOURCE_USER,
            unique_id="synthetic-bridge",
            discovery_keys=MappingProxyType({}),
            subentries_data=(),
        )
        with patch.object(self.hass.config_entries, "async_setup", new=AsyncMock()):
            await self.hass.config_entries.async_add(self.entry)
        self.client = Mock()
        self.client.set_poll_ms = AsyncMock()
        self.client.get_properties = AsyncMock(return_value=[{"name": "light"}])
        self.client.close = AsyncMock()

        async def first_refresh(coordinator):
            coordinator.data = deepcopy(DEVICES)
            coordinator.device_serial_counts = dict.fromkeys(DEVICES, 1)

        with (
            patch.object(integration, "EufySdkApiClient", return_value=self.client),
            patch.object(integration, "async_get_clientsession"),
            patch.object(integration, "async_get_loaded_integration"),
            patch.object(
                integration.EufySdkDataUpdateCoordinator,
                "async_config_entry_first_refresh",
                first_refresh,
            ),
            patch.object(
                self.hass.config_entries, "async_forward_entry_setups", new=AsyncMock()
            ),
        ):
            self.assertTrue(await integration.async_setup_entry(self.hass, self.entry))
        self.coordinator = self.entry.runtime_data.coordinator
        self.reload_patch = patch.object(
            self.hass.config_entries, "async_reload", new=AsyncMock(return_value=True)
        )
        self.reload = self.reload_patch.start()
        self.addCleanup(self.reload_patch.stop)
        self.flow = self._flow()

    def _flow(self):
        flow = EufySdkOptionsFlow()
        flow.hass = self.hass
        flow.handler = self.entry.entry_id
        return flow

    async def asyncTearDown(self):
        await self.hass.async_block_till_done()
        await self.entry._async_process_on_unload(self.hass)
        await self.hass.async_stop(force=True)
        self.tmp.cleanup()

    async def _update(self, options, **kwargs):
        self.hass.config_entries.async_update_entry(
            self.entry, options=deepcopy(options), **kwargs
        )
        await self.hass.async_block_till_done()

    def _controller(self):
        controller = Mock()
        self.coordinator._light_safety_controllers.add(controller)
        return controller

    async def _initial_values(self, **values):
        form = await self.flow.async_step_init()
        return form["data_schema"](values)

    async def test_default_off_and_existing_key_are_preserved(self):
        values = await self._initial_values()
        self.assertIs(values[CONF_LIGHT_CORRECTION], False)
        self.assertNotIn(CONF_LIGHT_SAFETY_NOTIFY_SERVICE, values)
        self.assertEqual(self.coordinator._light_safety_controllers, set())
        self.assertEqual(self.entry.options, {})

    async def test_binding_selectors_use_friendly_names_and_save_internal_map(self):
        values = await self._initial_values(
            **{
                CONF_LIGHT_CORRECTION: True,
                CONF_LIGHT_SAFETY_NOTIFY_SERVICE: "notify.test",
            }
        )
        front = await self.flow.async_step_init(values)
        self.assertEqual(front["step_id"], "light_safety_intent")
        self.assertEqual(front["description_placeholders"], {"light_name": "Front"})
        field, select = next(iter(front["data_schema"].schema.items()))
        self.assertEqual(field.schema, "intent_entity")
        self.assertIsInstance(select, selector.EntitySelector)
        side = await self.flow.async_step_light_safety_intent(
            front["data_schema"]({"intent_entity": "sensor.front_intent"})
        )
        self.assertEqual(side["description_placeholders"], {"light_name": "Side"})
        result = await self.flow.async_step_light_safety_intent(
            side["data_schema"]({"intent_entity": "input_select.side_intent"})
        )
        self.assertEqual(result["type"], FlowResultType.CREATE_ENTRY)
        self.assertEqual(
            result["data"][CONF_LIGHT_SAFETY_INTENT_ENTITIES],
            {
                "synthetic-front": "sensor.front_intent",
                "synthetic-side": "input_select.side_intent",
            },
        )
        self.assertEqual(
            result["data"][CONF_LIGHT_SAFETY_NOTIFY_SERVICE], "notify.test"
        )
        self.assertEqual(self.entry.options, {})

    async def test_bindings_restore_clear_and_preserve_temporarily_missing_lights(self):
        options = {
            CONF_LIGHT_SAFETY_INTENT_ENTITIES: {
                "synthetic-front": "sensor.front_intent",
                "synthetic-missing": "sensor.missing_intent",
            },
            "future_option": {"preserved": True},
        }
        await self._update(options)
        self.reload.reset_mock()
        front = await self.flow.async_step_init(await self._initial_values())
        field = next(iter(front["data_schema"].schema))
        self.assertEqual(field.description["suggested_value"], "sensor.front_intent")
        await self.flow.async_step_light_safety_intent({})
        result = await self.flow.async_step_light_safety_intent({})
        self.assertEqual(
            result["data"][CONF_LIGHT_SAFETY_INTENT_ENTITIES],
            {"synthetic-missing": "sensor.missing_intent"},
        )
        self.assertEqual(result["data"]["future_option"], {"preserved": True})
        self.assertEqual(self.entry.options, options)

    async def test_unsupported_models_parents_and_duplicate_ids_are_excluded(self):
        self.coordinator.data.update(
            {
                "synthetic-other": {"model": "T8170", "stationSn": "synthetic-station"},
                "synthetic-unbound": {"model": "T8425"},
                "synthetic-older": {"model": "T8425", "stationSn": "synthetic-old-hub"},
                "synthetic-old-hub": {"model": "T8010"},
            }
        )
        self.coordinator.device_serial_counts.update(
            dict.fromkeys(self.coordinator.data, 1)
        )
        self.coordinator.device_serial_counts["synthetic-side"] = 2
        form = await self.flow.async_step_init(await self._initial_values())
        self.assertEqual(form["description_placeholders"], {"light_name": "Front"})
        result = await self.flow.async_step_light_safety_intent({})
        self.assertEqual(result["type"], FlowResultType.CREATE_ENTRY)

    async def test_missing_names_never_fall_back_to_internal_ids(self):
        self.coordinator.data["synthetic-front"]["name"] = "synthetic-front"
        self.coordinator.data["synthetic-side"]["name"] = None
        first = await self.flow.async_step_init(await self._initial_values())
        second = await self.flow.async_step_light_safety_intent({})
        for index, form in enumerate((first, second), 1):
            self.assertEqual(
                form["description_placeholders"], {"light_name": f"Floodlight {index}"}
            )
            self.assertNotIn("synthetic-", repr(form["data_schema"]))

    async def test_no_runtime_devices_preserves_bindings(self):
        await self._update(
            {CONF_LIGHT_SAFETY_INTENT_ENTITIES: {"synthetic-front": "sensor.intent"}}
        )
        self.coordinator.data = None
        result = await self.flow.async_step_init(await self._initial_values())
        self.assertEqual(result["type"], FlowResultType.CREATE_ENTRY)
        self.assertEqual(
            result["data"][CONF_LIGHT_SAFETY_INTENT_ENTITIES],
            {"synthetic-front": "sensor.intent"},
        )

    async def test_notify_service_validation_and_optional_field_removal(self):
        await self._update(
            {
                CONF_LIGHT_CORRECTION_PAUSE: "input_boolean.pause_safety",
                CONF_LIGHT_SAFETY_NOTIFY_SERVICE: "notify.test",
            }
        )
        for service in ("switch.turn_on", "notify.invalid.service", "notify."):
            with self.subTest(service=service):
                result = await self.flow.async_step_init(
                    await self._initial_values(
                        **{CONF_LIGHT_SAFETY_NOTIFY_SERVICE: service}
                    )
                )
                self.assertEqual(
                    result["errors"],
                    {CONF_LIGHT_SAFETY_NOTIFY_SERVICE: "invalid_notify_service"},
                )
        self.coordinator.data = {}
        result = await self.flow.async_step_init(
            await self._initial_values(**{CONF_LIGHT_SAFETY_NOTIFY_SERVICE: "  "})
        )
        self.assertNotIn(CONF_LIGHT_CORRECTION_PAUSE, result["data"])
        self.assertNotIn(CONF_LIGHT_SAFETY_NOTIFY_SERVICE, result["data"])
        result = await self.flow.async_step_init(
            await self._initial_values(
                **{CONF_LIGHT_SAFETY_NOTIFY_SERVICE: " notify.test "}
            )
        )
        self.assertEqual(
            result["data"][CONF_LIGHT_SAFETY_NOTIFY_SERVICE], "notify.test"
        )

    async def test_each_safety_option_applies_live_to_all_controllers(self):
        first, second = self._controller(), self._controller()
        options = {}
        for key, value in (
            (CONF_LIGHT_CORRECTION, True),
            (CONF_LIGHT_CORRECTION_PAUSE, "input_boolean.pause_safety"),
            (CONF_LIGHT_SAFETY_INTENT_ENTITIES, {"synthetic-front": "sensor.intent"}),
            (CONF_LIGHT_SAFETY_NOTIFY_SERVICE, "notify.test"),
        ):
            options = options | {key: value}
            await self._update(options)
        self.assertEqual(first.options_updated.call_count, 4)
        self.assertEqual(second.options_updated.call_count, 4)
        self.reload.assert_not_awaited()
        self.client.close.assert_not_awaited()

    async def test_first_form_safety_change_does_not_reload_for_saved_defaults(self):
        controller = self._controller()
        await self.flow.async_step_init(
            await self._initial_values(**{CONF_LIGHT_CORRECTION: True})
        )
        await self.flow.async_step_light_safety_intent({})
        saved = await self.flow.async_step_light_safety_intent({})
        await self._update(saved["data"])
        controller.options_updated.assert_called_once_with()
        self.reload.assert_not_awaited()

    async def test_disabling_and_removing_bindings_apply_live(self):
        controller = self._controller()
        await self._update(
            {
                CONF_LIGHT_CORRECTION: True,
                CONF_LIGHT_SAFETY_INTENT_ENTITIES: {"synthetic-front": "sensor.intent"},
            }
        )
        await self._update({CONF_LIGHT_CORRECTION: False})
        await self._update({})
        self.assertEqual(controller.options_updated.call_count, 3)
        self.reload.assert_not_awaited()

    async def test_binding_replacement_and_removal_use_independent_snapshots(self):
        controller = self._controller()
        options = {
            CONF_LIGHT_SAFETY_INTENT_ENTITIES: {
                "synthetic-front": "sensor.first_intent"
            }
        }
        await self._update(options)
        previous = self.entry.runtime_data.options_snapshot
        options[CONF_LIGHT_SAFETY_INTENT_ENTITIES]["synthetic-front"] = (
            "sensor.second_intent"
        )
        await self._update(options)
        self.assertEqual(
            previous[CONF_LIGHT_SAFETY_INTENT_ENTITIES],
            {"synthetic-front": "sensor.first_intent"},
        )
        options[CONF_LIGHT_SAFETY_INTENT_ENTITIES].clear()
        await self._update(options)
        self.assertEqual(controller.options_updated.call_count, 3)
        self.reload.assert_not_awaited()

    async def test_non_safety_add_change_remove_and_mixed_changes_reload(self):
        controller = self._controller()
        options = {}
        changes = (
            {CONF_POLL_INTERVAL: 12},
            {CONF_SOC_REFRESH: 35},
            {CONF_LEGACY_DOORBELL_PRESSED: True},
            {"future_option": {"value": 1}},
            {"future_option": {"value": 2}, CONF_LIGHT_CORRECTION: True},
        )
        for change in changes:
            options = options | change
            await self._update(options)
        options.pop("future_option")
        await self._update(options)
        options.pop(CONF_POLL_INTERVAL)
        await self._update(options)
        self.assertEqual(self.reload.await_count, len(changes) + 2)
        controller.options_updated.assert_not_called()

    async def test_safety_changes_use_latest_full_options_snapshot(self):
        await self._update({"future_option": {"value": 1}})
        self.reload.reset_mock()
        controller = self._controller()
        await self._update({"future_option": {"value": 1}, CONF_LIGHT_CORRECTION: True})
        self.reload.assert_not_awaited()
        controller.options_updated.assert_called_once_with()
        await self._update(
            {"future_option": {"value": 2}, CONF_LIGHT_CORRECTION: False}
        )
        self.reload.assert_awaited_once_with(self.entry.entry_id)

    async def test_endpoint_data_change_still_reloads_with_safety_options(self):
        controller = self._controller()
        await self._update(
            {CONF_LIGHT_CORRECTION: True},
            data={CONF_HOST: "other.invalid", CONF_PORT: 3000},
        )
        self.reload.assert_awaited_once_with(self.entry.entry_id)
        controller.options_updated.assert_not_called()

    async def test_registry_handles_removal_and_isolates_callback_errors(self):
        first, second, broken = (
            self._controller(),
            self._controller(),
            self._controller(),
        )
        first.options_updated.side_effect = lambda: (
            self.coordinator._light_safety_controllers.discard(first)
        )
        broken.options_updated.side_effect = RuntimeError("synthetic-private-detail")
        with self.assertLogs(integration.LOGGER, level="ERROR") as logs:
            await self._update({CONF_LIGHT_CORRECTION: True})
        first.options_updated.assert_called_once_with()
        second.options_updated.assert_called_once_with()
        broken.options_updated.assert_called_once_with()
        self.assertNotIn("synthetic-private-detail", str(logs.output))
        self.reload.assert_not_awaited()

    async def test_all_translations_describe_new_options_and_binding_step(self):
        directory = Path(__file__).parents[1] / "custom_components/eufy_sdk"
        for path in (
            directory / "strings.json",
            *sorted((directory / "translations").glob("*.json")),
        ):
            with self.subTest(language=path.stem):
                options = json.loads(path.read_text(encoding="utf-8"))["options"]
                label = options["step"]["init"]["data"][CONF_LIGHT_CORRECTION]
                self.assertTrue(label)
                if path.stem in {"strings", "en"}:
                    self.assertEqual(label, "Floodlight safety")
                self.assertIn(
                    CONF_LIGHT_SAFETY_NOTIFY_SERVICE, options["step"]["init"]["data"]
                )
                self.assertIn(
                    "{light_name}", options["step"]["light_safety_intent"]["title"]
                )
                self.assertIn("invalid_notify_service", options["error"])
