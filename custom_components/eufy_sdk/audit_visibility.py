"""Once-only, reversible visibility for reviewed T9000 controls."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .light_schedule import t9000_attached

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .coordinator import EufySdkDataUpdateCoordinator
    from .data import EufySdkConfigEntry

MARKER = "entity_audit_visibility_v1"
_ALWAYS = frozenset(
    {
        "recordingMode",
        "powerSource",
        "hubAlarmTone",
        "motionLightMode",
        "soundDetectionType",
    }
)
_UNKNOWN = frozenset({"soundDetection", "soundDetectionSensitivity"})


def reviewed_control(
    coordinator: EufySdkDataUpdateCoordinator, sn: str, name: str
) -> bool:
    """Limit policy to the reviewed setup and property set; arming stays visible."""
    device = coordinator.data.get(sn, {})
    return bool(
        not device.get("error")
        and (
            str(device.get("model", "")).upper() == "T9000"
            or t9000_attached(coordinator, sn)
        )
        and (
            name in _ALWAYS | _UNKNOWN | {"refresh_last_event"}
            or (
                name == "motionLightDuration"
                and str(device.get("model", "")).upper() in {"T8425", "T8426"}
            )
        )
    )


def hidden_default(
    coordinator: EufySdkDataUpdateCoordinator, sn: str, name: str
) -> bool:
    """Hide reviewed controls, retaining reported sound controls."""
    return reviewed_control(coordinator, sn, name) and (
        name not in _UNKNOWN or device_value(coordinator, sn, name) is None
    )


def device_value(coordinator: EufySdkDataUpdateCoordinator, sn: str, name: str) -> Any:
    """Read the already normalized cached value without device I/O."""
    return (coordinator.data.get(sn, {}).get("state") or {}).get(name)


@callback
def async_migrate_audit_visibility(
    hass: HomeAssistant, entry: EufySdkConfigEntry
) -> None:
    """Persist one decision per identity, including visible sound controls."""
    registry = er.async_get(hass)
    coordinator = entry.runtime_data.coordinator
    decisions = {}
    for sn, specs in entry.runtime_data.properties.items():
        for spec in specs:
            name = spec["name"]
            if reviewed_control(coordinator, sn, name):
                # Keep meaningful read entities visible.
                for domain in ("select", "switch", "number"):
                    decisions[(domain, f"{sn}_{name}")] = hidden_default(
                        coordinator, sn, name
                    )
        if reviewed_control(coordinator, sn, "refresh_last_event"):
            decisions[("button", f"{sn}_refresh_last_event")] = True
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        key = (entity.domain, entity.unique_id)
        if entity.platform != DOMAIN or key not in decisions:
            continue
        options = dict(entity.options.get(DOMAIN, {}))
        if MARKER in options:
            continue
        owned = decisions[key] and entity.hidden_by is None
        options[MARKER] = {"version": 1, "owned": owned}
        registry.async_update_entity_options(entity.entity_id, DOMAIN, options)
        if owned:
            registry.async_update_entity(
                entity.entity_id, hidden_by=er.RegistryEntryHider.INTEGRATION
            )


@callback
def async_release_audit_hides(hass: HomeAssistant, entry_id: str) -> None:
    """Undo only owned integration hides; retain the marker to prevent reapplication."""
    registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(registry, entry_id):
        marker = entity.options.get(DOMAIN, {}).get(MARKER, {})
        if (
            entity.platform == DOMAIN
            and isinstance(marker, dict)
            and marker.get("owned") is True
        ):
            if entity.hidden_by == er.RegistryEntryHider.INTEGRATION:
                registry.async_update_entity(entity.entity_id, hidden_by=None)
            options = dict(entity.options.get(DOMAIN, {}))
            options[MARKER] = {"version": 1, "owned": False, "released": True}
            registry.async_update_entity_options(entity.entity_id, DOMAIN, options)
