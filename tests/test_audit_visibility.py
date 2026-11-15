"""Reviewed visibility, compatible reads and bounded volume controls."""

# ruff: noqa: ANN201, D101, D102, INP001, PT009, PT027
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.eufy_sdk.audit_visibility import (
    MARKER,
    async_migrate_audit_visibility,
    async_release_audit_hides,
    hidden_default,
)
from custom_components.eufy_sdk.entity import EufySdkPropertyEntity
from custom_components.eufy_sdk.manifest_reads import normalize_properties
from custom_components.eufy_sdk.number import EufySdkNumber
from custom_components.eufy_sdk.property_entities import _presentation_decisions
from custom_components.eufy_sdk.sensor import _is_sensor
from tests.test_property_entities import Entry, Registry


class AuditVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.hass = Mock(data={})
        self.coordinator = SimpleNamespace(
            data={
                "camera": {
                    "model": "T8425",
                    "stationSn": "station",
                    "state": {"soundDetection": False, "soundDetectionSensitivity": 3},
                },
                "station": {"model": "T9000", "state": {}},
            }
        )
        self.entry = SimpleNamespace(
            entry_id="synthetic-entry",
            runtime_data=SimpleNamespace(
                coordinator=self.coordinator,
                properties={
                    "camera": [
                        {"name": name}
                        for name in (
                            "soundDetection",
                            "soundDetectionSensitivity",
                            "soundDetectionType",
                            "recordingMode",
                            "powerSource",
                            "motionLightMode",
                        )
                    ],
                    "station": [
                        {"name": name}
                        for name in ("armingMode", "hubAlarmTone", "powerSource")
                    ],
                },
            ),
        )

    def registry_scope(self, entries: list[Entry]):
        registry = Registry(entries)
        stack = ExitStack()
        stack.enter_context(patch.object(er, "async_get", return_value=registry))
        stack.enter_context(
            patch.object(
                er,
                "async_entries_for_config_entry",
                side_effect=lambda reg, entry_id: [
                    e for e in reg.entities.values() if e.config_entry_id == entry_id
                ],
            )
        )
        self.addCleanup(stack.close)
        return registry

    def test_once_only_respects_user_hide_and_later_unhide(self):
        rows = [
            Entry(
                entity_id=f"select.{label}",
                domain="select",
                unique_id="camera_recordingMode",
                hidden_by=hidden,
            )
            for label, hidden in (
                ("visible", None),
                ("user", er.RegistryEntryHider.USER),
                ("other", er.RegistryEntryHider.INTEGRATION),
            )
        ]
        registry = self.registry_scope(rows)
        async_migrate_audit_visibility(self.hass, self.entry)
        self.assertEqual(
            registry.async_get("select.visible").hidden_by,
            er.RegistryEntryHider.INTEGRATION,
        )
        self.assertEqual(
            registry.async_get("select.user").hidden_by, er.RegistryEntryHider.USER
        )
        self.assertTrue(
            registry.async_get("select.other").options["eufy_sdk"][MARKER]["owned"]
        )
        registry.async_update_entity("select.visible", hidden_by=None)
        async_migrate_audit_visibility(self.hass, self.entry)
        self.assertIsNone(registry.async_get("select.visible").hidden_by)
        async_release_audit_hides(self.hass, self.entry.entry_id)
        async_migrate_audit_visibility(self.hass, self.entry)
        self.assertIsNone(registry.async_get("select.visible").hidden_by)
        self.assertEqual(
            registry.async_get("select.user").hidden_by, er.RegistryEntryHider.USER
        )
        self.assertIsNone(registry.async_get("select.other").hidden_by)

    def test_solar_charging_hidden_unless_solar_powered(self):
        solar = {"name": "solarPanelConnected"}
        power = {"name": "powerSource"}
        self.coordinator.data.update(
            {
                "panel": {"model": "T8170", "state": {"powerSource": 1}},
                "labelled": {"model": "T8171", "state": {"powerSource": "External Solar Panel"}},
                "battery": {"model": "T8134", "state": {"powerSource": 0}},
                "pending": {"model": "T8170", "state": {}},
                "hub": {"model": "T9000", "state": {}},
            }
        )
        self.entry.runtime_data.properties = {
            "panel": [solar, power],
            "labelled": [solar, power],
            "battery": [solar, power],
            "pending": [solar, power],
            "hub": [solar],
            "user": [solar, power],
        }
        self.coordinator.data["user"] = {"model": "T8134", "state": {"powerSource": "0"}}
        registry = self.registry_scope(
            [
                Entry(
                    entity_id=f"binary_sensor.{sn}",
                    domain="binary_sensor",
                    unique_id=f"{sn}_solarPanelConnected",
                    hidden_by=er.RegistryEntryHider.USER if sn == "user" else None,
                )
                for sn in ("panel", "labelled", "battery", "pending", "hub", "user")
            ]
        )
        async_migrate_audit_visibility(self.hass, self.entry)
        hidden = {
            sn: registry.async_get(f"binary_sensor.{sn}").hidden_by
            for sn in ("panel", "labelled", "battery", "pending", "hub", "user")
        }
        self.assertIsNone(hidden["panel"])
        self.assertIsNone(hidden["labelled"])
        self.assertEqual(hidden["battery"], er.RegistryEntryHider.INTEGRATION)
        self.assertEqual(hidden["hub"], er.RegistryEntryHider.INTEGRATION)
        self.assertEqual(hidden["user"], er.RegistryEntryHider.USER)
        # Unknown power source: no decision yet, so a later startup can still decide.
        self.assertIsNone(hidden["pending"])
        self.assertNotIn(MARKER, registry.async_get("binary_sensor.pending").options.get("eufy_sdk", {}))
        # Once decided, a user unhide sticks.
        registry.async_update_entity("binary_sensor.battery", hidden_by=None)
        async_migrate_audit_visibility(self.hass, self.entry)
        self.assertIsNone(registry.async_get("binary_sensor.battery").hidden_by)

    def test_release_only_owned_hides_without_changing_disable_or_other_options(self):
        registry = self.registry_scope(
            [
                Entry(
                    entity_id="number.synthetic",
                    domain="number",
                    unique_id="camera_motionLightMode",
                    disabled_by=er.RegistryEntryDisabler.USER,
                    options={"other": {"a": 1}},
                )
            ]
        )
        async_migrate_audit_visibility(self.hass, self.entry)
        async_release_audit_hides(self.hass, self.entry.entry_id)
        result = registry.async_get("number.synthetic")
        self.assertIsNone(result.hidden_by)
        self.assertEqual(result.disabled_by, er.RegistryEntryDisabler.USER)
        self.assertEqual(result.options["other"], {"a": 1})

    def test_reported_sound_and_arming_keep_presentation_across_later_unknown(self):
        rows = [
            Entry(
                entity_id="switch.synthetic",
                domain="switch",
                unique_id="camera_soundDetection",
            ),
            Entry(
                entity_id="select.synthetic",
                domain="select",
                unique_id="station_armingMode",
            ),
        ]
        registry = self.registry_scope(rows)
        async_migrate_audit_visibility(self.hass, self.entry)
        self.coordinator.data["camera"]["state"] = {}
        async_migrate_audit_visibility(self.hass, self.entry)
        self.assertTrue(
            all(row.hidden_by is None for row in registry.entities.values())
        )
        self.assertFalse(hidden_default(self.coordinator, "station", "armingMode"))
        self.assertFalse(hidden_default(self.coordinator, "camera", "soundDetection"))
        self.coordinator.data["station"]["model"] = "T8030"
        self.assertFalse(hidden_default(self.coordinator, "camera", "powerSource"))

    def test_new_read_entities_keep_visibility(self):
        for spec in (
            {"name": "powerSource", "type": "number"},
            {"name": "soundDetection", "type": "bool"},
        ):
            entity = EufySdkPropertyEntity(self.coordinator, "camera", spec)
            self.assertTrue(entity.entity_registry_visible_default)

    def test_sensor_and_unrelated_platforms_are_never_hidden(self):
        registry = self.registry_scope(
            [
                Entry(unique_id="camera_soundDetectionType"),
                Entry(
                    entity_id="switch.other",
                    domain="switch",
                    unique_id="camera_soundDetection",
                    platform="other",
                ),
            ]
        )
        async_migrate_audit_visibility(self.hass, self.entry)
        self.assertTrue(
            all(
                row.hidden_by is None and row.options == {}
                for row in registry.entities.values()
            )
        )

    def test_duration_number_migration_preserves_identity_and_user_choices(self):
        self.entry.runtime_data.properties["camera"].append(
            {"name": "motionLightDuration"}
        )
        rows = [
            Entry(
                entity_id="number.renamed_timer",
                domain="number",
                unique_id="camera_motionLightDuration",
                disabled_by=er.RegistryEntryDisabler.USER,
                options={"other": {"keep": True}},
            ),
            Entry(
                entity_id="select.motion_light_timer",
                domain="select",
                unique_id="camera_motionLightTimer",
            ),
        ]
        registry = self.registry_scope(rows)
        async_migrate_audit_visibility(self.hass, self.entry)
        number = registry.async_get(rows[0].entity_id)
        self.assertEqual(number.unique_id, rows[0].unique_id)
        self.assertEqual(number.hidden_by, er.RegistryEntryHider.INTEGRATION)
        self.assertEqual(number.disabled_by, rows[0].disabled_by)
        self.assertEqual(number.options["other"], {"keep": True})
        self.assertTrue(number.options["eufy_sdk"][MARKER]["owned"])
        self.assertEqual(registry.async_get(rows[1].entity_id), rows[1])
        registry.async_update_entity(rows[0].entity_id, hidden_by=None)
        async_migrate_audit_visibility(self.hass, self.entry)
        self.assertIsNone(registry.async_get(rows[0].entity_id).hidden_by)
        async_release_audit_hides(self.hass, self.entry.entry_id)
        self.assertIsNone(registry.async_get(rows[0].entity_id).hidden_by)

    def test_cold_sound_cache_defers_until_later_evidence(self):
        self.coordinator.data["camera"]["state"] = {}
        rows = [
            Entry(
                entity_id=f"{domain}.{name}", domain=domain, unique_id=f"camera_{name}"
            )
            for domain, name in (
                ("switch", "soundDetection"),
                ("number", "soundDetectionSensitivity"),
            )
        ]
        registry = self.registry_scope(rows)
        for _ in range(2):
            async_migrate_audit_visibility(self.hass, self.entry)
            for row in registry.entities.values():
                self.assertIsNone(row.hidden_by)
                self.assertNotIn(MARKER, row.options.get("eufy_sdk", {}))
        self.coordinator.data["camera"]["state"] = {
            "soundDetection": False,
            "soundDetectionSensitivity": 0,
        }
        async_migrate_audit_visibility(self.hass, self.entry)
        for row in registry.entities.values():
            self.assertIsNone(row.hidden_by)
            self.assertFalse(row.options["eufy_sdk"][MARKER]["owned"])

    def test_definitive_unsupported_sound_can_hide_but_report_wins(self):
        self.coordinator.data["camera"]["state"] = {}
        self.entry.runtime_data.properties["camera"] = [
            {"name": "soundDetection", "supported": False}
        ]
        registry = self.registry_scope(
            [
                Entry(
                    entity_id="switch.sound",
                    domain="switch",
                    unique_id="camera_soundDetection",
                )
            ]
        )
        async_migrate_audit_visibility(self.hass, self.entry)
        self.assertEqual(
            registry.async_get("switch.sound").hidden_by,
            er.RegistryEntryHider.INTEGRATION,
        )
        self.coordinator.data["camera"]["state"]["soundDetection"] = False
        async_migrate_audit_visibility(self.hass, self.entry)
        self.assertIsNone(registry.async_get("switch.sound").hidden_by)

    def test_station_model_prefix_matches_attached_policy(self):
        self.coordinator.data["station"]["model"] = "T9000-family"
        self.assertTrue(hidden_default(self.coordinator, "station", "powerSource"))
        self.assertTrue(hidden_default(self.coordinator, "camera", "powerSource"))


class VolumeTests(unittest.IsolatedAsyncioTestCase):
    async def test_numbers_keep_property_identity_and_percent_bounds(self):
        for name in ("ringtoneVolume", "promptVolume"):
            coordinator = Mock(data={"camera": {"state": {name: 50}}})
            spec = {
                "name": name,
                "type": "number",
                "kind": "percent",
                "unit": "%",
                "writable": True,
                "min": 0,
                "max": 100,
                "step": 1,
                "writeOnly": name == "promptVolume",
            }
            spec = normalize_properties(
                {"properties": [spec], "decodedProperties": {"details": []}}
            )[0]
            number = EufySdkNumber(coordinator, "camera", spec)
            number.write = AsyncMock()
            self.assertEqual(number.unique_id, f"camera_{name}")
            self.assertEqual(
                (number.native_min_value, number.native_max_value), (0, 100)
            )
            await number.async_set_native_value(75)
            number.write.assert_awaited_once_with(75)
            if name == "ringtoneVolume":
                self.assertTrue(_is_sensor(spec, t9000=True))
                self.assertFalse(_is_sensor(spec))
                entry = SimpleNamespace(
                    runtime_data=SimpleNamespace(
                        coordinator=SimpleNamespace(
                            data={
                                "camera": {"stationSn": "station"},
                                "station": {"model": "T9000"},
                            }
                        ),
                        properties={"camera": [spec]},
                    )
                )
                self.assertFalse(
                    _presentation_decisions(entry)[("sensor", "camera_ringtoneVolume")]
                )

    def test_current_report_is_never_excluded_by_new_range(self):
        coordinator = Mock(data={"camera": {"state": {"motionLightDuration": 1200}}})
        number = EufySdkNumber(
            coordinator,
            "camera",
            {
                "name": "motionLightDuration",
                "type": "number",
                "kind": "seconds",
                "min": 30,
                "max": 900,
            },
        )
        self.assertEqual((number.native_min_value, number.native_max_value), (30, 1200))

    async def test_display_widening_does_not_widen_write_bounds_or_shift_steps(self):
        for current in (1200, 15):
            number = EufySdkNumber(
                Mock(data={"camera": {"state": {"motionLightDuration": current}}}),
                "camera",
                {
                    "name": "motionLightDuration",
                    "type": "number",
                    "kind": "seconds",
                    "min": 30,
                    "max": 900,
                    "step": 30,
                },
            )
            number.write = AsyncMock()
            self.assertEqual(number.native_value, current)
            for invalid in (15, 1200, 45):
                with self.assertRaisesRegex(
                    HomeAssistantError, "permitted (range|step)"
                ):
                    await number.async_set_native_value(invalid)
            number.write.assert_not_awaited()
            await number.async_set_native_value(60)
            number.write.assert_awaited_once_with(60)
