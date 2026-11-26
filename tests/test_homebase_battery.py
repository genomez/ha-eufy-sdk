"""HomeBase battery entities consume synthetic SDK-to-bridge scalar fixtures."""

# ruff: noqa: INP001, S101

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.eufy_sdk.manifest_reads import normalize_properties
from custom_components.eufy_sdk.sensor import (
    EufySdkPropertySensor,
)
from custom_components.eufy_sdk.sensor import (
    async_setup_entry as setup_sensors,
)

ROOT = str(Path(__file__).resolve().parents[1])


async def test_source_manifests_preserve_health_identity_and_create_duration() -> None:
    """Use the bridge-tested manifests, decoded strings, omissions and family gates."""
    cases = json.loads(
        (
            Path(__file__).parent / "fixtures" / "homebase_battery_manifest.json"
        ).read_text(encoding="utf-8")
    )["cases"]
    for case in cases:
        coordinator = Mock(
            data={"synthetic-device": {"model": case["model"], "state": case["state"]}},
            solix_devices={},
            inventory_evidence=None,
        )
        entry = Mock(
            data={"host": "synthetic"},
            runtime_data=SimpleNamespace(
                coordinator=coordinator,
                properties={"synthetic-device": normalize_properties(case["reply"])},
            ),
        )
        added = []
        await setup_sensors(HomeAssistant(ROOT), entry, added.extend)
        sensors = {entity.unique_id: entity for entity in added}
        for prop, value in (
            ("batteryHealth", case["health"]),
            ("batteryLifeEstimate", case["life"]),
        ):
            uid = f"synthetic-device_{prop}"
            if uid in sensors:
                assert sensors[uid].native_value == value
                assert sensors[uid].entity_category is EntityCategory.DIAGNOSTIC
            else:
                assert value is None
        if case["health"] is not None:
            assert "synthetic-device_batteryHealth" in sensors
        if case["life"] is not None:
            duration = sensors["synthetic-device_batteryLifeEstimate"]
            assert duration.name == "Estimated Battery Life"
            assert duration.device_class is SensorDeviceClass.DURATION
            assert duration.native_unit_of_measurement == UnitOfTime.SECONDS
            assert duration.suggested_unit_of_measurement == UnitOfTime.HOURS
            assert duration.state_class is SensorStateClass.MEASUREMENT
        if case["model"] in ("T8214", "T8217"):
            assert "synthetic-device_batteryHealth" not in sensors
            assert "synthetic-device_batteryLifeEstimate" not in sensors


async def test_health_registry_identity_is_reused() -> None:
    """A parameter-source change keeps the existing registry entry and entity id."""
    hass = HomeAssistant(str(Path(ROOT) / ".offline" / "homebase-battery-registry"))
    dr.async_setup(hass)
    await dr.async_load(hass)
    registry = er.EntityRegistry(hass)
    hass.data[er.DATA_REGISTRY] = registry
    await registry.async_load()
    existing = registry.async_get_or_create(
        "sensor",
        "eufy_sdk",
        "synthetic-device_batteryHealth",
        suggested_object_id="synthetic_station_battery_health",
    )
    sensor = EufySdkPropertySensor(
        Mock(data={"synthetic-device": {"state": {"batteryHealth": 97}}}),
        "synthetic-device",
        {"name": "batteryHealth", "type": "number", "kind": "percent", "unit": "%"},
    )
    assert (
        registry.async_get_entity_id("sensor", "eufy_sdk", sensor.unique_id)
        == existing.entity_id
    )


def test_duration_uses_live_numeric_state_and_never_defaults_missing_to_zero() -> None:
    """Read refreshed seconds directly, keeping unknown distinct from reported zero."""
    coordinator = Mock(
        data={"synthetic-device": {"state": {"batteryLifeEstimate": 86400}}}
    )
    sensor = EufySdkPropertySensor(
        coordinator,
        "synthetic-device",
        {
            "name": "batteryLifeEstimate",
            "type": "number",
            "kind": "seconds",
            "unit": "s",
        },
    )
    for state, expected in (
        ({"batteryLifeEstimate": 86400}, 86400),
        ({"batteryLifeEstimate": 43200}, 43200),
        ({"batteryLifeEstimate": 0}, 0),
        ({}, None),
        ({"batteryLifeEstimate": "86400"}, None),
    ):
        coordinator.data["synthetic-device"]["state"] = state
        assert sensor.native_value == expected
