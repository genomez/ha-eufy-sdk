"""Structured schedules with private config fields excluded from presentation."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, ServiceCall

    from .coordinator import EufySdkDataUpdateCoordinator
    from .data import EufySdkConfigEntry

_PRIVATE = re.compile(
    r"(?:latitude|longitude|longtitude|location|account|serial|macaddress|ipaddress)"
    r"|^(?:lat|lon|lng|sn|ip|mac)$",
    re.IGNORECASE,
)
_RUNTIMES = f"{DOMAIN}_light_schedules"
_MAX_BYTES = 65536


def schedule_entries(value: Any) -> list | None:
    """Decode only the entries, recursively omitting private fields."""
    try:
        entries = json.loads(value) if isinstance(value, str) else value
        if not isinstance(entries, list):
            return None

        def clean(item: Any) -> Any:
            if isinstance(item, dict):
                return {
                    key: clean(child)
                    for key, child in item.items()
                    if not _PRIVATE.search(key)
                }
            if isinstance(item, list):
                return [clean(child) for child in item]
            return item

        return clean(entries)
    except (TypeError, ValueError, RecursionError):
        return None


def validate_entries(value: Any) -> list:
    """Require a bounded JSON list without inventing a vendor entry schema."""
    message = "Schedule entries must be a bounded JSON list"
    if not isinstance(value, list):
        raise vol.Invalid(message)
    try:
        encoded = json.dumps(value, allow_nan=False)
        result = json.loads(encoded)
    except (TypeError, ValueError, RecursionError):
        raise vol.Invalid(message) from None
    if len(encoded.encode()) > _MAX_BYTES:
        raise vol.Invalid(message)
    return result


def t9000_attached(coordinator: EufySdkDataUpdateCoordinator, sn: str) -> bool | None:
    """Use parent roster metadata; return None when the parent row is unknown."""
    dev = coordinator.data.get(sn, {})
    parent = dev.get("stationSn")
    if not parent or parent == sn:
        return False
    station = coordinator.data.get(parent, {})
    if station.get("error") or not station.get("model"):
        return None
    return str(station["model"]).upper().startswith("T9000")


def register_schedule_service(hass: HomeAssistant, entry: EufySdkConfigEntry) -> None:
    """Register one service across entries, releasing it when the last unloads."""
    # The API imports the schedule validator; defer this import until registration.
    from .api import EufySdkApiClientError  # noqa: PLC0415

    runtimes = hass.data.setdefault(_RUNTIMES, {})
    runtimes[entry.entry_id] = entry.runtime_data

    async def handle(call: ServiceCall) -> None:
        device = dr.async_get(hass).async_get(call.data["device_id"])
        matches = []
        if device is not None:
            for runtime in runtimes.values():
                for domain, sn in device.identifiers:
                    if domain == DOMAIN and t9000_attached(runtime.coordinator, sn):
                        specs = runtime.properties.get(sn, [])
                        if any(
                            spec.get("name") == "lightSchedule"
                            and spec.get("writable") is True
                            for spec in specs
                        ):
                            matches.append((runtime, sn))
        if len(matches) != 1:
            message = "Writable light schedule device unavailable"
            raise HomeAssistantError(message)
        runtime, sn = matches[0]
        try:
            await runtime.client.set_light_schedule(sn, call.data["entries"])
        except EufySdkApiClientError:
            message = "Light schedule update could not be confirmed"
            raise HomeAssistantError(message) from None
        await runtime.coordinator.async_request_refresh()

    if len(runtimes) == 1:
        hass.services.async_register(
            DOMAIN,
            "set_light_schedule",
            handle,
            schema=vol.Schema(
                {
                    vol.Required("device_id"): cv.string,
                    vol.Required("entries"): validate_entries,
                }
            ),
        )

    def unload() -> None:
        runtimes.pop(entry.entry_id, None)
        if not runtimes:
            hass.services.async_remove(DOMAIN, "set_light_schedule")
            hass.data.pop(_RUNTIMES, None)

    entry.async_on_unload(unload)
