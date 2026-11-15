"""Once-only, reversible visibility for reviewed T9000 controls."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.service import async_register_admin_service

from .const import DOMAIN
from .light_schedule import t9000_attached

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, ServiceCall

    from .coordinator import EufySdkDataUpdateCoordinator
    from .data import EufySdkConfigEntry

MARKER = "entity_audit_visibility_v1"
SOUND_SUPPORT_MARKER = "entity_audit_sound_support_v1"
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
# The Eufy app offers Sound Detection only on this model among T9000-attached cameras.
_SOUND_SUPPORTED_MODEL = "T8416"
# powerSource value for "External Solar Panel".
_SOLAR_PANEL = "1"
_RUNTIMES = f"{DOMAIN}_audit_visibility"


def reviewed_control(
    coordinator: EufySdkDataUpdateCoordinator, sn: str, name: str
) -> bool:
    """Limit policy to the reviewed setup and property set; arming stays visible."""
    device = coordinator.data.get(sn, {})
    return bool(
        not device.get("error")
        and (
            str(device.get("model", "")).upper().startswith("T9000")
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
    coordinator: EufySdkDataUpdateCoordinator,
    sn: str,
    name: str,
    spec: dict | None = None,
) -> bool:
    """Hide reviewed controls using app support for T9000-attached sound controls."""
    if (
        reviewed_control(coordinator, sn, name)
        and name in _UNKNOWN
        and t9000_attached(coordinator, sn)
    ):
        model = _model(coordinator, sn)
        # Stay visible until the model is known.
        return bool(model) and not model.startswith(_SOUND_SUPPORTED_MODEL)
    return reviewed_control(coordinator, sn, name) and (
        name not in _UNKNOWN
        or (device_value(coordinator, sn, name) is None and _unsupported(spec))
    )


def _unsupported(spec: dict | None) -> bool:
    """Only explicit negative support evidence settles an unread sound control."""
    return bool(
        spec and (spec.get("unsupported") is True or spec.get("supported") is False)
    )


def solar_hidden_default(
    coordinator: EufySdkDataUpdateCoordinator, sn: str, specs: list[dict]
) -> bool | None:
    """Hide solar charging unless the device runs on a solar panel; None until known.

    Battery status 2111 only shows solar values while a panel is charging, so the
    sensor says nothing useful on a HomeBase or a camera powered another way.
    """
    if not any(spec.get("name") == "powerSource" for spec in specs):
        return True
    # A HomeBase offers a power-source control it never reports, and has no panel.
    if str(coordinator.data.get(sn, {}).get("model", "")).upper().startswith("T9000"):
        return True
    value = device_value(coordinator, sn, "powerSource")
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return not (text == _SOLAR_PANEL or "solar" in text.lower())


def _model(coordinator: EufySdkDataUpdateCoordinator, sn: str) -> str:
    """The device's model, upper-cased; empty while not yet known."""
    return str(coordinator.data.get(sn, {}).get("model") or "").strip().upper()


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
    sound_decisions = set()
    for sn, specs in entry.runtime_data.properties.items():
        for spec in specs:
            name = spec["name"]
            if reviewed_control(coordinator, sn, name):
                sound_support = name in _UNKNOWN and t9000_attached(coordinator, sn)
                if sound_support and not _model(coordinator, sn):
                    # Decide once the model is known; the marker makes it final.
                    continue
                if (
                    not sound_support
                    and name in _UNKNOWN
                    and device_value(coordinator, sn, name) is None
                    and not _unsupported(spec)
                ):
                    continue
                # Keep meaningful read entities visible.
                for domain in ("select", "switch", "number"):
                    key = (domain, f"{sn}_{name}")
                    decisions[key] = hidden_default(coordinator, sn, name, spec)
                    if sound_support:
                        sound_decisions.add(key)
        if any(spec["name"] == "solarPanelConnected" for spec in specs):
            hide = solar_hidden_default(coordinator, sn, specs)
            if hide is not None:
                decisions[("binary_sensor", f"{sn}_solarPanelConnected")] = hide
        if reviewed_control(coordinator, sn, "refresh_last_event"):
            decisions[("button", f"{sn}_refresh_last_event")] = True
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        key = (entity.domain, entity.unique_id)
        if entity.platform != DOMAIN or key not in decisions:
            continue
        options = dict(entity.options.get(DOMAIN, {}))
        marker_key = SOUND_SUPPORT_MARKER if key in sound_decisions else MARKER
        if marker_key in options:
            marker = options[marker_key]
            if (
                marker_key == MARKER
                and entity.unique_id.rsplit("_", 1)[-1] in _UNKNOWN
                and decisions[key] is False
                and isinstance(marker, dict)
                and marker.get("owned") is True
            ):
                if entity.hidden_by == er.RegistryEntryHider.INTEGRATION:
                    registry.async_update_entity(entity.entity_id, hidden_by=None)
                options[MARKER] = {"version": 1, "owned": False}
                registry.async_update_entity_options(entity.entity_id, DOMAIN, options)
            continue
        owned = decisions[key] and entity.hidden_by in (
            None,
            er.RegistryEntryHider.INTEGRATION,
        )
        previous = options.get(MARKER, {})
        if marker_key == SOUND_SUPPORT_MARKER and isinstance(previous, dict):
            # A released hide or a user unhide of an owned v1 hide stays released.
            if previous.get("released") or (
                previous.get("owned") is True
                and entity.hidden_by != er.RegistryEntryHider.INTEGRATION
            ):
                owned = False
            elif (
                not decisions[key]
                and previous.get("owned") is True
                and entity.hidden_by == er.RegistryEntryHider.INTEGRATION
            ):
                registry.async_update_entity(entity.entity_id, hidden_by=None)
                options[MARKER] = {"version": 1, "owned": False}
        options[marker_key] = {"version": 1, "owned": owned}
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
        if entity.platform != DOMAIN:
            continue
        options = dict(entity.options.get(DOMAIN, {}))
        owned_markers = [
            key
            for key in (MARKER, SOUND_SUPPORT_MARKER)
            if isinstance(options.get(key), dict) and options[key].get("owned") is True
        ]
        if owned_markers:
            if entity.hidden_by == er.RegistryEntryHider.INTEGRATION:
                registry.async_update_entity(entity.entity_id, hidden_by=None)
            for key in owned_markers:
                options[key] = {"version": 1, "owned": False, "released": True}
            registry.async_update_entity_options(entity.entity_id, DOMAIN, options)


def register_audit_release_service(
    hass: HomeAssistant, entry: EufySdkConfigEntry
) -> None:
    """Register an admin-only release for loaded entries, removing it on last unload."""
    entries = hass.data.setdefault(_RUNTIMES, {})
    entries[entry.entry_id] = entry

    async def handle(call: ServiceCall) -> None:
        target = entries.get(call.data["entry_id"])
        if target is None:
            message = "Audit visibility entry is not loaded"
            raise HomeAssistantError(message)
        async_migrate_audit_visibility(hass, target)
        async_release_audit_hides(hass, target.entry_id)

    if len(entries) == 1:
        async_register_admin_service(
            hass,
            DOMAIN,
            "release_audit_hides",
            handle,
            schema=vol.Schema({vol.Required("entry_id"): cv.string}),
        )

    def unload() -> None:
        entries.pop(entry.entry_id, None)
        if not entries:
            hass.services.async_remove(DOMAIN, "release_audit_hides")
            hass.data.pop(_RUNTIMES, None)

    entry.async_on_unload(unload)
