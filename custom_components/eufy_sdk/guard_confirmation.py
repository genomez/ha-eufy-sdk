"""Temporal guard reports and deduplicated intent problem delivery."""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Any

from homeassistant.components import persistent_notification

from .notification_delivery import (
    NOTICE_RETRY_DELAYS,
    PHONE_FAILURE_SENTENCE,
    deliver_phone_notice,
    notice_retry_delay,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

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
    """Deliver one persistent problem per intent, with shared phone retries."""

    def __init__(self, hass: HomeAssistant, options: dict) -> None:
        """Bind delivery to the loaded entry and its configured notify service."""
        self.hass = hass
        self.options = options
        self.seen: dict[tuple[str, str], None] = {}
        self.latest: dict[str, tuple[str, int]] = {}
        self.notices: dict[str, set[str]] = {}
        self.tasks: dict[str, asyncio.Task] = {}

    def apply(self, raw: dict) -> None:
        """Ignore older or duplicate outcomes; only a later report clears problems."""
        receipt = guard_receipt(raw)
        if receipt is None:
            return
        serial, handle = receipt["deviceSn"], receipt["handle"]
        previous = self.latest.get(serial)
        if (
            previous
            and previous[0] == receipt["runtimeGeneration"]
            and receipt["generation"] < previous[1]
        ):
            return
        self.latest[serial] = (receipt["runtimeGeneration"], receipt["generation"])
        key = (receipt["runtimeGeneration"], handle)
        if receipt["outcome"] == "pending" or key in self.seen:
            return
        self.seen[key] = None
        if len(self.seen) > MAX_TARGETS:
            self.seen.pop(next(iter(self.seen)))
        task = self.tasks.pop(serial, None)
        if task:
            task.cancel()
        if receipt["outcome"] == "reported":
            for tag in self.notices.pop(serial, set()):
                persistent_notification.async_dismiss(self.hass, tag)
            return
        tag = f"eufy_guard_{handle}"
        title = (
            "Eufy guard mode not reported"
            if receipt["outcome"] == "not_reported"
            else "Eufy guard mode contradicted"
        )
        message = (
            f"Station {serial}, requested guard mode {receipt['requestedMode']}: "
            f"{receipt['outcome']} ({receipt['reason']}). "
            "A matching report after this command was not received. "
            "Check the station in the app."
        )
        persistent_notification.async_create(
            self.hass, message, title=title, notification_id=tag
        )
        self.notices.setdefault(serial, set()).add(tag)
        service = self.options.get("light_safety_notify_service")
        if isinstance(service, str) and service:
            self.tasks[serial] = self.hass.async_create_task(
                self._deliver(service, title, message, tag)
            )

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
