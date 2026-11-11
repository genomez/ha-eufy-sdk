"""Temporal guard reports and deduplicated intent problem delivery."""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
import time
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
from .property_sync import MAX_OBSERVATION_AGE_SECONDS

if TYPE_CHECKING:
    from collections.abc import Callable

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
RECONNECT_CONFIRM_SECONDS = 60
NOT_REPORTED_GRACE_SECONDS = 10
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


class GuardReconciliation:
    """Keep local intent evidence across receipt loss without replaying writes."""

    def __init__(self, emit: Callable[[dict], None]) -> None:
        """Bind local resolutions to the client's identity-fenced projection."""
        self.emit = emit
        self.starts: dict[str, float] = {}
        self.identities: dict[str, tuple[str, str]] = {}
        self.states: dict[str, tuple[float, int | None]] = {}
        self.preparing: dict[str, object] = {}
        self.lost: dict[str, dict] = {}
        self.timers: dict[str, asyncio.TimerHandle] = {}
        self.loss_deadlines: dict[str, float] = {}
        self.pending: dict[str, dict] = {}
        self.restarts: dict[tuple[str, str], None] = {}
        self.closed = False

    def begin(self, serial: str) -> object | None:
        """Fence cached observations before a local command can be dispatched."""
        if serial not in self.starts and len(self.starts) >= MAX_TARGETS:
            return None
        self.starts[serial] = time.monotonic()
        token = object()
        self.preparing[serial] = token
        return token

    def end(self, serial: str, token: object | None) -> None:
        """Retire an RPC's preparation fence without disturbing a newer command."""
        if self.preparing.get(serial) is token:
            self.preparing.pop(serial, None)

    def track(self, receipt: dict) -> None:
        """Start conservative evidence tracking for remotely initiated intents."""
        serial = receipt["deviceSn"]
        identity = (receipt["runtimeGeneration"], receipt["handle"])
        if serial not in self.starts and len(self.starts) >= MAX_TARGETS:
            return
        if self.identities.get(serial) != identity:
            if serial not in self.preparing:
                self.starts[serial] = time.monotonic()
            self.identities[serial] = identity
            self.preparing.pop(serial, None)
            self.loss_deadlines.pop(serial, None)
        self._forget_loss(serial)
        if receipt["outcome"] == "pending":
            self.pending[serial] = receipt
        else:
            self.pending.pop(serial, None)

    def lose(self, receipt: dict, *, after_restart: bool = True) -> None:
        """Retain a lost pending receipt until fresh state or a finite deadline."""
        serial = receipt["deviceSn"]
        if after_restart:
            self.restarts[(receipt["runtimeGeneration"], receipt["handle"])] = None
            if len(self.restarts) > MAX_TARGETS:
                self.restarts.pop(next(iter(self.restarts)))
        if serial in self.lost:
            # Another restart must fence old state without extending the deadline.
            if after_restart:
                self.starts[serial] = time.monotonic()
            return
        if serial not in self.starts:
            self.track(receipt)
        if serial not in self.starts:
            return
        if after_restart:
            self.starts[serial] = time.monotonic()
        self.lost[serial] = receipt
        self.loss_deadlines[serial] = time.monotonic() + RECONNECT_CONFIRM_SECONDS
        self.timers[serial] = asyncio.get_running_loop().call_later(
            RECONNECT_CONFIRM_SECONDS, self._expire_loss, serial
        )

    def _expire_loss(self, serial: str) -> None:
        """Bound unknown reported state after a lost pending intent."""
        pending = self.lost.get(serial)
        if pending is not None:
            self._resolve_loss(serial, matched=self.matches(pending))

    def observe(self, serial: str, mode: Any, started: float) -> None:
        """Accept only valued, ordered arming-mode reports for tracked stations."""
        if serial not in self.starts:
            return
        previous = self.states.get(serial)
        if previous and started < previous[0]:
            return
        mode = mode if type(mode) is int and mode in MODE_LABELS else None
        self.states[serial] = (started, mode)
        lost = self.lost.get(serial)
        if lost and lost["attempts"] == 1 and self.matches(lost):
            self._resolve_loss(serial, matched=True)

    def matches(self, receipt: dict) -> bool:
        """Require this intent's current reported mode and post-command evidence."""
        serial = receipt["deviceSn"]
        state = self.states.get(serial)
        return bool(
            self.identities.get(serial)
            == (receipt["runtimeGeneration"], receipt["handle"])
            and state
            and state[0] > self.starts[serial]
            and state[1] == receipt["requestedMode"]
        )

    def observe_receipt(self, event: dict) -> None:
        """Use RTC observation time, excluding delayed pre-command reports."""
        stamp = event.get("observedAtMs")
        properties = event.get("properties")
        if (
            event.get("source") != "rtc"
            or not isinstance(event.get("deviceSn"), str)
            or type(stamp) not in (int, float)
            or not math.isfinite(stamp)
            or not isinstance(properties, dict)
            or "armingMode" not in properties
        ):
            return
        age = time.time() - stamp / 1000
        if not 0 <= age <= MAX_OBSERVATION_AGE_SECONDS:
            return
        self.observe(
            event["deviceSn"], properties["armingMode"], time.monotonic() - age
        )

    def observe_snapshot(self, device: dict, started: float) -> None:
        """Never give cached values the poll's time as station observation time."""
        serial = device.get("sn")
        if serial not in self.starts:
            return
        state = device.get("state")
        mode = state.get("armingMode") if isinstance(state, dict) else None
        evidence = device.get("stateEvidence")
        evidence = evidence.get("armingMode") if isinstance(evidence, dict) else None
        if (
            isinstance(evidence, dict)
            and type(evidence.get("version")) is int
            and evidence.get("version") == 1
            and evidence.get("source") in ("rtc_push", "rtc_readback")
            and evidence.get("status") == "current"
        ):
            self.observe_receipt(
                {
                    "deviceSn": serial,
                    "source": "rtc",
                    "observedAtMs": evidence.get("receivedAtMs"),
                    "properties": {"armingMode": mode},
                }
            )
        previous = self.states.get(serial)
        if mode is None or (previous is not None and previous[1] != mode):
            # Missing/changed unqualified cache state cannot preserve a match.
            self.observe(serial, None, started)

    def restart_known(self, receipt: dict) -> bool:
        """Distinguish authoritative rollover from an expired recovery handle."""
        return (receipt["runtimeGeneration"], receipt["handle"]) in self.restarts

    def grace_seconds(self, receipt: dict) -> float:
        """Keep restart reconciliation and its delivery grace within one minute."""
        serial = receipt["deviceSn"]
        if (
            self.identities.get(serial)
            == (
                receipt["runtimeGeneration"],
                receipt["handle"],
            )
            and serial in self.loss_deadlines
        ):
            return min(
                NOT_REPORTED_GRACE_SECONDS,
                max(0, self.loss_deadlines[serial] - time.monotonic()),
            )
        return NOT_REPORTED_GRACE_SECONDS

    def _forget_loss(self, serial: str) -> None:
        """Retire a completed or replaced local reconciliation."""
        self.lost.pop(serial, None)
        timer = self.timers.pop(serial, None)
        if timer:
            timer.cancel()

    def _resolve_loss(self, serial: str, *, matched: bool) -> None:
        """Use only existing contract values for a local reconnect resolution."""
        pending = self.lost.get(serial)
        if pending is None:
            return
        self._forget_loss(serial)
        self.pending.pop(serial, None)
        silent_drop = matched and pending["attempts"] == 0
        self.emit(
            {
                **pending,
                "outcome": "reported"
                if matched and not silent_drop
                else "not_reported",
                "reason": (
                    "cancelled"
                    if silent_drop
                    else "reported_after_command"
                    if matched
                    else "runtime_closed"
                ),
                "outcomeAtMs": int(time.time() * 1000),
            }
        )

    def close(self) -> None:
        """Resolve unconfirmed pending intents on the safe side before unload."""
        if self.closed:
            return
        self.closed = True
        for serial, pending in tuple(self.pending.items()):
            self.lost.setdefault(serial, pending)
            # Notification delivery also checks the coordinator's current mode
            # and failed-poll gate before suppressing this unload resolution.
            self._resolve_loss(serial, matched=False)
        for timer in tuple(self.timers.values()):
            timer.cancel()
        self.timers.clear()
        self.lost.clear()


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
        self.deferred: dict[tuple[str, str], tuple[dict, asyncio.TimerHandle]] = {}
        self.closed = False

    def options_updated(self, options: dict) -> None:
        """Route unresolved problems to the live service and cancel old deliveries."""
        previous = self.options.get("light_safety_notify_service")
        self.options = options
        if previous == options.get("light_safety_notify_service"):
            return
        self._cancel_deliveries()
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

    def _accept(self, receipt: dict) -> bool:
        """Order and deduplicate outcomes independently of notification timing."""
        serial, handle = receipt["deviceSn"], receipt["handle"]
        previous = self.latest.get(serial)
        if previous is None and len(self.latest) >= MAX_TARGETS:
            return False
        if (
            previous
            and previous[0] == receipt["runtimeGeneration"]
            and (
                receipt["generation"] < previous[1]
                or (receipt["generation"] == previous[1] and previous[2])
            )
        ):
            return False
        self.latest[serial] = (
            receipt["runtimeGeneration"],
            receipt["generation"],
            receipt["outcome"] != "pending",
        )
        key = (receipt["runtimeGeneration"], handle)
        if receipt["outcome"] == "pending" or key in self.seen:
            return False
        self.seen[key] = None
        if len(self.seen) > MAX_TARGETS:
            self.seen.pop(next(iter(self.seen)))
        return True

    def apply(self, raw: dict) -> None:
        """Ignore older or duplicate outcomes; only a later report clears problems."""
        receipt = guard_receipt(raw)
        if receipt is None or not self._accept(receipt):
            return
        serial = receipt["deviceSn"]
        if receipt["outcome"] == "not_reported" and receipt["reason"] in {
            "superseded",
            "cancelled",
        }:
            return
        if receipt["outcome"] == "reported":
            task = self.tasks.pop(serial, None)
            if task:
                task.cancel()
            for deferred_key, (waiting, timer) in tuple(self.deferred.items()):
                if waiting["deviceSn"] == serial:
                    timer.cancel()
                    self.deferred.pop(deferred_key)
            notice = self.notices.pop(serial, None)
            self.unresolved.pop(serial, None)
            if notice:
                persistent_notification.async_dismiss(self.hass, notice[2])
            return
        reconciliation = getattr(
            getattr(getattr(self.entry, "runtime_data", None), "client", None),
            "guard_reconciliation",
            None,
        )
        if receipt["outcome"] == "not_reported" and reconciliation is not None:
            self._defer_problem(receipt, reconciliation)
            return
        self._raise_problem(receipt)

    def _defer_problem(
        self, receipt: dict, reconciliation: GuardReconciliation
    ) -> None:
        """Allow a finite grace period without silencing older real failures."""
        if self._reported_match(receipt, reconciliation):
            return
        if self.closed:
            self._raise_problem(receipt)
            return
        if len(self.deferred) >= MAX_TARGETS:
            # Capacity pressure must not silently lose a real failure.
            oldest = next(iter(self.deferred))
            waiting, timer = self.deferred.pop(oldest)
            timer.cancel()
            self._finish_problem(waiting, reconciliation)
        key = (receipt["runtimeGeneration"], receipt["handle"])
        self.deferred[key] = (
            receipt,
            asyncio.get_running_loop().call_later(
                reconciliation.grace_seconds(receipt),
                self._finish_deferred,
                key,
                reconciliation,
            ),
        )

    def _finish_deferred(
        self, key: tuple[str, str], reconciliation: GuardReconciliation
    ) -> None:
        """Recheck after late reports, before persistent or phone delivery."""
        waiting = self.deferred.pop(key, None)
        if waiting is not None:
            self._finish_problem(waiting[0], reconciliation)

    def _finish_problem(
        self, receipt: dict, reconciliation: GuardReconciliation
    ) -> None:
        """Suppress only a fresh match for the exact failed intent."""
        if not self._reported_match(receipt, reconciliation):
            self._raise_problem(receipt)

    def _reported_match(
        self, receipt: dict, reconciliation: GuardReconciliation
    ) -> bool:
        """Require fresh evidence and the coordinator's current reported state."""
        if not reconciliation.matches(receipt):
            return False
        coordinator = getattr(
            getattr(self.entry, "runtime_data", None), "coordinator", None
        )
        if coordinator is None:
            return True
        data = getattr(coordinator, "data", None) or {}
        mode = data.get(receipt["deviceSn"], {}).get("state", {}).get("armingMode")
        return (
            getattr(coordinator, "last_update_success", False) is True
            and type(mode) is int
            and mode == receipt["requestedMode"]
        )

    def _raise_problem(self, receipt: dict) -> None:
        """Publish one station problem after the fresh-state suppression gate."""
        serial = receipt["deviceSn"]
        task = self.tasks.pop(serial, None)
        if task:
            task.cancel()
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
            else {
                "dispatch_rejected": "The station rejected the command.",
                "ack_missing": "The command may not have reached the station.",
                "transport_uncertain": "The command may not have reached the station.",
                "runtime_closed": (
                    "The connection to the add-on was interrupted before a report."
                ),
                "session_changed": "The connection changed before a report.",
                "window_expired": "No report was received within the time window.",
                "budget_expired": "No report was received within the time window.",
            }.get(receipt["reason"], "The mode change could not be confirmed.")
        )
        runtime = getattr(self.entry, "runtime_data", None)
        reconciliation = getattr(
            getattr(runtime, "client", None), "guard_reconciliation", None
        )
        if receipt["attempts"] == 0:
            explanation = "The command was not sent to the station."
        elif (
            receipt["reason"] == "runtime_closed"
            and reconciliation is not None
            and reconciliation.restart_known(receipt)
        ):
            explanation = "The mode change could not be confirmed after a restart."
        count_sentence = f" {count} unresolved intents." if count > 1 else ""
        message = (
            f"{self._station_name(serial)}, requested guard mode {mode}. "
            f"{explanation}{count_sentence} Check the station in the app."
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

    def _cancel_deliveries(self) -> None:
        """Retire phone retries when routing changes or the entry unloads."""
        for task in self.tasks.values():
            task.cancel()
        self.tasks.clear()

    def close(self) -> None:
        """Flush unconfirmed alerts immediately when their timers cannot survive."""
        if self.closed:
            return
        self.closed = True
        self._cancel_deliveries()
        runtime = getattr(self.entry, "runtime_data", None)
        reconciliation = getattr(
            getattr(runtime, "client", None), "guard_reconciliation", None
        )
        if reconciliation is not None:
            reconciliation.close()
        for key, (receipt, timer) in tuple(self.deferred.items()):
            timer.cancel()
            if reconciliation is not None:
                self._finish_deferred(key, reconciliation)
            else:
                self._raise_problem(receipt)
        self.deferred.clear()
