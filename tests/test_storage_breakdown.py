"""The two cached component sensors preserve identity, validity and expiry owners."""

import copy
import logging
import unittest
from datetime import timedelta
from unittest.mock import Mock, patch

from custom_components.eufy_sdk.const import DOMAIN
from custom_components.eufy_sdk.storage_telemetry import FRESHNESS_MS, storage_sensors
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import EntityPlatform
from test_storage_telemetry import MODULE, NOW, UNIT_PROFILE, coordinator, device

OLD_FIELDS = (
    "total_bytes",
    "used_bytes",
    "available_bytes",
    "used_percent",
    "observed_at",
)
NEW_FIELDS = ("video_bytes", "system_bytes")


def by_field(coord):
    return {entity._field: entity for entity in storage_sensors(coord)}


class BreakdownValueTests(unittest.TestCase):
    def test_original_identities_and_only_two_optional_diagnostics(self):
        coord = coordinator()
        entities = by_field(coord)
        self.assertEqual(set(entities), set(OLD_FIELDS + NEW_FIELDS))
        self.assertEqual(
            [entities[field].unique_id for field in OLD_FIELDS],
            ["S1_storage_" + field for field in OLD_FIELDS],
        )
        for field in NEW_FIELDS:
            entity = entities[field]
            self.assertEqual(entity.unique_id, "S1_storage_" + field)
            self.assertFalse(entity.entity_registry_enabled_default)
            self.assertEqual(entity.entity_category, EntityCategory.DIAGNOSTIC)
            self.assertEqual(entity.device_info["identifiers"], {(DOMAIN, "S1")})
            self.assertEqual(entity.native_unit_of_measurement, "B")
            self.assertEqual(entity.device_class, SensorDeviceClass.DATA_SIZE)
            self.assertEqual(entity.state_class, SensorStateClass.MEASUREMENT)
        self.assertEqual(
            entities["observed_at"].device_class, SensorDeviceClass.TIMESTAMP
        )
        self.assertIsNone(entities["observed_at"].state_class)

    def test_component_values_and_provenance_reuse_same_observation_without_requests(
        self,
    ):
        coord = coordinator()
        before = copy.deepcopy(coord.data)
        entities = by_field(coord)
        with patch(MODULE + "._now_ms", return_value=NOW):
            self.assertEqual(
                [entities[field].native_value for field in NEW_FIELDS], [700, 50]
            )
            self.assertEqual(
                sum(entities[field].native_value for field in NEW_FIELDS),
                entities["used_bytes"].native_value,
            )
            for field in NEW_FIELDS:
                attributes = entities[field].extra_state_attributes
                self.assertEqual(attributes["storage_status"], "current")
                self.assertEqual(attributes["observed_at_ms"], NOW - 1000)
                self.assertEqual(attributes["unit_profile"], UNIT_PROFILE)
                self.assertEqual(attributes["correlation"], "time-associated")
                self.assertIs(attributes["exactly_correlated"], False)
            coord.data["S1"]["storageTelemetry"]["values"].update(
                video_bytes=0, system_bytes=750
            )
            self.assertEqual(
                [entities[field].native_value for field in NEW_FIELDS], [0, 750]
            )
            coord.data = before
        self.assertEqual(coord.data, before)
        self.assertEqual(coord.config_entry.runtime_data.client.mock_calls, [])

    def test_missing_inconsistent_disabled_or_unavailable_are_never_zero_filled(self):
        for field in NEW_FIELDS:
            for value in (None, True, -1, "50", 50.5, 1 << 53):
                coord = coordinator()
                coord.data["S1"]["storageTelemetry"]["values"][field] = value
                with patch(MODULE + "._now_ms", return_value=NOW):
                    for entity in by_field(coord).values():
                        if entity._field in NEW_FIELDS:
                            self.assertFalse(entity.available)
                            self.assertIsNone(entity.native_value)
                self.assertEqual(coord.config_entry.runtime_data.client.mock_calls, [])
        for status in ("disabled", "unknown"):
            row = device(status, None)
            row["storageTelemetry"].update(
                values=None, unitProfile=None, correlation=None
            )
            coord = coordinator({"S1": row})
            with patch(MODULE + "._now_ms", return_value=NOW):
                for field in NEW_FIELDS:
                    entity = by_field(coord)[field]
                    self.assertTrue(entity.available)
                    self.assertIsNone(entity.native_value)
                    self.assertIsNone(entity.extra_state_attributes["observed_at_ms"])
        coord = coordinator()
        coord.last_update_success = False
        with patch(MODULE + "._now_ms", return_value=NOW):
            for field in NEW_FIELDS:
                entity = by_field(coord)[field]
                self.assertFalse(entity.available)
                self.assertIsNone(entity.native_value)
                self.assertIsNone(entity.extra_state_attributes["observed_at_ms"])

    def test_non_s1_camera_unknown_firmware_and_parent_owned_rows_remain_excluded(self):
        devices = {"S1": device()}
        for index, (key, value) in enumerate(
            (
                ("model", "T8030"),
                ("model", "T9000X"),
                ("codec", "camera"),
                ("firmwareVersion", "4.4.0.5"),
                ("firmwareVersion", None),
                ("stationSn", "other-owner"),
                ("storageTelemetry", None),
            )
        ):
            row = device()
            row[key] = value
            devices["other-" + str(index)] = row
        before = copy.deepcopy(devices)
        coord = coordinator(devices)
        entities = storage_sensors(coord)
        self.assertEqual(len(entities), 7)
        self.assertTrue(all(entity._sn == "S1" for entity in entities))
        self.assertEqual(coord.data, before)
        self.assertEqual(coord.config_entry.runtime_data.client.mock_calls, [])


class BreakdownNativeTests(unittest.IsolatedAsyncioTestCase):
    async def registry(self, config_path):
        hass = HomeAssistant(config_path)
        devices = dr.DeviceRegistry(hass)
        hass.data[dr.DATA_REGISTRY] = devices
        await devices.async_load(load_empty=True)
        registry = er.EntityRegistry(hass)
        hass.data[er.DATA_REGISTRY] = registry
        await registry.async_load()
        return hass, devices, registry

    async def test_both_native_component_states_expire_without_new_snapshot_and_keep_evidence(
        self,
    ):
        hass, _, _ = await self.registry("/tmp/offline-storage-breakdown-expiry")
        coord = coordinator()
        entities = by_field(coord)
        selected = [entities[field] for field in NEW_FIELDS + ("observed_at",)]
        platform = EntityPlatform(
            hass=hass,
            logger=logging.getLogger(__name__),
            domain="sensor",
            platform_name=DOMAIN,
            platform=None,
            scan_interval=timedelta(seconds=30),
            entity_namespace=None,
        )
        scheduled = []

        def schedule(*args):
            cancel = Mock()
            scheduled.append(cancel)
            return cancel

        with (
            patch(MODULE + "._now_ms", return_value=NOW),
            patch(MODULE + ".async_call_later", side_effect=schedule) as later,
        ):
            for entity in selected:
                entity.add_to_platform_start(hass, platform, None)
                entity.entity_id = "sensor.native_" + entity._field
                await entity.add_to_platform_finish()
            self.assertEqual(
                [hass.states.get(entity.entity_id).state for entity in selected[:2]],
                ["700", "50"],
            )
            for entity in selected[:2]:
                self.assertEqual(
                    hass.states.get(entity.entity_id).attributes["observed_at_ms"],
                    NOW - 1000,
                )
            original_timestamp = hass.states.get(selected[-1].entity_id).state
            self.assertEqual(later.call_count, 3)
        with patch(MODULE + "._now_ms", return_value=NOW + FRESHNESS_MS):
            for entity in selected:
                entity._expire(None)
            for entity in selected[:2]:
                state = hass.states.get(entity.entity_id)
                self.assertEqual(state.state, "unavailable")
                self.assertEqual(
                    entity.extra_state_attributes["observed_at_ms"], NOW - 1000
                )
                self.assertEqual(
                    entity.extra_state_attributes["storage_status"], "stale"
                )
                self.assertIsNone(entity.native_value)
                self.assertIsNone(entity._expiry_cancel)
            self.assertEqual(
                hass.states.get(selected[-1].entity_id).state, original_timestamp
            )
            self.assertEqual(
                hass.states.get(selected[-1].entity_id).attributes["observed_at_ms"],
                NOW - 1000,
            )
            coord.data["S1"]["storageTelemetry"].update(status="stale", values=None)
            for entity in selected:
                entity._handle_coordinator_update()
            self.assertEqual(
                hass.states.get(selected[-1].entity_id).state, original_timestamp
            )
        coord.data["S1"] = device()
        with (
            patch(MODULE + "._now_ms", return_value=NOW),
            patch(MODULE + ".async_call_later", side_effect=schedule),
        ):
            for entity in selected:
                entity._schedule_expiry()
                cancel = scheduled[-1]
                await entity.async_will_remove_from_hass()
                cancel.assert_called_once()
                self.assertIsNone(entity._expiry_cancel)
        self.assertEqual(coord.config_entry.runtime_data.client.mock_calls, [])
        await hass.async_stop()

    async def test_native_registry_keeps_five_enabled_ids_and_other_device_and_disables_two_new(
        self,
    ):
        hass, devices, registry = await self.registry(
            "/tmp/offline-storage-breakdown-registry"
        )
        hass.config_entries = Mock()
        hass.config_entries.async_get_entry.return_value = Mock(
            disabled_by=None, subentries={}, pref_disable_new_entities=False
        )
        coord = coordinator()
        coord.config_entry.title = "Synthetic storage"
        coord.config_entry.pref_disable_new_entities = False
        coord.config_entry.subentries = {}
        coord.config_entry.disabled_by = None
        station = devices.async_get_or_create(
            config_entry_id="synthetic-storage", identifiers={(DOMAIN, "S1")}
        )
        other = devices.async_get_or_create(
            config_entry_id="synthetic-other", identifiers={(DOMAIN, "OTHER")}
        )
        entities = by_field(coord)
        original = {}
        for field in OLD_FIELDS:
            row = registry.async_get_or_create(
                "sensor",
                DOMAIN,
                entities[field].unique_id,
                config_entry=coord.config_entry,
                device_id=station.id,
                disabled_by=None,
                suggested_object_id="existing_" + field,
            )
            registry.async_update_entity(row.entity_id, name="Retained " + field)
            original[field] = row.entity_id
        for field, entity in entities.items():
            row = registry.async_get_or_create(
                "sensor",
                DOMAIN,
                entity.unique_id,
                config_entry=coord.config_entry,
                device_id=station.id,
                disabled_by=er.RegistryEntryDisabler.INTEGRATION
                if not entity.entity_registry_enabled_default
                else None,
                suggested_object_id="new_" + field,
            )
            self.assertEqual(row.device_id, station.id)
            if field in OLD_FIELDS:
                self.assertEqual(row.entity_id, original[field])
                self.assertIsNone(row.disabled_by)
                self.assertEqual(row.name, "Retained " + field)
            else:
                self.assertEqual(row.disabled_by, er.RegistryEntryDisabler.INTEGRATION)
        self.assertEqual(len(registry.entities), 7)
        self.assertEqual(devices.async_get(other.id).identifiers, {(DOMAIN, "OTHER")})
        self.assertEqual(coord.config_entry.runtime_data.client.mock_calls, [])
        await hass.async_stop()
