"""Publish late face identifications independently of detection events."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from time import monotonic
from typing import TYPE_CHECKING, Any

from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant

PERSON_IDENTIFIED_EVENT = f"{DOMAIN}_person_identified"
IDENTIFICATION_TTL_SECONDS = 30 * 60
MAX_IDENTIFICATIONS = 512
_MILLISECOND_EPOCH_THRESHOLD = 100_000_000_000
type Occurrence = tuple[str | int | float, str | int | float | None]


def occurrence_for(data: dict[str, Any]) -> Occurrence | None:
    """Keep the bridge's exact occurrence pair; missing trigger time means None."""
    created, triggered = data.get("create_time"), data.get("trigger_time")
    for value in (created, triggered):
        if value is None:
            continue
        if type(value) not in (str, int, float):
            return None
        if isinstance(value, str) and not value.strip():
            return None
        if isinstance(value, float) and not math.isfinite(value):
            return None
    return (created, triggered) if created is not None else None


def timestamp_iso(value: Any, *, milliseconds: bool = False) -> str | None:
    """Convert a finite Unix timestamp to ISO UTC, safely ignoring invalid values."""
    if type(value) not in (int, float, str):
        return None
    try:
        seconds = float(value)
        if milliseconds or abs(seconds) >= _MILLISECOND_EPOCH_THRESHOLD:
            seconds /= 1000
        return datetime.fromtimestamp(seconds, UTC).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def detected_at_for(data: dict[str, Any]) -> str | None:
    """Prefer explicit detection milliseconds, then the original occurrence time."""
    return (
        timestamp_iso(data.get("detected_at_ms"), milliseconds=True)
        or timestamp_iso(data.get("create_time"))
        or timestamp_iso(data.get("trigger_time"))
    )


def valid_identification(data: dict[str, Any]) -> bool:
    """Require a named correction with a usable device and occurrence identity."""
    serial, name = data.get("deviceSn"), data.get("person_name")
    return (
        data.get("event") == "personIdentified"
        and isinstance(serial, str)
        and bool(serial.strip())
        and isinstance(name, str)
        and bool(name.strip())
        and occurrence_for(data) is not None
    )


class PersonIdentifications:
    """Deduplicate corrections before applying a sensor change or bus event."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Create an entry-scoped ledger and active Last person subscriptions."""
        self.hass = hass
        self._seen: dict[tuple[str, Occurrence], float] = {}
        self._listeners: dict[str, Callable[[dict[str, Any]], bool]] = {}

    @callback
    def listen(
        self, serial: str, listener: Callable[[dict[str, Any]], bool]
    ) -> Callable[[], None]:
        """Register the active sensor and remove only this subscription on unload."""
        self._listeners[serial] = listener

        @callback
        def unsubscribe() -> None:
            if self._listeners.get(serial) == listener:
                self._listeners.pop(serial)

        return unsubscribe

    @callback
    def close(self) -> None:
        """Release entry-owned subscriptions and deduplication state on unload."""
        self._listeners.clear()
        self._seen.clear()

    @callback
    def apply(self, data: dict[str, Any]) -> None:
        """Apply each valid correction once without generic bridge dispatch."""
        if not valid_identification(data):
            return
        occurrence = occurrence_for(data)
        serial = data["deviceSn"]
        now = monotonic()
        self._seen = {
            key: at
            for key, at in self._seen.items()
            if now - at < IDENTIFICATION_TTL_SECONDS
        }
        key = (serial, occurrence)
        if key in self._seen or len(self._seen) >= MAX_IDENTIFICATIONS:
            return
        self._seen[key] = now
        listener = self._listeners.get(serial)
        current = listener(data) if listener is not None else False
        device = dr.async_get(self.hass).async_get_device(
            identifiers={(DOMAIN, serial)}
        )
        names = data.get("person_names")
        if (
            not isinstance(names, list)
            or not names
            or not all(isinstance(name, str) and name.strip() for name in names)
        ):
            names = [data["person_name"]]
        self.hass.bus.async_fire(
            PERSON_IDENTIFIED_EVENT,
            {
                "device_id": device.id if device else None,
                "deviceSn": serial,
                "person_name": data["person_name"],
                "person_names": list(names),
                "detected_at": detected_at_for(data),
                "identified_at": timestamp_iso(
                    data.get("identified_at_ms"), milliseconds=True
                ),
                "current_last_person": current,
            },
        )
