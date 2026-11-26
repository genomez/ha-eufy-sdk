"""Source SDK manifests omit placeholders and retire only owned registry entries."""

# ruff: noqa: INP001, S101

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.eufy_sdk.manifest_reads import normalize_properties
from custom_components.eufy_sdk.sensor import async_setup_entry as setup_sensors
from tests import test_property_entities as property_tests

Entry = property_tests.Entry
Registry = property_tests.Registry
ROOT = str(Path(__file__).resolve().parents[1])


def health_cases() -> list[dict]:
    """Read the synthetic fixture generated from SDK source and bridge metadata."""
    return json.loads(
        (
            Path(__file__).parent / "fixtures" / "doorbell_health_manifest.json"
        ).read_text(encoding="utf-8")
    )["cases"]


async def test_source_sdk_bridge_manifests_drive_actual_sensor_creation() -> None:
    """Exercise sensor setup for unsupported placeholders and supported readings."""
    for case in health_cases():
        specs = normalize_properties(case["reply"])
        coordinator = Mock(
            data={
                "synthetic-device": {
                    "model": case["model"],
                    "state": {"battery": 80, "batteryHealth": case["health"]},
                }
            },
            solix_devices={},
            inventory_evidence=None,
        )
        entry = Mock(
            data={"host": "synthetic"},
            runtime_data=SimpleNamespace(
                coordinator=coordinator, properties={"synthetic-device": specs}
            ),
        )
        added = []
        hass = HomeAssistant(ROOT)
        await setup_sensors(hass, entry, added.extend)
        ids = {entity.unique_id for entity in added}
        assert "synthetic-device_battery" in ids
        assert ("synthetic-device_batteryHealth" in ids) == (case["model"] == "T8134")
        if case["model"] == "T8134":
            sensor = next(
                entity
                for entity in added
                if entity.unique_id == "synthetic-device_batteryHealth"
            )
            assert sensor.native_value == case["health"]


class DoorbellHealthRegistryTests(unittest.TestCase):
    """Reuse the existing migration harness to preserve registry ownership rules."""

    setUp = property_tests.RegistryTests.setUp
    apply = property_tests.RegistryTests.apply

    def test_placeholder_entries_use_reversible_presentation_retirement(self) -> None:
        """Retain identity, audit markers and user disables across repeated setup."""
        for case in health_cases():
            self.hass.data = {}
            original = Entry(
                entity_id="sensor.renamed_health",
                unique_id="synthetic-device_batteryHealth",
                options={
                    "sensor": {"precision": 1},
                    "eufy_sdk": {"entity_audit_visibility_v1": {"owned": False}},
                },
            )
            user_disabled = Entry(
                entity_id="sensor.user_disabled_health",
                unique_id="other-device_batteryHealth",
                disabled_by=er.RegistryEntryDisabler.USER,
            )
            registry = Registry([original, user_disabled])
            self.entry.runtime_data.coordinator.data = {
                "synthetic-device": {"model": case["model"], "stationSn": "station"},
                "other-device": {"model": case["model"], "stationSn": "station"},
                "station": {"model": "T9000"},
            }
            specs = normalize_properties(case["reply"])
            self.entry.runtime_data.properties = {
                "synthetic-device": specs,
                "other-device": specs,
            }
            self.apply(registry)
            self.apply(registry)
            result = registry.async_get(original.entity_id)
            assert result.unique_id == original.unique_id
            assert result.options["sensor"] == original.options["sensor"]
            assert result.options["eufy_sdk"]["entity_audit_visibility_v1"] == {
                "owned": False
            }
            assert result.disabled_by == (
                er.RegistryEntryDisabler.INTEGRATION
                if case["model"] != "T8134"
                else None
            )
            assert registry.async_get(user_disabled.entity_id) == user_disabled
            if case["model"] != "T8134":
                assert result.options["eufy_sdk"]["property_presentation_v2"][
                    "owned_disabled"
                ]
