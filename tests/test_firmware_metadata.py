"""Firmware presentation uses ordinary snapshots, never device commands."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.template import Template

from custom_components.eufy_sdk.const import DOMAIN
from custom_components.eufy_sdk.entity import EufySdkDeviceEntity, firmware_version
from custom_components.eufy_sdk.manifest_reads import normalize_snapshot
from custom_components.eufy_sdk.sensor import EufySdkInfoSensor


def coordinator(version=None):
    coord = Mock(
        data={
            "synthetic": {
                "codec": "station",
                "model": "T9000",
                "capabilities": [],
                "firmwareVersion": version,
            }
        }
    )
    coord.last_update_success = True
    coord.config_entry.entry_id = "synthetic-entry"
    return coord


class FirmwareMetadataTests(unittest.TestCase):
    def test_validated_info_only_and_initial_device_info(self):
        for invalid in (
            None,
            404,
            True,
            {},
            [],
            "",
            "unknown",
            "unavailable",
            "https://host/4",
            "4\nsecret",
            "1" * 65,
        ):
            self.assertIsNone(
                firmware_version(
                    {"firmwareVersion": invalid, "state": {"softwareVersion": "9.9"}}
                )
            )
            self.assertNotIn(
                "sw_version",
                EufySdkDeviceEntity(coordinator(invalid), "synthetic").device_info,
            )
        self.assertEqual(
            EufySdkDeviceEntity(coordinator(" 4.4.0.4 "), "synthetic").device_info[
                "sw_version"
            ],
            "4.4.0.4",
        )

    def test_snapshot_normalization_preserves_metadata_without_touching_controls(self):
        data = {
            "firmwareVersion": "4.4.0.4",
            "state": {"light": False},
            "codec": "station",
        }
        result = normalize_snapshot(data)
        self.assertEqual(result["firmwareVersion"], "4.4.0.4")
        self.assertEqual(result["state"], data["state"])

    def test_changed_report_updates_only_existing_sdk_device_and_then_publishes(self):
        coord = coordinator("4.4.0.4")
        entity = EufySdkInfoSensor(coord, "synthetic")
        entity.hass = Mock()
        entity.async_write_ha_state = Mock()
        registry = Mock()
        device = SimpleNamespace(
            id="sdk-device", sw_version="4.2.4.2", identifiers={(DOMAIN, "synthetic")}
        )
        with (
            patch(
                "custom_components.eufy_sdk.sensor.dr.async_get", return_value=registry
            ),
            patch(
                "custom_components.eufy_sdk.sensor.dr.async_entries_for_config_entry",
                return_value=[device],
            ) as entries,
        ):
            entity._handle_coordinator_update()
        entries.assert_called_once_with(registry, "synthetic-entry")
        registry.async_update_device.assert_called_once_with(
            "sdk-device", sw_version="4.4.0.4"
        )
        entity.async_write_ha_state.assert_called_once()
        self.assertEqual(entity.extra_state_attributes["firmware_version"], "4.4.0.4")
        coord.config_entry.runtime_data.client.assert_not_called()

    def test_same_missing_invalid_or_disconnected_metadata_does_not_write_registry(
        self,
    ):
        for version, connected, existing in (
            ("4.4.0.4", True, "4.4.0.4"),
            (None, True, "4.2.4.2"),
            ({}, True, "4.2.4.2"),
            ("4.4.0.4", False, "4.2.4.2"),
        ):
            coord = coordinator(version)
            coord.last_update_success = connected
            entity = EufySdkInfoSensor(coord, "synthetic")
            entity.hass = Mock()
            entity.async_write_ha_state = Mock()
            registry = Mock()
            device = SimpleNamespace(
                id="sdk-device",
                sw_version=existing,
                identifiers={(DOMAIN, "synthetic")},
            )
            with (
                patch(
                    "custom_components.eufy_sdk.sensor.dr.async_get",
                    return_value=registry,
                ),
                patch(
                    "custom_components.eufy_sdk.sensor.dr.async_entries_for_config_entry",
                    return_value=[device],
                ),
            ):
                entity._handle_coordinator_update()
            registry.async_update_device.assert_not_called()
            entity.async_write_ha_state.assert_called_once()


class NativeFirmwareTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_registry_updates_without_restart_and_no_legacy_write(self):
        hass = HomeAssistant("/tmp/offline-firmware-test")
        registry = dr.DeviceRegistry(hass)
        hass.data[dr.DATA_REGISTRY] = registry
        await registry.async_load(load_empty=True)
        hass.config_entries = Mock()
        hass.config_entries.async_get_entry.return_value = Mock(
            disabled_by=None, subentries={}, pref_disable_new_entities=False
        )
        sdk = registry.async_get_or_create(
            config_entry_id="synthetic-entry",
            identifiers={(DOMAIN, "synthetic")},
            sw_version="4.2.4.2",
        )
        legacy = registry.async_get_or_create(
            config_entry_id="synthetic-legacy",
            identifiers={("eufy_security", "synthetic")},
            sw_version="4.2.4.2",
        )
        other_entry = registry.async_get_or_create(
            config_entry_id="synthetic-other-entry",
            identifiers={(DOMAIN, "synthetic")},
            sw_version="1.1.1.1",
        )
        coord = coordinator("4.4.0.4")
        entity = EufySdkInfoSensor(coord, "synthetic")
        entity.hass = hass
        entity.async_write_ha_state = Mock()
        entity._handle_coordinator_update()
        self.assertEqual(registry.async_get(sdk.id).sw_version, "4.4.0.4")
        self.assertEqual(registry.async_get(legacy.id).sw_version, "4.2.4.2")
        self.assertEqual(registry.async_get(other_entry.id).sw_version, "1.1.1.1")
        coord.data["synthetic"]["firmwareVersion"] = None
        entity._handle_coordinator_update()
        self.assertIsNone(entity.extra_state_attributes["firmware_version"])
        self.assertEqual(registry.async_get(sdk.id).sw_version, "4.4.0.4")
        await hass.async_stop()

    async def test_display_uses_current_info_and_cannot_reuse_legacy_or_self_state(
        self,
    ):
        hass = HomeAssistant("/tmp/offline-firmware-template")
        state = Template(
            "{{ state_attr('sensor.homebases1_info', 'firmware_version') or 'unknown' }}",
            hass,
        )
        available = Template("{{ has_value('sensor.homebases1_info') }}", hass)
        hass.states.async_set("sensor.homebase_s1_firmware", "4.2.4.2")
        self.assertEqual(state.async_render(), "unknown")
        self.assertFalse(available.async_render())
        hass.states.async_set(
            "sensor.homebases1_info", "station", {"firmware_version": "4.4.0.4"}
        )
        self.assertEqual(state.async_render(), "4.4.0.4")
        self.assertTrue(available.async_render())
        hass.states.async_set(
            "sensor.homebases1_info", "station", {"firmware_version": None}
        )
        self.assertEqual(state.async_render(), "unknown")
        hass.states.async_set(
            "sensor.homebases1_info", "unavailable", {"firmware_version": "4.4.0.4"}
        )
        self.assertFalse(available.async_render())
        await hass.async_stop()
