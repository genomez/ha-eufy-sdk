# ruff: noqa: ANN201, D100, D101, D102, D107, INP001, PT009

import importlib.util
import unittest
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.eufy_sdk.entity import classify
from custom_components.eufy_sdk.property_entities import (
    async_reconcile_property_entities,
    async_release_presentation_hides,
)


def baseline_v1_module():
    """Load the unchanged pilot.5 migration fixture with its real relative imports."""
    path = Path(__file__).parent / "fixtures" / "property_entities_v1.py"
    spec = importlib.util.spec_from_file_location(
        "custom_components.eufy_sdk._baseline_property_entities_v1", path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@dataclass
class Entry:
    entity_id: str = "sensor.synthetic"
    domain: str = "sensor"
    unique_id: str = "station_cameraInfo"
    platform: str = "eufy_sdk"
    config_entry_id: str = "synthetic-entry"
    hidden_by: str | None = None
    disabled_by: str | None = None
    options: dict = field(default_factory=dict)


class Registry:
    def __init__(self, entries: list[Entry]) -> None:
        self.entities = {entry.entity_id: entry for entry in entries}

    def async_get(self, entity_id: str):
        return self.entities.get(entity_id)

    def async_update_entity(self, entity_id: str, **changes: object):
        entry = self.entities[entity_id] = replace(self.entities[entity_id], **changes)
        return entry

    def async_update_entity_options(
        self, entity_id: str, domain: str, options: dict | None
    ):
        values = dict(self.entities[entity_id].options)
        if options is None:
            values.pop(domain, None)
        else:
            values[domain] = options
        return self.async_update_entity(entity_id, options=values)


class PresentationTests(unittest.TestCase):
    def test_diagnostic_metadata_blocks_every_generic_platform(self):
        for spec, expected in (
            ({"type": "bool", "writable": True}, "switch"),
            ({"type": "bool"}, "binary_sensor"),
            ({"type": "number", "writable": True, "kind": "seconds"}, "number"),
            ({"type": "enum", "writable": True, "enumValues": {"0": "low"}}, "select"),
            ({"type": "string"}, "sensor"),
            ({"type": "number", "kind": "bitfield"}, "bitfield"),
        ):
            self.assertEqual(classify(spec), expected)
            self.assertIsNone(classify({**spec, "unexposed": True}))
            self.assertIsNone(classify({**spec, "raw": True}))
        self.assertEqual(classify({"type": "enum", "raw": False}), "sensor")


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.hass = Mock(data={})
        self.entry = SimpleNamespace(
            entry_id="synthetic-entry",
            async_on_unload=Mock(),
            runtime_data=SimpleNamespace(
                coordinator=SimpleNamespace(data={"station": {"codec": "station"}}),
                properties={
                    "station": [
                        {"name": "cameraInfo", "unexposed": True},
                        {"name": "battery", "type": "number"},
                    ]
                },
            ),
        )

    def apply(self, registry: Registry):
        with (
            patch.object(er, "async_get", return_value=registry),
            patch.object(
                er,
                "async_entries_for_config_entry",
                side_effect=lambda reg, entry_id: [
                    e for e in reg.entities.values() if e.config_entry_id == entry_id
                ],
            ),
        ):
            async_reconcile_property_entities(self.hass, self.entry)

    def test_disables_diagnostic_preserving_identity_options_and_idempotence(self):
        original = Entry(
            options={"eufy_sdk": {"other": True}, "sensor": {"precision": 2}}
        )
        registry = Registry([original])
        self.apply(registry)
        self.apply(registry)
        entity = registry.async_get(original.entity_id)
        self.assertEqual(entity.disabled_by, er.RegistryEntryDisabler.INTEGRATION)
        self.assertIsNone(entity.hidden_by)
        self.assertEqual(entity.unique_id, original.unique_id)
        self.assertEqual(entity.options["eufy_sdk"]["other"], True)
        self.assertEqual(entity.options["sensor"], {"precision": 2})
        self.assertEqual(self.hass.bus.async_listen.call_count, 1)

    def test_initial_user_or_unowned_preferences_untouched(self):
        for original in (
            Entry(hidden_by=er.RegistryEntryHider.USER),
            Entry(disabled_by=er.RegistryEntryDisabler.USER),
            Entry(hidden_by=er.RegistryEntryHider.INTEGRATION),
            Entry(disabled_by=er.RegistryEntryDisabler.INTEGRATION),
        ):
            self.hass.data = {}
            registry = Registry([original])
            self.apply(registry)
            self.assertEqual(registry.async_get(original.entity_id), original)

    def test_v1_owned_hide_migrates_without_claiming_other_options(self):
        original = Entry(
            hidden_by=er.RegistryEntryHider.INTEGRATION,
            options={
                "eufy_sdk": {"property_presentation_v1": {"owned": True}, "other": 1}
            },
        )
        registry = Registry([original])
        self.apply(registry)
        result = registry.async_get(original.entity_id)
        self.assertIsNone(result.hidden_by)
        self.assertEqual(result.disabled_by, er.RegistryEntryDisabler.INTEGRATION)
        self.assertEqual(
            result.options["eufy_sdk"],
            {"property_presentation_v2": {"owned_disabled": True}, "other": 1},
        )

    def test_v1_opt_out_and_unloaded_user_overrides_are_respected(self):
        for old in (
            Entry(
                options={"eufy_sdk": {"property_presentation_v1": {"opt_out": True}}}
            ),
            Entry(options={"eufy_sdk": {"property_presentation_v1": {"owned": True}}}),
            Entry(
                hidden_by=er.RegistryEntryHider.USER,
                options={"eufy_sdk": {"property_presentation_v1": {"owned": True}}},
            ),
        ):
            self.hass.data = {}
            registry = Registry([old])
            self.apply(registry)
            result = registry.async_get(old.entity_id)
            self.assertEqual(result.hidden_by, old.hidden_by)
            self.assertIsNone(result.disabled_by)
            self.assertEqual(
                result.options["eufy_sdk"],
                {"property_presentation_v2": {"opt_out": True}},
            )

    def test_v1_owned_hide_with_user_disable_releases_only_owned_hide(self):
        old = Entry(
            hidden_by=er.RegistryEntryHider.INTEGRATION,
            disabled_by=er.RegistryEntryDisabler.USER,
            options={"eufy_sdk": {"property_presentation_v1": {"owned": True}}},
        )
        registry = Registry([old])
        self.apply(registry)
        self.assertIsNone(registry.async_get(old.entity_id).hidden_by)
        self.assertEqual(
            registry.async_get(old.entity_id).disabled_by, er.RegistryEntryDisabler.USER
        )

    def test_supported_unknown_and_unproven_entries_untouched(self):
        for old in (
            Entry(unique_id="station_battery"),
            Entry(unique_id="station_motionSensitivity"),
            Entry(unique_id="station_futureReading"),
            Entry(unique_id="missing_cameraInfo"),
            Entry(platform="other"),
            Entry(config_entry_id="another-entry"),
            Entry(unique_id="station_recordingQuality"),
        ):
            self.hass.data = {}
            registry = Registry([old])
            self.apply(registry)
            self.assertEqual(registry.async_get(old.entity_id), old)

    def test_missing_empty_or_error_schema_does_not_retire(self):
        for specs, device in (
            ({}, {}),
            ({"station": []}, {}),
            ({"station": [{"name": "battery"}]}, {"error": True}),
        ):
            self.hass.data = {}
            self.entry.runtime_data.properties = specs
            self.entry.runtime_data.coordinator.data = {"station": device}
            old = Entry(unique_id="station_recordDuration")
            registry = Registry([old])
            self.apply(registry)
            self.assertEqual(registry.async_get(old.entity_id), old)

    def test_corrected_platforms_return_to_supported_without_recreating(self):
        for domain, name in (
            ("number", "recordDuration"),
            ("select", "workingMode"),
            ("switch", "recordAutoStop"),
            ("sensor", "batteryTemperature"),
            ("binary_sensor", "solarConnected24h"),
        ):
            self.hass.data = {}
            old = Entry(
                entity_id=f"{domain}.synthetic",
                domain=domain,
                unique_id=f"station_{name}",
            )
            registry = Registry([old])
            self.apply(registry)
            self.assertEqual(
                registry.async_get(old.entity_id).disabled_by,
                er.RegistryEntryDisabler.INTEGRATION,
            )
            self.entry.runtime_data.properties["station"].append(
                {"name": name, "type": "number"}
            )
            self.apply(registry)
            self.assertEqual(registry.async_get(old.entity_id), old)

    def test_camera_lock_scope_and_raw_alone(self):
        old = Entry(entity_id="lock.synthetic", domain="lock", unique_id="station_lock")
        registry = Registry([old])
        self.entry.runtime_data.coordinator.data["station"] = {
            "codec": "camera",
            "capabilities": ["camera"],
        }
        self.apply(registry)
        self.assertEqual(
            registry.async_get(old.entity_id).disabled_by,
            er.RegistryEntryDisabler.INTEGRATION,
        )
        self.entry.runtime_data.coordinator.data["station"]["capabilities"].append(
            "lock"
        )
        self.apply(registry)
        self.assertEqual(registry.async_get(old.entity_id), old)
        self.entry.runtime_data.properties["station"].append(
            {"name": "recordingQuality", "raw": True}
        )
        old = Entry(unique_id="station_recordingQuality")
        self.hass.data = {}
        registry = Registry([old])
        self.apply(registry)
        self.assertEqual(registry.async_get(old.entity_id), old)

    def test_snooze_supersession_preserves_number_and_returns_sensor(self):
        self.entry.runtime_data.properties["station"].append(
            {
                "name": "snoozeTime",
                "type": "number",
                "kind": "seconds",
                "writable": True,
            }
        )
        sensor = Entry(unique_id="station_snoozeTime")
        number = Entry(
            entity_id="number.synthetic",
            domain="number",
            unique_id="station_snoozeTime",
        )
        registry = Registry([sensor, number])
        self.apply(registry)
        self.assertEqual(
            registry.async_get(sensor.entity_id).disabled_by,
            er.RegistryEntryDisabler.INTEGRATION,
        )
        self.assertEqual(registry.async_get(number.entity_id), number)
        self.entry.runtime_data.properties["station"][-1] = {
            "name": "snoozeTime",
            "type": "string",
            "writable": True,
        }
        self.apply(registry)
        self.assertEqual(registry.async_get(sensor.entity_id), sensor)

    def test_user_enable_while_unloaded_opts_out(self):
        registry = Registry([Entry()])
        self.apply(registry)
        registry.async_update_entity("sensor.synthetic", disabled_by=None)
        self.apply(registry)
        result = registry.async_get("sensor.synthetic")
        self.assertIsNone(result.disabled_by)
        self.assertEqual(
            result.options["eufy_sdk"]["property_presentation_v2"], {"opt_out": True}
        )

    def test_rollback_releases_both_versions_and_keeps_user_replacements(self):
        entries = [
            Entry(
                entity_id="sensor.v1",
                hidden_by=er.RegistryEntryHider.INTEGRATION,
                options={"eufy_sdk": {"property_presentation_v1": {"owned": True}}},
            ),
            Entry(
                entity_id="sensor.v2",
                disabled_by=er.RegistryEntryDisabler.INTEGRATION,
                options={
                    "eufy_sdk": {"property_presentation_v2": {"owned_disabled": True}}
                },
            ),
            Entry(
                entity_id="sensor.user",
                hidden_by=er.RegistryEntryHider.USER,
                disabled_by=er.RegistryEntryDisabler.USER,
                options={
                    "eufy_sdk": {
                        "property_presentation_v2": {"owned_disabled": True},
                        "other": 1,
                    }
                },
            ),
        ]
        registry = Registry(entries)
        with (
            patch.object(er, "async_get", return_value=registry),
            patch.object(
                er,
                "async_entries_for_config_entry",
                return_value=list(registry.entities.values()),
            ),
        ):
            async_release_presentation_hides(self.hass, "synthetic-entry")
        self.assertIsNone(registry.async_get("sensor.v1").hidden_by)
        self.assertIsNone(registry.async_get("sensor.v2").disabled_by)
        self.assertEqual(
            registry.async_get("sensor.user").hidden_by, er.RegistryEntryHider.USER
        )
        self.assertEqual(
            registry.async_get("sensor.user").disabled_by, er.RegistryEntryDisabler.USER
        )
        self.assertEqual(
            registry.async_get("sensor.user").options, {"eufy_sdk": {"other": 1}}
        )


class RealRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tempfile import TemporaryDirectory  # noqa: PLC0415

        from homeassistant.core import HomeAssistant  # noqa: PLC0415

        self.directory = TemporaryDirectory()
        self.hass = HomeAssistant(self.directory.name)
        dr.async_setup(self.hass)
        await dr.async_load(self.hass)
        self.registry = er.EntityRegistry(self.hass)
        await self.registry.async_load()
        self.unsubscribes = []
        self.config_entry = SimpleNamespace(
            entry_id="synthetic-entry",
            disabled_by=None,
            pref_disable_new_entities=False,
            async_on_unload=self.unsubscribes.append,
            runtime_data=SimpleNamespace(
                coordinator=SimpleNamespace(data={"station": {"codec": "station"}}),
                properties={"station": [{"name": "cameraInfo", "unexposed": True}]},
            ),
        )
        self.hass.config_entries = Mock()
        self.hass.config_entries.async_get_entry.return_value = self.config_entry
        self.entity = self.registry.async_get_or_create(
            "sensor",
            "eufy_sdk",
            "station_cameraInfo",
            config_entry=self.config_entry,
            suggested_object_id="synthetic_diagnostic",
        )
        self.patch = patch.object(er, "async_get", return_value=self.registry)
        self.patch.start()

    async def asyncTearDown(self):
        self.patch.stop()
        for unsubscribe in self.unsubscribes:
            unsubscribe()
        await self.hass.async_stop()
        self.directory.cleanup()

    def apply(self):
        async_reconcile_property_entities(self.hass, self.config_entry)

    def current(self):
        return self.registry.async_get(self.entity.entity_id)

    async def test_new_disable_own_event_does_not_opt_out_and_user_enable_does(self):
        self.apply()
        await self.hass.async_block_till_done()
        self.assertEqual(
            self.current().disabled_by, er.RegistryEntryDisabler.INTEGRATION
        )
        self.assertEqual(
            self.current().options["eufy_sdk"]["property_presentation_v2"],
            {"owned_disabled": True},
        )
        self.registry.async_update_entity(self.entity.entity_id, disabled_by=None)
        await self.hass.async_block_till_done()
        self.apply()
        self.assertIsNone(self.current().disabled_by)
        self.assertEqual(
            self.current().options["eufy_sdk"]["property_presentation_v2"],
            {"opt_out": True},
        )

    async def test_v1_migration_consumes_own_combined_queued_event(self):
        self.registry.async_update_entity_options(
            self.entity.entity_id,
            "eufy_sdk",
            {"property_presentation_v1": {"owned": True}},
        )
        self.registry.async_update_entity(
            self.entity.entity_id, hidden_by=er.RegistryEntryHider.INTEGRATION
        )
        await self.hass.async_block_till_done()
        self.apply()
        self.apply()
        await self.hass.async_block_till_done()
        self.assertIsNone(self.current().hidden_by)
        self.assertEqual(
            self.current().disabled_by, er.RegistryEntryDisabler.INTEGRATION
        )
        self.assertEqual(
            self.current().options["eufy_sdk"],
            {"property_presentation_v2": {"owned_disabled": True}},
        )

    async def test_immediate_user_enable_after_own_disable_before_event_delivery(self):
        self.apply()
        self.registry.async_update_entity(self.entity.entity_id, disabled_by=None)
        await self.hass.async_block_till_done()
        self.apply()
        self.assertIsNone(self.current().disabled_by)
        self.assertEqual(
            self.current().options["eufy_sdk"]["property_presentation_v2"],
            {"opt_out": True},
        )

    async def test_user_disable_replacement_is_not_cleared_on_support_return(self):
        self.apply()
        self.registry.async_update_entity(
            self.entity.entity_id, disabled_by=er.RegistryEntryDisabler.USER
        )
        await self.hass.async_block_till_done()
        self.config_entry.runtime_data.properties["station"][0]["unexposed"] = False
        self.apply()
        await self.hass.async_block_till_done()
        self.assertEqual(self.current().disabled_by, er.RegistryEntryDisabler.USER)
        self.assertEqual(self.current().options, {})

    async def test_manual_unhide_opts_out_and_releases_only_owned_disable(self):
        self.apply()
        await self.hass.async_block_till_done()
        self.registry.async_update_entity(
            self.entity.entity_id, hidden_by=er.RegistryEntryHider.USER
        )
        await self.hass.async_block_till_done()
        self.assertEqual(
            self.current().disabled_by, er.RegistryEntryDisabler.INTEGRATION
        )
        self.registry.async_update_entity(self.entity.entity_id, hidden_by=None)
        await self.hass.async_block_till_done()
        self.apply()
        self.assertIsNone(self.current().disabled_by)
        self.assertIsNone(self.current().hidden_by)
        self.assertEqual(
            self.current().options["eufy_sdk"]["property_presentation_v2"],
            {"opt_out": True},
        )

    async def test_owned_release_survives_queued_events_and_preserves_manual_hide(self):
        self.apply()
        self.registry.async_update_entity(
            self.entity.entity_id, hidden_by=er.RegistryEntryHider.USER
        )
        async_release_presentation_hides(self.hass, self.config_entry.entry_id)
        await self.hass.async_block_till_done()
        self.assertIsNone(self.current().disabled_by)
        self.assertEqual(self.current().hidden_by, er.RegistryEntryHider.USER)
        self.assertEqual(self.current().options, {})

    async def test_rollback_v1_preserves_recorded_enable_opt_out(self):
        self.apply()
        await self.hass.async_block_till_done()
        self.registry.async_update_entity(self.entity.entity_id, disabled_by=None)
        await self.hass.async_block_till_done()
        async_release_presentation_hides(
            self.hass, self.config_entry.entry_id, restore_v1=True
        )
        baseline_v1_module().async_reconcile_property_entities(
            self.hass, self.config_entry
        )
        await self.hass.async_block_till_done()
        self.assertIsNone(self.current().hidden_by)
        self.assertIsNone(self.current().disabled_by)
        self.assertEqual(
            self.current().options["eufy_sdk"],
            {"property_presentation_v1": {"opt_out": True}},
        )

    async def test_rollback_v1_detects_enable_while_unloaded(self):
        self.apply()
        await self.hass.async_block_till_done()
        for unsubscribe in self.unsubscribes:
            unsubscribe()
        self.unsubscribes.clear()
        self.registry.async_update_entity(self.entity.entity_id, disabled_by=None)
        await self.hass.async_block_till_done()
        async_release_presentation_hides(
            self.hass, self.config_entry.entry_id, restore_v1=True
        )
        baseline_v1_module().async_reconcile_property_entities(
            self.hass, self.config_entry
        )
        await self.hass.async_block_till_done()
        self.assertIsNone(self.current().hidden_by)
        self.assertIsNone(self.current().disabled_by)
        self.assertEqual(
            self.current().options["eufy_sdk"],
            {"property_presentation_v1": {"opt_out": True}},
        )

    async def test_rollback_v1_preserves_manual_unhide_opt_out(self):
        self.apply()
        await self.hass.async_block_till_done()
        self.registry.async_update_entity(
            self.entity.entity_id, hidden_by=er.RegistryEntryHider.USER
        )
        await self.hass.async_block_till_done()
        self.registry.async_update_entity(self.entity.entity_id, hidden_by=None)
        await self.hass.async_block_till_done()
        async_release_presentation_hides(
            self.hass, self.config_entry.entry_id, restore_v1=True
        )
        baseline_v1_module().async_reconcile_property_entities(
            self.hass, self.config_entry
        )
        await self.hass.async_block_till_done()
        self.assertIsNone(self.current().hidden_by)
        self.assertIsNone(self.current().disabled_by)

    async def test_rollback_v1_restores_prior_owned_hide_without_user_override(self):
        self.apply()
        await self.hass.async_block_till_done()
        async_release_presentation_hides(
            self.hass, self.config_entry.entry_id, restore_v1=True
        )
        baseline_v1_module().async_reconcile_property_entities(
            self.hass, self.config_entry
        )
        await self.hass.async_block_till_done()
        self.assertEqual(self.current().hidden_by, er.RegistryEntryHider.INTEGRATION)
        self.assertIsNone(self.current().disabled_by)
        self.assertEqual(
            self.current().options["eufy_sdk"],
            {"property_presentation_v1": {"owned": True}},
        )
