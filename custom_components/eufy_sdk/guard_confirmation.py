"""Temporal guard reports and deduplicated intent problem delivery."""

from __future__ import annotations

import asyncio
import hashlib
import re
from typing import TYPE_CHECKING, Any

from homeassistant.components import persistent_notification
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN
from .notification_delivery import (
    NOTICE_RETRY_DELAYS,
    PHONE_FAILURE_SENTENCE,
    deliver_phone_notice,
    notice_retry_delay,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .data import EufySdkConfigEntry

REASONS = frozenset(
    {
        "waiting_dispatch",
        "waiting_report",
        "reported_after_command",
        "different_mode_reported",
        "window_expired",
        "budget_expired",
        "ack_missing",
        "transport_uncertain",
        "cancelled",
        "superseded",
        "session_changed",
        "runtime_closed",
        "dispatch_rejected",
    }
)
MAX_TARGETS = 1024
MAX_SERIAL_LENGTH = 128
KEYS = (
    "version",
    "runtimeGeneration",
    "handle",
    "generation",
    "deviceSn",
    "requestedMode",
    "outcome",
    "reason",
    "attempts",
    "outcomeAtMs",
)

MODE_LABELS = {
    0: "Away",
    1: "Home",
    2: "Schedule",
    3: "Custom 1",
    4: "Custom 2",
    5: "Custom 3",
    6: "Off",
    47: "Geofencing",
    63: "Disarmed",
}


def guard_runtime(raw: Any) -> str | None:
    """Validate an authoritative runtime advertisement independently of receipts."""
    if (
        isinstance(raw, dict)
        and type(raw.get("version")) is int
        and raw["version"] == 1
        and isinstance(raw.get("runtimeGeneration"), str)
        and re.fullmatch(r"[a-f0-9-]{36}", raw["runtimeGeneration"])
    ):
        return raw["runtimeGeneration"]
    return None


def guard_capability(raw: Any) -> bool:
    """Enable only the explicit single-attempt contract."""
    return (
        isinstance(raw, dict)
        and type(raw.get("version")) is int
        and raw["version"] == 1
        and type(raw.get("maxAttempts")) is int
        and raw["maxAttempts"] == 1
        and raw.get("enabledRetry") is False
    )


def guard_receipt(raw: Any) -> dict | None:  # noqa: PLR0911 - validate each independent wire field
    """Validate additive metadata without projecting unknown payload fields."""
    if (
        not isinstance(raw, dict)
        or not all(key in raw for key in KEYS)
        or type(raw.get("version")) is not int
        or raw["version"] != 1
    ):
        return None
    if any(
        not isinstance(raw.get(key), str)
        or not re.fullmatch(r"[a-f0-9-]{36}", raw[key])
        for key in ("runtimeGeneration", "handle")
    ):
        return None
    if (
        not isinstance(raw.get("deviceSn"), str)
        or not 1 <= len(raw["deviceSn"]) <= MAX_SERIAL_LENGTH
        or type(raw.get("generation")) is not int
        or not 1 <= raw["generation"] <= 2**53 - 1
    ):
        return None
    if (
        type(raw.get("requestedMode")) is not int
        or raw["requestedMode"] not in {0, 1, 2, 3, 4, 5, 6, 47, 63}
        or type(raw.get("attempts")) is not int
        or raw["attempts"] not in {0, 1}
        or not isinstance(raw.get("outcome"), str)
        or not isinstance(raw.get("reason"), str)
        or raw.get("outcome")
        not in {"pending", "reported", "contradicted", "not_reported"}
        or raw.get("reason") not in REASONS
    ):
        return None
    if raw["outcome"] == "pending":
        if raw.get("outcomeAtMs") is not None or raw["reason"] not in {
            "waiting_dispatch",
            "waiting_report",
        }:
            return None
    elif (
        type(raw.get("outcomeAtMs")) is not int
        or not 0 <= raw["outcomeAtMs"] <= 2**53 - 1
    ):
        return None
    if raw["outcome"] in {"reported", "contradicted"} and (
        raw["attempts"] != 1
        or raw["reason"]
        != {
            "reported": "reported_after_command",
            "contradicted": "different_mode_reported",
        }[raw["outcome"]]
    ):
        return None
    return {key: raw.get(key) for key in KEYS}


def guard_attributes(client: Any, serial: str) -> dict:
    """Expose intent outcomes separately from the ordinary panel state."""
    if getattr(client, "guard_confirmation_supported", False) is not True:
        return {}
    receipt = guard_receipt(getattr(client, "guard_receipts", {}).get(serial))
    if receipt is None:
        return {}
    return {
        "guard_requested_mode": receipt["requestedMode"],
        "guard_outcome": receipt["outcome"],
        "guard_reason": receipt["reason"],
        "guard_attempts": receipt["attempts"],
        "guard_outcome_time": receipt["outcomeAtMs"],
    }


class GuardNotifications:
    """Maintain one bounded station problem, with shared phone retries."""

    def __init__(
        self,
        hass: HomeAssistant,
        options: dict,
        entry: EufySdkConfigEntry | None = None,
    ) -> None:
        """Bind delivery to the loaded entry and its configured notify service."""
        self.hass = hass
        self.options = options
        self.entry = entry
        self.seen: dict[tuple[str, str], None] = {}
        self.latest: dict[str, tuple[str, int, bool]] = {}
        self.notices: dict[str, tuple[str, str, str]] = {}
        self.unresolved: dict[str, int] = {}
        self.tasks: dict[str, asyncio.Task] = {}

    def options_updated(self, options: dict) -> None:
        """Route unresolved problems to the live service and cancel old deliveries."""
        previous = self.options.get("light_safety_notify_service")
        self.options = options
        if previous == options.get("light_safety_notify_service"):
            return
        self.close()
        for serial, (title, message, tag) in self.notices.items():
            self._schedule(serial, title, message, tag)

    def _station_name(self, serial: str) -> str:
        """Use the device registry display name, excluding a serial-valued alias."""
        device = dr.async_get(self.hass).async_get_device(
            identifiers={(DOMAIN, serial)}
        )
        name = (device.name_by_user or device.name) if device else None
        if (
            isinstance(name, str)
            and name.strip()
            and serial.casefold() not in name.casefold()
        ):
            return name
        return "HomeBase"

    def _mode_label(self, serial: str, mode: int) -> str:
        """Use the select's published option label, with known-mode fallbacks."""
        properties = getattr(
            getattr(self.entry, "runtime_data", None), "properties", {}
        )
        for spec in properties.get(serial, []):
            if spec.get("name") == "armingMode":
                labels = spec.get("enumValues") or {}
                label = labels.get(str(mode), labels.get(mode))
                if (
                    isinstance(label, str)
                    and label
                    and serial.casefold() not in label.casefold()
                ):
                    return label
        return MODE_LABELS.get(mode, f"mode {mode}")

    def _schedule(self, serial: str, title: str, message: str, tag: str) -> None:
        """Keep at most one phone delivery task per tracked station."""
        service = self.options.get("light_safety_notify_service")
        if not isinstance(service, str) or not service:
            return
        task = self.hass.async_create_task(self._deliver(service, title, message, tag))
        self.tasks[serial] = task

        def finished(done: asyncio.Task) -> None:
            if self.tasks.get(serial) is done:
                self.tasks.pop(serial, None)

        task.add_done_callback(finished)

    def apply(self, raw: dict) -> None:
        """Ignore older or duplicate outcomes; only a later report clears problems."""
        receipt = guard_receipt(raw)
        if receipt is None:
            return
        serial, handle = receipt["deviceSn"], receipt["handle"]
        previous = self.latest.get(serial)
        if previous is None and len(self.latest) >= MAX_TARGETS:
            return
        if (
            previous
            and previous[0] == receipt["runtimeGeneration"]
            and (
                receipt["generation"] < previous[1]
                or (receipt["generation"] == previous[1] and previous[2])
            )
        ):
            return
        self.latest[serial] = (
            receipt["runtimeGeneration"],
            receipt["generation"],
            receipt["outcome"] != "pending",
        )
        key = (receipt["runtimeGeneration"], handle)
        if receipt["outcome"] == "pending" or key in self.seen:
            return
        self.seen[key] = None
        if len(self.seen) > MAX_TARGETS:
            self.seen.pop(next(iter(self.seen)))
        if receipt["outcome"] == "not_reported" and receipt["reason"] in {
            "superseded",
            "cancelled",
        }:
            return
        task = self.tasks.pop(serial, None)
        if task:
            task.cancel()
        if receipt["outcome"] == "reported":
            notice = self.notices.pop(serial, None)
            self.unresolved.pop(serial, None)
            if notice:
                persistent_notification.async_dismiss(self.hass, notice[2])
            return
        tag = f"eufy_guard_{hashlib.sha256(serial.encode()).hexdigest()}"
        title = (
            "Eufy guard mode not reported"
            if receipt["outcome"] == "not_reported"
            else "Eufy guard mode contradicted"
        )
        count = min(2**53 - 1, self.unresolved.get(serial, 0) + 1)
        self.unresolved[serial] = count
        mode = self._mode_label(serial, receipt["requestedMode"])
        explanation = (
            "The station reported a different guard mode after this command."
            if receipt["outcome"] == "contradicted"
            else "No matching report was received in time."
        )
        message = (
            f"{self._station_name(serial)}, requested guard mode {mode}. "
            f"{explanation} {count} unresolved intent(s). Check the station in the app."
        )
        persistent_notification.async_create(
            self.hass, message, title=title, notification_id=tag
        )
        self.notices[serial] = (title, message, tag)
        self._schedule(serial, title, message, tag)

    async def _deliver(self, service: str, title: str, message: str, tag: str) -> None:
        """Reuse phone formatting, bounded retry spacing and persistent fallback."""
        for attempt in range(1, len(NOTICE_RETRY_DELAYS) + 2):
            try:
                await deliver_phone_notice(
                    self.hass, service, title, message, tag, problem=True
                )
            except Exception:  # noqa: BLE001 - preserve persistent fallback
                if attempt == len(NOTICE_RETRY_DELAYS) + 1:
                    persistent_notification.async_create(
                        self.hass,
                        f"{message} {PHONE_FAILURE_SENTENCE}",
                        title=title,
                        notification_id=tag,
                    )
                    return
                await asyncio.sleep(notice_retry_delay(attempt))
            else:
                return

    def close(self) -> None:
        """Cancel delivery tasks on entry unload without replaying commands."""
        for task in self.tasks.values():
            task.cancel()
        self.tasks.clear()
