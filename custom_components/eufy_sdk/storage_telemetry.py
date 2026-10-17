"""Optional S1 storage diagnostics from cached, timestamped bridge snapshots."""

# Modified for genomez S1 experimental source preview, 2026-10-17.
# Reuse validated UTC timestamps and skip storage work while disconnected.

from __future__ import annotations

import math
import time
from datetime import UTC, datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.helpers.event import async_call_later

from .entity import EufySdkDeviceEntity

FRESHNESS_MS = 2 * 60 * 60 * 1000
UNIT_PROFILE = "T9000/4.4.0.4 app-compared MiB"
_MAX_INTEGER = (1 << 53) - 1
_PERCENT_MAX = 100
_FAILURE_CATEGORIES = (
    "busy",
    "consumed_session",
    "timeout",
    "invalid_quantity",
    "ownership_changed",
    "other",
)
_BYTE_FIELDS = (
    "total_bytes",
    "used_bytes",
    "available_bytes",
    "video_bytes",
    "system_bytes",
)
_SENSORS = (
    ("total_bytes", "HDD total", "B"),
    ("used_bytes", "HDD used", "B"),
    ("available_bytes", "HDD available", "B"),
    ("used_percent", "HDD used percentage", "%"),
    ("video_bytes", "HDD video used", "B"),
    ("system_bytes", "HDD system used", "B"),
    ("observed_at", "Storage observation", None),
)


def _now_ms() -> float:
    return time.time() * 1000


def _attempt_diagnostics(telemetry: dict) -> dict[str, Any]:
    """Keep optional attempt evidence bounded, including from older bridges."""
    result = {}
    for source, target in (
        ("lastAttemptAtMs", "last_attempt_at_ms"),
        ("nextEligibleAtMs", "next_eligible_at_ms"),
    ):
        value = telemetry.get(source)
        # Older bridge builds send a fractional next-attempt time.
        if type(value) is float and math.isfinite(value):
            value = int(value)
        result[target] = (
            value if type(value) is int and 0 <= value <= _MAX_INTEGER else None
        )
    failures = telemetry.get("consecutiveFailures")
    result["consecutive_failures"] = (
        failures if type(failures) is int and 0 <= failures <= _MAX_INTEGER else 0
    )
    category = telemetry.get("lastFailureCategory")
    result["last_failure_category"] = (
        (category if category in _FAILURE_CATEGORIES else "other")
        if result["consecutive_failures"]
        else None
    )
    return result


def supports_storage(device: dict[str, Any]) -> bool:
    """Require the actual S1 station and explicit supported snapshot schema."""
    telemetry = device.get("storageTelemetry")
    return (
        device.get("codec") == "station"
        and device.get("model") == "T9000"
        and device.get("firmwareVersion") == "4.4.0.4"
        and device.get("stationSn") in (None, device.get("sn"))
        and isinstance(telemetry, dict)
        and type(telemetry.get("schemaVersion")) is int
        and telemetry["schemaVersion"] == 1
        and telemetry.get("supported") is True
    )


def storage_reading(device: dict[str, Any], now_ms: float) -> tuple[str, dict | None]:
    """Validate the complete projection; never consult a client or refresh a value."""
    if not supports_storage(device):
        return "unavailable", None
    telemetry = device["storageTelemetry"]
    status = telemetry.get("status")
    if (
        type(telemetry.get("enabled")) is not bool
        or telemetry.get("exactlyCorrelated") is not False
        or status not in ("disabled", "unknown", "current", "stale", "unavailable")
    ):
        return "unavailable", None
    if (status == "disabled" and telemetry["enabled"] is False) or (
        status == "unknown" and telemetry["enabled"] is True
    ):
        return "unknown", None
    if telemetry["enabled"] is not True or status != "current":
        return "unavailable", None
    if not _valid_current(telemetry, now_ms):
        return "unavailable", None
    return "current", telemetry


def storage_observed_at(device: dict[str, Any], now_ms: float) -> int | None:
    """Keep a validated original timestamp for current or historical stale data."""
    if not supports_storage(device):
        return None
    telemetry = device["storageTelemetry"]
    observed = telemetry.get("observedAtMs")
    if (
        telemetry.get("enabled") is not True
        or telemetry.get("status") not in ("current", "stale")
        or telemetry.get("exactlyCorrelated") is not False
        or telemetry.get("unitProfile") != UNIT_PROFILE
        or telemetry.get("correlation") != "time-associated"
        or type(observed) is not int
        or not 0 < observed <= _MAX_INTEGER
        or not math.isfinite(now_ms)
        or observed > now_ms
    ):
        return None
    return observed


def _valid_current(telemetry: dict, now_ms: float) -> bool:
    """Validate freshness, field types and the qualified denominator together."""
    observed = telemetry.get("observedAtMs")
    values = telemetry.get("values")
    if (
        type(observed) is not int
        or not 0 < observed <= _MAX_INTEGER
        or not math.isfinite(now_ms)
        or not 0 <= now_ms - observed < FRESHNESS_MS
        or telemetry.get("unitProfile") != UNIT_PROFILE
        or telemetry.get("correlation") != "time-associated"
        or not isinstance(values, dict)
    ):
        return False
    if any(
        type(values.get(field)) is not int or not 0 <= values[field] <= _MAX_INTEGER
        for field in _BYTE_FIELDS
    ):
        return False
    total, used = values["total_bytes"], values["used_bytes"]
    percent = values.get("used_percent")
    return not (
        total <= 0
        or used > total
        or values["available_bytes"] != total - used
        or values["video_bytes"] + values["system_bytes"] != used
        or type(percent) not in (int, float)
        or not 0 <= percent <= _PERCENT_MAX
        or not math.isfinite(percent)
        or not math.isclose(
            percent, _PERCENT_MAX * used / total, rel_tol=1e-9, abs_tol=1e-7
        )
    )


def storage_sensors(coordinator: Any) -> list[EufySdkStorageSensor]:
    """Register stable optional sensors only for explicitly supported S1 stations."""
    return [
        EufySdkStorageSensor(coordinator, serial, field, name, unit)
        for serial, device in coordinator.data.items()
        if supports_storage(device)
        for field, name, unit in _SENSORS
    ]


class EufySdkStorageSensor(EufySdkDeviceEntity, SensorEntity):
    """A validated cached storage quantity with one cancellable expiry timer."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_icon = "mdi:harddisk"

    def __init__(
        self, coordinator: Any, serial: str, field: str, name: str, unit: str | None
    ) -> None:
        """Bind a diagnostic to the existing station and a stable field identity."""
        super().__init__(coordinator, serial)
        self._field = field
        self._attr_unique_id = f"{serial}_storage_{field}"
        self._attr_name = name
        self._expiry_cancel = None
        self._observed_datetime_ms: int | None = None
        self._observed_datetime: datetime | None = None
        if field == "observed_at":
            self._attr_device_class = SensorDeviceClass.TIMESTAMP
            self._attr_icon = "mdi:clock-outline"
        else:
            self._attr_native_unit_of_measurement = unit
            self._attr_state_class = SensorStateClass.MEASUREMENT
            if unit == "B":
                self._attr_device_class = SensorDeviceClass.DATA_SIZE

    @property
    def available(self) -> bool:
        """Require owner availability and a valid current or empty projection."""
        if not super().available:
            return False
        now_ms = _now_ms()
        return (
            self._field == "observed_at"
            and storage_observed_at(self.device, now_ms) is not None
        ) or storage_reading(self.device, now_ms)[0] != "unavailable"

    @property
    def native_value(self) -> int | float | datetime | None:
        """Return cached qualified values with the original observation timestamp."""
        if not super().available:
            return None
        now_ms = _now_ms()
        if self._field == "observed_at":
            observed = storage_observed_at(self.device, now_ms)
            if observed is None:
                return None
            if observed != self._observed_datetime_ms:
                self._observed_datetime = datetime.fromtimestamp(observed / 1000, UTC)
                self._observed_datetime_ms = observed
            return self._observed_datetime
        status, reading = storage_reading(self.device, now_ms)
        if status != "current" or reading is None:
            return None
        return reading["values"][self._field]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Retain timestamp provenance after values expire, without reviving values."""
        if not super().available:
            status, observed = "unavailable", None
            attempts = _attempt_diagnostics({})
        else:
            now_ms = _now_ms()
            device = self.device
            status = storage_reading(device, now_ms)[0]
            observed = storage_observed_at(device, now_ms)
            attempts = _attempt_diagnostics(
                device["storageTelemetry"] if supports_storage(device) else {}
            )
            if observed is not None and (
                device["storageTelemetry"]["status"] == "stale"
                or now_ms - observed >= FRESHNESS_MS
            ):
                status = "stale"
        if status == "unknown":
            status = (
                "off"
                if device["storageTelemetry"]["enabled"] is False
                else "waiting_for_first_reading"
            )
        if (
            status in ("waiting_for_first_reading", "current", "stale")
            and attempts["last_attempt_at_ms"] is not None
            and attempts["consecutive_failures"] > 0
        ):
            status = "last_attempt_failed"
        return {
            "storage_status": status,
            **attempts,
            "unit_profile": UNIT_PROFILE if observed is not None else None,
            "correlation": "time-associated" if observed is not None else None,
            "exactly_correlated": False,
            "observed_at_ms": observed,
        }

    async def async_added_to_hass(self) -> None:
        """Subscribe through the coordinator and arm one expiry callback."""
        await super().async_added_to_hass()
        self._schedule_expiry()

    @callback
    def _cancel_expiry(self) -> None:
        if self._expiry_cancel is not None:
            self._expiry_cancel()
            self._expiry_cancel = None

    @callback
    def _schedule_expiry(self) -> None:
        self._cancel_expiry()
        now_ms = _now_ms()
        status, reading = storage_reading(self.device, now_ms)
        if super().available and status == "current" and reading is not None:
            remaining = (reading["observedAtMs"] + FRESHNESS_MS - now_ms) / 1000
            self._expiry_cancel = async_call_later(self.hass, remaining, self._expire)

    @callback
    def _expire(self, _now: object) -> None:
        self._expiry_cancel = None
        self._schedule_expiry()
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._schedule_expiry()
        super()._handle_coordinator_update()

    async def async_will_remove_from_hass(self) -> None:
        """Cancel expiry before removing the coordinator subscription."""
        self._cancel_expiry()
        await super().async_will_remove_from_hass()
