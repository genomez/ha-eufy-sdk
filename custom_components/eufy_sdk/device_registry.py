"""Resolve device identifiers within the owning integration entry."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


@callback
def async_entry_device(
    hass: HomeAssistant, serial: str, entry_id: str | None
) -> dr.DeviceEntry | None:
    """Use the scoped lookup, or entry enumeration on older HA releases."""
    if entry_id is None:
        return None
    registry = dr.async_get(hass)
    identifier = (DOMAIN, serial)
    lookup = getattr(registry, "async_get_device_by_identifier", None)
    if lookup is not None:
        return lookup(identifier, entry_id)
    return next(
        (
            device
            for device in dr.async_entries_for_config_entry(registry, entry_id)
            if identifier in device.identifiers
        ),
        None,
    )
