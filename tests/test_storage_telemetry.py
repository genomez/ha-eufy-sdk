"""S1 cached storage must stay isolated, valid and fresh without wire requests."""

import copy
import logging
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import EntityPlatform

from custom_components.eufy_sdk.const import DOMAIN
from custom_components.eufy_sdk.sensor import async_setup_entry
from custom_components.eufy_sdk.storage_telemetry import (
    FRESHNESS_MS,
    UNIT_PROFILE,
    storage_observed_at,
    storage_reading,
    storage_sensors,
)

NOW = 1_790_999_000_000
MODULE = "custom_components.eufy_sdk.storage_telemetry"


def device(status="current", observed=NOW - 1000):
    return {
        "sn": "S1",
        "model": "T9000",
        "firmwareVersion": "4.4.0.4",
        "codec": "station",
        "stationSn": None,
        "name": "Synthetic station",
        "capabilities": [],
        "storageTelemetry": {
            "schemaVersion": 1,
            "supported": True,
            "enabled": status != "disabled",
            "status": status,
            "observedAtMs": observed,
            "unitProfile": UNIT_PROFILE,
            "correlation": "time-associated",
            "exactlyCorrelated": False,
            "values": {
                "total_bytes": 1000,
                "used_bytes": 750,
                "available_bytes": 250,
                "used_percent": 75,
                "video_bytes": 700,
                "system_bytes": 50,
            },
        },
    }


def coordinator(devices=None):
    coord = Mock(data={"S1": device()} if devices is None else devices)
    coord.last_update_success = True
    coord.solix_devices = {}
    coord.config_entry.entry_id = "synthetic-storage"
    coord.config_entry.runtime_data = SimpleNamespace(client=Mock())
    coord.async_add_listener.return_value = Mock()
    return coord


class StorageValidationTests(unittest.TestCase):
    def test_valid_zero_and_consistent_percent(self):
        row = device()
        values = row["storageTelemetry"]["values"]
        values.update(
            used_bytes=0,
            available_bytes=1000,
            used_percent=0,
            video_bytes=0,
            system_bytes=0,
        )
        self.assertEqual(storage_reading(row, NOW)[0], "current")
        values.update(
            used_bytes=1000,
            available_bytes=0,
            used_percent=100,
            video_bytes=900,
            system_bytes=100,
        )
        self.assertEqual(storage_reading(row, NOW)[0], "current")

    def test_invalid_numeric_values_and_inconsistent_projection(self):
        changes = [
            (field, bad)
            for field in (
                "total_bytes",
                "used_bytes",
                "available_bytes",
                "video_bytes",
                "system_bytes",
            )
            for bad in (
                True,
                None,
                "100",
                {},
                [],
                -1,
                0.5,
                float("nan"),
                float("inf"),
                1 << 53,
            )
        ] + [
            ("used_percent", bad)
            for bad in (True, None, "75", {}, float("nan"), float("inf"), -1, 101, 74)
        ]
        changes += [
            ("total_bytes", 0),
            ("used_bytes", 1001),
            ("available_bytes", 249),
            ("video_bytes", 701),
        ]
        for field, value in changes:
            with self.subTest(field=field, value=value):
                row = device()
                row["storageTelemetry"]["values"][field] = value
                self.assertEqual(storage_reading(row, NOW), ("unavailable", None))

    def test_schema_identity_units_and_correlation_are_required(self):
        for key, value in (
            ("schemaVersion", True),
            ("schemaVersion", 2),
            ("supported", False),
            ("enabled", 1),
            ("exactlyCorrelated", True),
            ("exactlyCorrelated", None),
            ("status", "healthy"),
            ("unitProfile", "MB"),
            ("correlation", "exact"),
            ("values", None),
            ("values", []),
        ):
            row = device()
            row["storageTelemetry"][key] = value
            self.assertEqual(storage_reading(row, NOW), ("unavailable", None))
        row = device()
        del row["storageTelemetry"]["values"]["system_bytes"]
        self.assertEqual(storage_reading(row, NOW), ("unavailable", None))

    def test_freshness_boundary_future_and_invalid_timestamps(self):
        for observed in (
            None,
            True,
            "1",
            0,
            -1,
            NOW + 1,
            NOW - FRESHNESS_MS,
            NOW - FRESHNESS_MS - 1,
        ):
            self.assertEqual(
                storage_reading(device(observed=observed), NOW), ("unavailable", None)
            )
        self.assertEqual(
            storage_reading(device(observed=NOW - FRESHNESS_MS + 1), NOW)[0], "current"
        )

    def test_disabled_neverseen_stale_and_unavailable(self):
        for status in ("disabled", "unknown"):
            row = device(status, None)
            row["storageTelemetry"].update(
                unitProfile=None, correlation=None, values=None
            )
            self.assertEqual(storage_reading(row, NOW), ("unknown", None))
        for status in ("stale", "unavailable"):
            self.assertEqual(
                storage_reading(device(status), NOW), ("unavailable", None)
            )

    def test_historical_timestamp_requires_qualified_provenance_and_current_owner(self):
        row = device("stale", NOW - FRESHNESS_MS)
        row["storageTelemetry"]["values"] = None
        self.assertEqual(storage_observed_at(row, NOW), NOW - FRESHNESS_MS)
        for key, value in (
            ("enabled", False),
            ("status", "disabled"),
            ("status", "unknown"),
            ("status", "unavailable"),
            ("unitProfile", None),
            ("correlation", None),
            ("exactlyCorrelated", True),
            ("observedAtMs", NOW + 1),
            ("observedAtMs", True),
            ("observedAtMs", None),
        ):
            invalid = copy.deepcopy(row)
            invalid["storageTelemetry"][key] = value
            self.assertIsNone(storage_observed_at(invalid, NOW))
        coord = coordinator({"S1": row})
        entity = storage_sensors(coord)[-1]
        coord.last_update_success = False
        with patch(MODULE + "._now_ms", return_value=NOW):
            self.assertFalse(entity.available)
            self.assertIsNone(entity.native_value)
            self.assertIsNone(entity.extra_state_attributes["observed_at_ms"])

    def test_only_actual_supported_s1_station_registers(self):
        devices = {"S1": device()}
        for key, value in (
            ("model", "T8030"),
            ("model", "T8010"),
            ("model", "T9000?"),
            ("model", "T9000X"),
            ("model", "unknown"),
            ("firmwareVersion", "4.4.0.5"),
            ("firmwareVersion", None),
            ("codec", "camera"),
            ("stationSn", "parent"),
            ("storageTelemetry", None),
        ):
            row = device()
            row[key] = value
            devices[f"{key}-{value}"] = row
        coord = coordinator(devices)
        entities = storage_sensors(coord)
        self.assertEqual(len(entities), 7)
        self.assertEqual(len({entity.unique_id for entity in entities}), 7)
        self.assertTrue(all(entity._sn == "S1" for entity in entities))
        self.assertTrue(
            all(entity.entity_registry_enabled_default is False for entity in entities)
        )
        self.assertTrue(
            all(
                entity.entity_category == EntityCategory.DIAGNOSTIC
                for entity in entities
            )
        )
        self.assertTrue(
            all(
                entity.device_info["identifiers"] == {(DOMAIN, "S1")}
                for entity in entities
            )
        )
        self.assertEqual(
            [entity.state_class for entity in entities[:4]],
            [SensorStateClass.MEASUREMENT] * 4,
        )
        self.assertEqual(entities[-1].device_class, SensorDeviceClass.TIMESTAMP)
        self.assertIsNone(entities[-1].state_class)
        self.assertEqual(
            [entity.native_unit_of_measurement for entity in entities[:4]],
            ["B", "B", "B", "%"],
        )
        coord.data["S1"] = device("disabled", None)
        self.assertEqual(
            [entity.unique_id for entity in storage_sensors(coord)],
            [entity.unique_id for entity in entities],
        )

    def test_getters_do_not_request_storage_and_snapshot_does_not_refresh_time(self):
        coord = coordinator()
        original = copy.deepcopy(coord.data)
        with patch(MODULE + "._now_ms", return_value=NOW):
            entities = storage_sensors(coord)
            self.assertEqual(
                [entity.native_value for entity in entities[:4]], [1000, 750, 250, 75]
            )
            self.assertEqual(
                entities[-1].native_value,
                datetime.fromtimestamp((NOW - 1000) / 1000, timezone.utc),
            )
            self.assertTrue(all(entity.available for entity in entities))
            self.assertTrue(
                all(
                    entity.extra_state_attributes["observed_at_ms"] == NOW - 1000
                    for entity in entities
                )
            )
        self.assertEqual(coord.data, original)
        self.assertEqual(coord.config_entry.runtime_data.client.mock_calls, [])
        coord.last_update_success = False
        self.assertFalse(entities[0].available)
        coord.last_update_success = True
        coord.data = {}
        self.assertFalse(entities[0].available)


class StorageLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_platform_registration_keeps_controls_and_makes_no_client_calls(self):
        coord = coordinator()
        entry = SimpleNamespace(
            runtime_data=SimpleNamespace(
                coordinator=coord, properties={}, client=Mock()
            ),
            data={"host": "offline"},
            async_on_unload=Mock(),
        )
        added = Mock()
        await async_setup_entry(Mock(), entry, added)
        entities = added.call_args.args[0]
        self.assertEqual(
            len([entity for entity in entities if "_storage_" in entity.unique_id]), 7
        )
        self.assertEqual(
            len([entity for entity in entities if entity.unique_id.endswith("_info")]),
            1,
        )
        self.assertEqual(entry.runtime_data.client.mock_calls, [])

    async def test_expiry_publishes_unavailable_without_coordinator_updates_and_cleans_up(
        self,
    ):
        hass = HomeAssistant("/tmp/offline-storage-expiry")
        devices = dr.DeviceRegistry(hass)
        hass.data[dr.DATA_REGISTRY] = devices
        await devices.async_load(load_empty=True)
        registry = er.EntityRegistry(hass)
        hass.data[er.DATA_REGISTRY] = registry
        await registry.async_load()
        coord = coordinator()
        entity = storage_sensors(coord)[0]
        platform = EntityPlatform(
            hass=hass,
            logger=logging.getLogger(__name__),
            domain="sensor",
            platform_name=DOMAIN,
            platform=None,
            scan_interval=timedelta(seconds=30),
            entity_namespace=None,
        )
        entity.add_to_platform_start(hass, platform, None)
        entity.entity_id = "sensor.synthetic_storage_total"
        cancel = Mock()
        with (
            patch(MODULE + "._now_ms", return_value=NOW),
            patch(MODULE + ".async_call_later", return_value=cancel) as later,
        ):
            await entity.add_to_platform_finish()
            later.assert_called_once()
            self.assertEqual(later.call_args.args[1], (FRESHNESS_MS - 1000) / 1000)
            entity.async_write_ha_state()
            self.assertEqual(hass.states.get(entity.entity_id).state, "1000")
        with patch(MODULE + "._now_ms", return_value=NOW + FRESHNESS_MS):
            entity._expire(None)
            self.assertEqual(hass.states.get(entity.entity_id).state, "unavailable")
            self.assertIsNone(entity._expiry_cancel)
        self.assertEqual(coord.config_entry.runtime_data.client.mock_calls, [])
        with (
            patch(MODULE + "._now_ms", return_value=NOW),
            patch(MODULE + ".async_call_later", return_value=cancel),
        ):
            entity._schedule_expiry()
            await entity.async_will_remove_from_hass()
        cancel.assert_called_once()
        self.assertIsNone(entity._expiry_cancel)
        await hass.async_stop()

    async def test_native_stale_values_expire_but_original_timestamp_stays_visible(
        self,
    ):
        hass = HomeAssistant("/tmp/offline-storage-historical-time")
        devices = dr.DeviceRegistry(hass)
        hass.data[dr.DATA_REGISTRY] = devices
        await devices.async_load(load_empty=True)
        registry = er.EntityRegistry(hass)
        hass.data[er.DATA_REGISTRY] = registry
        await registry.async_load()
        coord = coordinator()
        entities = storage_sensors(coord)
        numeric, timestamp = entities[0], entities[-1]
        platform = EntityPlatform(
            hass=hass,
            logger=logging.getLogger(__name__),
            domain="sensor",
            platform_name=DOMAIN,
            platform=None,
            scan_interval=timedelta(seconds=30),
            entity_namespace=None,
        )
        original = NOW - 1000
        with (
            patch(MODULE + "._now_ms", return_value=NOW),
            patch(MODULE + ".async_call_later", return_value=Mock()),
        ):
            for entity, entity_id in (
                (numeric, "sensor.historical_total"),
                (timestamp, "sensor.historical_observation"),
            ):
                entity.add_to_platform_start(hass, platform, None)
                entity.entity_id = entity_id
                await entity.add_to_platform_finish()
            original_state = hass.states.get(timestamp.entity_id).state
            self.assertEqual(hass.states.get(numeric.entity_id).state, "1000")
        with patch(MODULE + "._now_ms", return_value=NOW + FRESHNESS_MS):
            numeric._expire(None)
            timestamp._expire(None)
            self.assertEqual(hass.states.get(numeric.entity_id).state, "unavailable")
            self.assertEqual(hass.states.get(timestamp.entity_id).state, original_state)
            self.assertIsNone(numeric.native_value)
            self.assertEqual(numeric.extra_state_attributes["observed_at_ms"], original)
            self.assertEqual(
                timestamp.extra_state_attributes["storage_status"], "stale"
            )
            coord.data["S1"]["storageTelemetry"].update(status="stale", values=None)
            for entity in (numeric, timestamp):
                entity._handle_coordinator_update()
            self.assertEqual(hass.states.get(numeric.entity_id).state, "unavailable")
            self.assertEqual(hass.states.get(timestamp.entity_id).state, original_state)
            self.assertEqual(
                timestamp.extra_state_attributes["observed_at_ms"], original
            )
        self.assertEqual(coord.config_entry.runtime_data.client.mock_calls, [])
        for entity in (numeric, timestamp):
            await entity.async_will_remove_from_hass()
        await hass.async_stop()

    async def test_same_value_new_observation_reschedules_and_preserves_timestamp(self):
        coord = coordinator()
        entity = storage_sensors(coord)[-1]
        entity.hass = Mock()
        entity.async_write_ha_state = Mock()
        first_cancel, second_cancel = Mock(), Mock()
        with (
            patch(MODULE + "._now_ms", return_value=NOW),
            patch(
                MODULE + ".async_call_later", side_effect=[first_cancel, second_cancel]
            ) as later,
        ):
            entity._schedule_expiry()
            prior = entity.native_value
            coord.data["S1"]["storageTelemetry"]["observedAtMs"] = NOW
            entity._handle_coordinator_update()
            self.assertNotEqual(entity.native_value, prior)
            self.assertEqual(
                entity.native_value, datetime.fromtimestamp(NOW / 1000, timezone.utc)
            )
            self.assertEqual(later.call_args.args[1], FRESHNESS_MS / 1000)
            first_cancel.assert_called_once()
            entity.async_write_ha_state.assert_called_once()
        await entity.async_will_remove_from_hass()
        second_cancel.assert_called_once()

    async def test_native_registry_preserves_user_enable_and_other_device(self):
        hass = HomeAssistant("/tmp/offline-storage-registry")
        devices = dr.DeviceRegistry(hass)
        hass.data[dr.DATA_REGISTRY] = devices
        await devices.async_load(load_empty=True)
        registry = er.EntityRegistry(hass)
        hass.data[er.DATA_REGISTRY] = registry
        await registry.async_load()
        hass.config_entries = Mock()
        hass.config_entries.async_get_entry.return_value = Mock(
            disabled_by=None, subentries={}, pref_disable_new_entities=False
        )
        station = devices.async_get_or_create(
            config_entry_id="synthetic-storage", identifiers={(DOMAIN, "S1")}
        )
        other = devices.async_get_or_create(
            config_entry_id="synthetic-other", identifiers={(DOMAIN, "OTHER")}
        )
        coord = coordinator()
        entity = storage_sensors(coord)[0]
        coord.config_entry.title = "Synthetic storage"
        coord.config_entry.pref_disable_new_entities = False
        coord.config_entry.subentries = {}
        coord.config_entry.disabled_by = None
        row = registry.async_get_or_create(
            "sensor",
            DOMAIN,
            entity.unique_id,
            config_entry=coord.config_entry,
            device_id=station.id,
            disabled_by=er.RegistryEntryDisabler.INTEGRATION,
            suggested_object_id="synthetic_storage_total",
        )
        self.assertEqual(row.disabled_by, er.RegistryEntryDisabler.INTEGRATION)
        registry.async_update_entity(row.entity_id, disabled_by=None)
        again = registry.async_get_or_create(
            "sensor",
            DOMAIN,
            entity.unique_id,
            config_entry=coord.config_entry,
            device_id=station.id,
            disabled_by=er.RegistryEntryDisabler.INTEGRATION,
            suggested_object_id="synthetic_storage_total",
        )
        self.assertEqual(row.entity_id, again.entity_id)
        self.assertIsNone(again.disabled_by)
        self.assertEqual(again.device_id, station.id)
        self.assertEqual(len(registry.entities), 1)
        self.assertEqual(devices.async_get(other.id).identifiers, {(DOMAIN, "OTHER")})
        await hass.async_stop()
