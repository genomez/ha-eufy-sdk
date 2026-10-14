"""Synchronize generic bridge property events with the coordinator snapshot."""

from __future__ import annotations

import math
from time import time
from typing import Any

MAX_OBSERVATION_AGE_SECONDS = 60


def apply_property_event(coordinator: Any, event: dict[str, Any]) -> bool:
    """Apply an explicit scalar value for a known device and notify its entities."""
    if event.get("event") != "propertyChanged" or "value" not in event:
        return False
    serial = event.get("deviceSn") or event.get("sn")
    name = event.get("property")
    data = coordinator.data
    if (
        not isinstance(serial, str)
        or not isinstance(name, str)
        or not name
        or not data
        or serial not in data
    ):
        return False
    value = event["value"]
    if value is not None and (
        type(value) not in (bool, int, float, str)
        or (isinstance(value, float) and not math.isfinite(value))
    ):
        return False
    data[serial].setdefault("state", {})[name] = value
    record = getattr(coordinator, "record_state_event", None)
    if callable(record):
        record(serial, {name: value})
    coordinator.async_update_listeners()
    return True


def apply_observation_event(coordinator: Any, event: dict[str, Any]) -> bool:
    """Retain device receipts separately from polled or optimistic state."""
    serial = event.get("deviceSn")
    stamp = event.get("observedAtMs")
    values = event.get("properties")
    now = time()
    if (
        event.get("event") != "propertiesObserved"
        or event.get("source") != "rtc"
        or not isinstance(serial, str)
        or serial not in (coordinator.data or {})
        or type(stamp) not in (int, float)
        or not math.isfinite(stamp)
        or not 0 <= now - stamp / 1000 <= MAX_OBSERVATION_AGE_SECONDS
        or not isinstance(values, dict)
    ):
        return False
    receipts = coordinator.property_observations
    if receipts is None:
        receipts = {}
        coordinator.property_observations = receipts
    target = receipts.setdefault(serial, {})
    accepted = {}
    for name, value in values.items():
        if (
            not isinstance(name, str)
            or name not in coordinator.data[serial].get("state", {})
            or type(value) not in (bool, int, float, str)
            or (
                type(coordinator.data[serial]["state"].get(name)) is bool
                and type(value) is not bool
            )
            or (isinstance(value, float) and not math.isfinite(value))
            or stamp / 1000 <= target.get(name, {}).get("observed_at", 0)
        ):
            continue
        target[name] = {"value": value, "observed_at": stamp / 1000}
        accepted[name] = value
    if not accepted:
        return False
    coordinator.data[serial].setdefault("state", {}).update(accepted)
    coordinator.record_state_event(serial, accepted)
    coordinator.async_update_listeners()
    return True
