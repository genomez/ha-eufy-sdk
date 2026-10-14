"""Pilot diagnostics expose counts and fixed status categories only."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .data import EufySdkConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,  # noqa: ARG001
    entry: EufySdkConfigEntry,
) -> dict[str, Any]:
    """Keep device state and configuration in HA, outside exported diagnostics."""
    data = entry.runtime_data
    coordinator = data.coordinator
    client = data.client
    try:
        auth = await client.auth_status()
    except Exception:  # noqa: BLE001 - diagnostics report failure without raw causes
        auth = {"state": "unavailable"}
    state = auth.get("state")
    if state not in (
        "ok",
        "pending",
        "require_2fa",
        "require_captcha",
        "blocked",
        "cooldown",
        "unavailable",
    ):
        state = "unknown"
    return {
        "connection": {
            "bridge_connected": bool(client.connected),
            "last_update_success": bool(coordinator.last_update_success),
            "auth_state": state,
        },
        "summary": {
            "device_count": len(coordinator.data or {}),
            "solix_device_count": len(getattr(coordinator, "solix_devices", {}) or {}),
            "property_spec_count": sum(len(v) for v in data.properties.values()),
        },
    }
