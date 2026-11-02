"""Publish already received firmware metadata independently of enabled entities."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from homeassistant.helpers import device_registry as dr

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

FIRMWARE_VERSION_MAX_LENGTH = 64


def firmware_version(record: dict) -> str | None:
    """Validate only received SDK metadata, never wire parameters or legacy state."""
    value = record.get("firmwareVersion")
    if not isinstance(value, str):
        return None
    version = value.strip()
    if (
        len(version) <= FIRMWARE_VERSION_MAX_LENGTH
        and re.fullmatch(r"[A-Za-z0-9._+-]+", version)
        and re.search(r"[0-9]", version)
    ):
        return version
    return None


def sync_firmware_registry(
    hass: HomeAssistant, entry_id: str, snapshot: dict[str, dict]
) -> None:
    """Update only existing devices belonging to this integration entry."""
    versions = {
        serial: version
        for serial, record in snapshot.items()
        if (version := firmware_version(record)) is not None
    }
    if not entry_id or not versions:
        return
    registry = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(registry, entry_id):
        for domain, serial in device.identifiers:
            if domain == DOMAIN and (version := versions.get(serial)):
                if device.sw_version != version:
                    registry.async_update_device(device.id, sw_version=version)
                break


def apply_metadata_event(coordinator: Any, event: dict) -> bool:
    """Apply validated metadata and journal it across an overlapping snapshot."""
    serial = event.get("deviceSn")
    value = event.get("firmwareVersion")
    if (
        event.get("event") != "deviceMetadata"
        or not isinstance(serial, str)
        or serial not in (coordinator.data or {})
        or "firmwareVersion" not in event
        or (value is not None and firmware_version(event) is None)
    ):
        return False
    value = firmware_version(event)
    coordinator.data[serial]["firmwareVersion"] = value
    pending = getattr(coordinator, "_pending_metadata_events", None)
    if pending is not None:
        pending[serial] = value
    sync_firmware_registry(
        coordinator.hass,
        coordinator.config_entry.entry_id,
        {serial: coordinator.data[serial]},
    )
    coordinator.async_update_listeners()
    return True
