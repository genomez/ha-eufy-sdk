"""Pilot diagnostics expose counts and fixed status categories only."""

from __future__ import annotations

from collections import deque
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
    failures = getattr(coordinator, "refresh_failures", None)
    slow_rpcs = getattr(client, "slow_successful_rpcs", None)
    last_complete_ms = getattr(coordinator, "last_complete_snapshot_ms", None)
    controllers = getattr(coordinator, "_light_safety_controllers", ())
    if not isinstance(controllers, (set, list, tuple)):
        controllers = ()
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
        "refresh": {
            "snapshot_fresh": getattr(coordinator, "snapshot_fresh", False) is True,
            "last_complete_snapshot_ms": (
                last_complete_ms if type(last_complete_ms) is int else None
            ),
        },
        # These rings contain only locally constructed fixed fields, never bridge
        # payloads, request arguments, device identifiers or exception messages.
        "refresh_failures": (
            [dict(row) for row in failures] if isinstance(failures, deque) else []
        ),
        "slow_successful_rpcs": (
            [dict(row) for row in slow_rpcs] if isinstance(slow_rpcs, deque) else []
        ),
        "floodlight_safety": [
            {
                "light": index,
                "status": controller.state,
                "journal": list(controller.journal),
            }
            for index, controller in enumerate(controllers, start=1)
            if controller.enabled
        ],
    }
