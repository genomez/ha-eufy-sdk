"""Reversible, narrowly scoped cleanup of superseded property registry entries."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .entity import classify

if TYPE_CHECKING:
    from homeassistant.core import Event, HomeAssistant

    from .data import EufySdkConfigEntry

# Only reviewed schema corrections belong here. Missing arbitrary properties,
# absent devices and transient readings never establish that an entity is obsolete.
_CORRECTED_PROPERTIES = frozenset(
    {
        "workingMode",
        "recordDuration",
        "recordInterval",
        "recordAutoStop",
        "battery",
        "batteryHealth",
        "batteryTemperature",
        "batteryPowerStats",
        "solarIntensity",
        "solarConnected24h",
        "charging",
        "testMode",
        "sensorPirSensitivity",
        "locked",
    }
)
_PROPERTY_PLATFORMS = ("sensor", "binary_sensor", "switch", "select", "number")
_MARKER = "property_presentation_v1"


def _marker(entry: er.RegistryEntry) -> dict[str, Any]:
    value = entry.options.get(DOMAIN, {}).get(_MARKER, {})
    return value if isinstance(value, dict) else {}


def _write_marker(
    registry: er.EntityRegistry, entry: er.RegistryEntry, value: dict | None
) -> None:
    options = dict(entry.options.get(DOMAIN, {}))
    if value is None:
        options.pop(_MARKER, None)
    else:
        options[_MARKER] = value
    registry.async_update_entity_options(entry.entity_id, DOMAIN, options or None)


def _release(registry: er.EntityRegistry, entry: er.RegistryEntry) -> None:
    marker = _marker(entry)
    # Remove ownership first so our own unhide cannot be mistaken for user intent.
    _write_marker(registry, entry, None)
    if (
        marker.get("owned") is True
        and entry.hidden_by == er.RegistryEntryHider.INTEGRATION
    ):
        registry.async_update_entity(entry.entity_id, hidden_by=None)


@callback
def async_release_presentation_hides(hass: HomeAssistant, entry_id: str) -> None:
    """Release only this migration's hides before rolling back the integration."""
    registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(registry, entry_id):
        if entity.platform == DOMAIN and _marker(entity):
            _release(registry, entity)


def _presentation_decisions(entry: EufySdkConfigEntry) -> dict[tuple[str, str], bool]:
    """Scope migration to explicit metadata and reviewed capability corrections."""
    decisions: dict[tuple[str, str], bool] = {}
    for serial, specs in entry.runtime_data.properties.items():
        device = entry.runtime_data.coordinator.data.get(serial, {})
        if device.get("error") or not specs:
            continue
        by_name = {
            spec["name"]: spec for spec in specs if isinstance(spec.get("name"), str)
        }
        names = _CORRECTED_PROPERTIES | set(by_name)
        for name in names:
            spec = by_name.get(name)
            obsolete = spec is None or spec.get("unexposed") is True
            # Raw alone is not a retirement signal: a newer SDK can expose a
            # decoded recording-quality property despite its structured wire value.
            for platform in _PROPERTY_PLATFORMS:
                decisions[(platform, f"{serial}_{name}")] = obsolete
        # snoozeTime was a raw string sensor before its typed seconds projection.
        # Retire only that proven old route, keeping the number's stable identity.
        snooze = by_name.get("snoozeTime")
        if snooze is not None and classify(snooze) == "number":
            decisions[("sensor", f"{serial}_snoozeTime")] = True
        # Older camera schemas inferred a lock from display names. The dedicated
        # lock entity does not use a property unique ID and needs its own scope.
        if device.get("codec") == "camera":
            decisions[("lock", f"{serial}_lock")] = "lock" not in device.get(
                "capabilities", []
            )

    return decisions


@callback
def async_reconcile_property_entities(
    hass: HomeAssistant, entry: EufySdkConfigEntry
) -> None:
    """Hide explicit diagnostics and reviewed removed properties, retaining IDs."""
    registry = er.async_get(hass)
    decisions = _presentation_decisions(entry)

    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if entity.platform != DOMAIN:
            continue
        obsolete = decisions.get((entity.domain, entity.unique_id))
        if obsolete is None:
            continue
        marker = _marker(entity)
        if not obsolete:
            if marker:
                _release(registry, entity)
            continue
        if marker.get("opt_out") is True:
            continue
        if marker.get("owned") is True:
            if entity.hidden_by != er.RegistryEntryHider.INTEGRATION:
                _write_marker(registry, entity, {"opt_out": True})
            continue
        if entity.hidden_by is not None or entity.disabled_by is not None:
            continue
        _write_marker(registry, entity, {"owned": True})
        registry.async_update_entity(
            entity.entity_id, hidden_by=er.RegistryEntryHider.INTEGRATION
        )

    @callback
    def _respect_user_change(event: Event) -> None:
        if event.data.get("action") != "update" or "hidden_by" not in event.data.get(
            "changes", {}
        ):
            return
        entity = registry.async_get(event.data["entity_id"])
        if (
            entity is None
            or entity.config_entry_id != entry.entry_id
            or entity.platform != DOMAIN
        ):
            return
        if (
            _marker(entity).get("owned") is True
            and entity.hidden_by != er.RegistryEntryHider.INTEGRATION
        ):
            _write_marker(registry, entity, {"opt_out": True})

    entry.async_on_unload(
        hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, _respect_user_change)
    )
