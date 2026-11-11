"""Opt-in floodlight safety, independent of optimistic entity presentation."""

# The controller shares the property's private lifecycle and target identity.
# ruff: noqa: SLF001, FBT001, FBT003

from __future__ import annotations

import asyncio
import math
from collections import deque
from time import monotonic, time
from typing import Any
from uuid import uuid4

from homeassistant.components import persistent_notification
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store

from .api import CommandReceipt, command_health, command_health_is_current
from .const import CONF_LIGHT_CORRECTION, CONF_LIGHT_CORRECTION_PAUSE
from .notification_delivery import (
    NOTICE_RETRY_DELAYS,
    PHONE_FAILURE_SENTENCE,
    deliver_phone_notice,
    notice_retry_delay,
)

SAFETY_EVENT = "eufy_sdk_floodlight_safety"
QUIET_SECONDS = 900
CAP_SECONDS = 1800
OFF_DWELL_SECONDS = 120
GAP_SECONDS = 300
OFF_ALERT_SECONDS = 300
OFF_BLOCKED_GRACE_SECONDS = 180
OFF_STUCK_BLOCKED_SECONDS = 300
OFF_STUCK_ESCALATE_SECONDS = 120
OFF_STUCK_ID_LENGTH = 32
ON_ALERT_SECONDS = 600
REPORT_FRESH_SECONDS = 120
REPORT_TIME_EPSILON = 0.000001
OFF_DELAYS = (60, 120, 300)
ON_DELAYS = (30, 60, 120, 300)
SPACING_SECONDS = 3
TICK_SECONDS = 15
RECOVERY_SECONDS = 45
# SDK late-effect work ends at sentAt + 45 s; the bounded caller lasts 80 s
# (api.BOUNDED_CALLER_SECONDS). Allow a further conservative settlement margin.
OUTCOME_SETTLE_SECONDS = 180
PHONE_KINDS = frozenset(
    {
        "off_unconfirmed",
        "off_blocked",
        "activation_gap",
        "now_off",
        "on_unconfirmed",
        "now_on",
    }
)
EPISODE_NOTICE_KINDS = (PHONE_KINDS - {"activation_gap"}) | {"safety_off"}
NOTICE_KINDS = frozenset(
    {
        "safety_off",
        "now_off",
        "now_on",
        "off_unconfirmed",
        "off_blocked",
        "activation_gap",
        "activation_restored",
        "on_unconfirmed",
        "camera_gap",
        "intent_gap",
        "outcome_gap",
        "camera_restored",
        "intent_restored",
        "outcome_restored",
    }
)


def supports_safety(entity: Any) -> bool:
    """Restrict policy to native T8425/T8426 lights attached to a T9000."""
    device = entity.device
    parent = (entity.coordinator.data or {}).get(device.get("stationSn"), {})
    return (
        entity._prop == "light"
        and device.get("model") in {"T8425", "T8426"}
        and parent.get("model") == "T9000"
        and device.get("stationSn") != entity._sn
    )


class HubQueue:
    """One controller RPC per hub; explicit HA commands do not acquire the lock."""

    def __init__(self) -> None:
        """Create an entry-local arbiter for one HomeBase."""
        self.lock = asyncio.Lock()
        self.next_send = 0.0
        self.controllers: set[LightCorrection] = set()

    def busy(self) -> bool:
        """Retain reservations for explicit requests and pending SDK recovery."""
        return any(c.explicit_busy or c.recovery_pending for c in self.controllers)


class LightCorrection:
    """One reported-state episode and current authority per floodlight."""

    def __init__(self, entity: Any) -> None:  # noqa: PLR0915
        """Bind policy to one property entity without starting work."""
        self.entity = entity
        self.generation = 0
        self.runtime_generation = 0
        self.state = "disabled"
        self.last_command = False
        self.on_authorized = False
        self.current_intent: bool | None = None
        self.intent_initialized = False
        self.off_since: float | None = None
        self.intent_bad_since: float | None = None
        self.observed: bool | None = None
        self.observed_at = 0.0
        self.report_floor = monotonic()
        self.report_floor_wall = time()
        self.observed_mono: float | None = None
        self.report_received: float | None = None
        self.on_since: float | None = None
        self.on_reported_at: float | None = None
        self.confirmed_on_minutes = 0
        self.confirmed_on_reported_at: float | None = None
        self.last_motion: float | None = None
        self.motion_known = False
        self.motion_at = 0.0
        self.motion_active = False
        self.pending: bool | None = None
        self.pending_since = 0.0
        self.sent_at = 0.0
        self.sent_wall = 0.0
        self.retry_due = 0.0
        self.attempts = 0
        self.safety_off = False
        self.sdk_intent: int | None = None
        self.recoveries: dict[int, float] = {}
        self.automatic_owned = False
        self.was_paused = False
        self.outcome_unknown_since: float | None = None
        self.receipt_unknown = False
        self.explicit_busy = 0
        self.health_projection: dict | None = None
        self.health_until = 0.0
        # Wall-clock end of the cooldown, fixed when health evidence arrives so the
        # exposed attribute does not change on every state write.
        self.health_until_ms: int | None = None
        self.timer = None
        self.task: asyncio.Task | None = None
        self.notice_task: asyncio.Task | None = None
        self.pause_unsub = None
        self.queue: HubQueue | None = None
        self.store: Store | None = None
        self.loaded = False
        self.loading = False
        self.storage_loaded = False
        self.activation_attempts = 0
        self.activation_retry_due = 0.0
        self.activation_task: asyncio.Task | None = None
        self.bound_entity: str | None = None
        self.started = monotonic()
        self.silence_since: float | None = None
        self.start_unsub = None
        self.stopped = False
        self.notice_kind: str | None = None
        self.notice_bell_kind: str | None = None
        self.notice_kinds: set[str] = set()
        self.notice_problem = False
        self.notice_pending: dict | None = None
        self.notice_queue: deque[dict] = deque()
        self.notice_retry_due = 0.0
        self.journal: deque[dict] = deque(maxlen=80)
        self.off_stuck: dict | None = None
        self.off_stuck_replay = False
        self.waiting_since: float | None = None
        self.watchdog_blocked = False
        self.blocked_since: float | None = None
        self.queue_owned = False
        self.off_send_in_flight = False
        self.off_stuck_floor: float | None = None

    @property
    def options(self) -> dict:
        """Read current entry options at each admission boundary."""
        return getattr(self.entity.coordinator.config_entry, "options", {})

    @property
    def enabled(self) -> bool:
        """Require opt-in, supported topology and an active lifecycle."""
        return (
            supports_safety(self.entity)
            and self.options.get(CONF_LIGHT_CORRECTION, False) is True
            and not self.entity._removed
        )

    @property
    def binding(self) -> str | None:
        """Resolve per-light intent without assuming an entity name."""
        return self.options.get("light_safety_intent_entities", {}).get(self.entity._sn)

    @property
    def paused(self) -> bool:
        """Fail closed for a configured pause helper with invalid state."""
        helper = self.options.get(CONF_LIGHT_CORRECTION_PAUSE)
        if not helper:
            return False
        state = self.entity.hass.states.get(helper)
        return state is None or state.state != "off"

    def intent(self) -> bool | None:
        """Let a binding override commands; use restored ON only to inhibit OFF."""
        if self.binding:
            state = self.entity.hass.states.get(self.binding)
            return {"on": True, "off": False}.get(
                state.state if state is not None else "unknown"
            )
        return self.last_command

    def _record(self, kind: str, **data: Any) -> None:
        self.journal.append({"at": time(), "kind": kind, **data})

    async def async_added(self) -> None:
        """Register for live options, without timers or storage when disabled."""
        registry = getattr(self.entity.coordinator, "_light_safety_controllers", None)
        if registry is None:
            registry = self.entity.coordinator._light_safety_controllers = set()
        registry.add(self)
        await self._activate()

    async def _activate(self) -> None:  # noqa: PLR0912, PLR0915
        if not self.enabled or self.loading or self._stopping():
            return
        self.loading = True
        try:
            if self.store is None:
                entry_id = self.entity.coordinator.config_entry.entry_id
                self.store = Store(
                    self.entity.hass,
                    1,
                    f"eufy_sdk_floodlight_safety.{entry_id}.{self.entity.entity_id}",
                )
            saved = await self.store.async_load() if not self.storage_loaded else None
            if not self.enabled:
                return
            if isinstance(saved, dict):
                self._restore_off_stuck(saved.get("off_stuck"))
                observation = saved.get("observation")
                if (
                    self.observed is None
                    and isinstance(observation, dict)
                    and type(observation.get("value")) is bool
                    and type(observation.get("at")) in (int, float)
                    and math.isfinite(observation["at"])
                    and 0 < observation["at"] <= time()
                ):
                    self.observed = observation["value"]
                    self.observed_at = observation["at"]
                if not self.on_authorized and self.pending is None:
                    self.last_command = saved.get("last_command") is True
                    if saved.get("pending_off") is True:
                        self.pending = False
                        self.pending_since = monotonic()
                        self.sent_at = monotonic()
                        self.sent_wall = time()
                        self.retry_due = monotonic() + OFF_DELAYS[0]
                        attempts = saved.get("attempts", 0)
                        self.attempts = (
                            attempts if type(attempts) is int and attempts >= 0 else 0
                        )
                        self.safety_off = saved.get("safety_off") is True
                self.notice_kind = saved.get("notice_kind")
                self.notice_bell_kind = saved.get("notice_bell_kind")
                kinds = saved.get("notice_kinds", [])
                if isinstance(kinds, list):
                    self.notice_kinds.update(
                        kind for kind in kinds if kind in NOTICE_KINDS
                    )
                self.notice_problem = saved.get("notice_problem") is True
                queued = saved.get("notice_queue", [])
                notices = [saved.get("notice_pending")]
                if isinstance(queued, list):
                    notices.extend(queued[: len(NOTICE_KINDS)])
                for notice in notices:
                    if (
                        isinstance(notice, dict)
                        and notice.get("kind") in NOTICE_KINDS
                        and set(notice)
                        <= {
                            "kind",
                            "problem",
                            "attempts",
                            "emitted",
                            "phone_attempts",
                            "on_minutes",
                            "on_since",
                        }
                    ):
                        if self.notice_pending is None:
                            self.notice_pending = notice
                        else:
                            self.notice_queue.append(notice)
            self.storage_loaded = True
            self.loaded = True
            self.bound_entity = self.binding
            self.started = monotonic()
            if self.queue is None:
                queues = getattr(self.entity.coordinator, "_light_safety_queues", None)
                if queues is None:
                    queues = self.entity.coordinator._light_safety_queues = {}
                self.queue = queues.setdefault(
                    self.entity.device.get("stationSn"), HubQueue()
                )
                self.queue.controllers.add(self)
            if self.pause_unsub is None:
                self.pause_unsub = self.entity.hass.bus.async_listen(
                    "state_changed", self._state_changed
                )
            if (
                self.entity.hass.state is not CoreState.running
                and self.start_unsub is None
            ):
                self.start_unsub = self.entity.hass.bus.async_listen_once(
                    EVENT_HOMEASSISTANT_STARTED, self._ha_started
                )
            self.check_policy()
            self.activation_attempts = 0
            if "activation_gap" in self.notice_kinds:
                self.notice_kinds.discard("activation_gap")
                self.notice_kinds.discard("activation_restored")
                self._notice("activation_restored", False)
        except Exception:  # noqa: BLE001 - never retain raw storage exceptions
            self.loaded = False
            self.state = "protection gap"
            self._record("storage_unavailable")
            self.activation_attempts += 1
            self.activation_retry_due = monotonic() + notice_retry_delay(
                self.activation_attempts
            )
            self._notice("activation_gap", True)
            self._arm()
        finally:
            self.loading = False
            self.activation_task = None

    @callback
    def _ha_started(self, _event: Any) -> None:
        self.start_unsub = None
        self.check_policy()

    def _stopping(self) -> bool:
        return self.stopped or self.entity.hass.state in {
            CoreState.stopping,
            CoreState.final_write,
        }

    def _save(self) -> None:
        if self.store is not None and self.loaded and not self._stopping():
            self.store.async_delay_save(
                lambda: {
                    "last_command": self.last_command,
                    "pending_off": self.pending is False,
                    "attempts": self.attempts if self.pending is False else 0,
                    "safety_off": self.safety_off,
                    "notice_kind": self.notice_kind,
                    "notice_bell_kind": self.notice_bell_kind,
                    "notice_kinds": sorted(self.notice_kinds),
                    "notice_problem": self.notice_problem,
                    "notice_pending": {
                        key: value
                        for key, value in self.notice_pending.items()
                        if key != "retry_due"
                    }
                    if self.notice_pending is not None
                    else None,
                    "notice_queue": [
                        {key: value for key, value in row.items() if key != "retry_due"}
                        for row in self.notice_queue
                    ],
                    "off_stuck": self.off_stuck,
                    "observation": {"value": self.observed, "at": self.observed_at},
                },
                1,
            )

    def options_updated(self) -> None:
        """Revoke queued sends and apply safety options without a reload."""
        self._revoke_automatic()
        self.generation += 1
        if self.bound_entity != self.binding:
            self.on_authorized = False
            if self.pending is True:
                self.pending = None
        if not self.enabled:
            self.stop("disabled")
        else:
            self.entity.hass.async_create_task(self._activate())

    @callback
    def _state_changed(self, event: Any) -> None:
        if event.data.get("entity_id") in {
            self.binding,
            self.options.get(CONF_LIGHT_CORRECTION_PAUSE),
        }:
            self.check_policy()

    def start(self, value: Any) -> None:
        """Record every HA request before its first await, including no-op OFF."""
        if not self.enabled or type(value) is not bool:
            return
        self._revoke_automatic()
        self.generation += 1
        if self.pending is None or self.pending is not value:
            self.notice_kinds.difference_update(EPISODE_NOTICE_KINDS)
            self.notice_problem = False
        self.last_command = value
        self.on_authorized = value
        self.explicit_busy += 1
        if self.pending is not value:
            self.pending_since = monotonic()
        if self.pending is not value or value:
            self.sent_at = monotonic()
            self.sent_wall = time()
        self.pending = value
        self.attempts = 1
        self.safety_off = False
        self.retry_due = monotonic() + (ON_DELAYS[0] if value else OFF_DELAYS[0])
        self._record("ha_request", desired=value)
        self.check_policy()
        self._save()

    def explicit_finished(self) -> None:
        """Release the explicit RPC reservation on every exit path."""
        self.explicit_busy = max(0, self.explicit_busy - 1)
        self.check_policy()

    def receipt(self, receipt: CommandReceipt | None) -> None:
        """Retain hub ownership beyond the RPC while SDK recovery is pending."""
        if not self.enabled:
            return
        if isinstance(receipt, CommandReceipt):
            if receipt.send_attempted and self.pending is False:
                self.watchdog_blocked = False
                self.blocked_since = None
            self.health(receipt.command_health)
            self.sdk_intent = receipt.intent
            self.track_recovery(receipt)
            self._record(
                "outcome", outcome=receipt.status, attempted=receipt.send_attempted
            )
        else:
            self._record("outcome", outcome="unconfirmed")
        self.check_policy()

    def track_recovery(self, receipt: CommandReceipt | None) -> None:
        """Keep old SDK child ownership even when explicit intent supersedes it."""
        if (
            self.enabled
            and isinstance(receipt, CommandReceipt)
            and receipt.status == "pending_accepted"
            and type(receipt.intent) is int
        ):
            self.recoveries.setdefault(receipt.intent, monotonic() + RECOVERY_SECONDS)

    @property
    def recovery_pending(self) -> bool:
        """Retain every outstanding SDK token, including unknown terminal outcomes."""
        self._expire_outcomes()
        return bool(self.recoveries) or self.outcome_unknown_since is not None

    def _expire_outcomes(self) -> None:
        now = monotonic()
        if (
            self.outcome_unknown_since is not None
            and now - self.outcome_unknown_since
            >= OUTCOME_SETTLE_SECONDS - TICK_SECONDS
        ):
            self._notice("outcome_gap", True)
        if (
            self.outcome_unknown_since is not None
            and now - self.outcome_unknown_since >= OUTCOME_SETTLE_SECONDS
        ):
            self._notice("outcome_gap", True)
            self.receipt_unknown = False
            self.outcome_unknown_since = None
            self._record("outcome_wait_expired")
        for intent, due in tuple(self.recoveries.items()):
            if now >= due + OUTCOME_SETTLE_SECONDS:
                self._notice("outcome_gap", True)
                self.recoveries.pop(intent)
                self._record("recovery_wait_expired")
        self._recovery_settled()

    def new_runtime(self) -> None:
        """Clear transaction ownership when a replacement runtime is ready."""
        if not self.enabled:
            return
        self.generation += 1
        self.runtime_generation += 1
        self.recoveries.clear()
        self.receipt_unknown = False
        self.outcome_unknown_since = None
        self.sdk_intent = None
        self.automatic_owned = False
        self._record("runtime_outcomes_cleared")
        self._recovery_settled()

    @property
    def recovery_intent(self) -> int | None:
        """Expose the newest known recovery identity for diagnostics."""
        return next(reversed(self.recoveries), None)

    def _revoke_automatic(self) -> None:
        if self.automatic_owned:
            self.entity.client.pause_light_correction(
                self.entity._sn, keep_observation=True
            )
            self.automatic_owned = False

    def uncertain_outcome(self) -> None:
        """Without a final receipt, fail closed rather than overlap an unknown child."""
        if self.enabled:
            self.receipt_unknown = True
            if self.outcome_unknown_since is None:
                self.outcome_unknown_since = monotonic()
            self._record("outcome_unknown", reason="final_receipt_unavailable")

    def health(self, value: Any) -> None:
        """Mirror ordered command-health evidence without extending cooldowns."""
        health = command_health(value)
        if health is None or not command_health_is_current(
            health, self.health_projection
        ):
            return
        until = monotonic() + health["cooldownRemainingMs"] / 1000
        if self.health_projection is not None and health.get(
            "revision"
        ) == self.health_projection.get("revision"):
            until = min(until, self.health_until)
        previous = self.health_projection
        self.health_projection = health
        self.health_until = until if health["status"] == "command_degraded" else 0.0
        if health["status"] != "command_degraded":
            self.health_until_ms = None
        elif (
            self.health_until_ms is None
            or previous is None
            or previous.get("revision") != health.get("revision")
            or previous.get("status") != health["status"]
        ):
            # Set once per cooldown; repeated deliveries of the same revision keep
            # the end time so clock sampling cannot nudge the attribute.
            self.health_until_ms = int(
                time() * 1000 + max(0.0, until - monotonic()) * 1000
            )
        self.check_policy()

    def reset_health(self) -> None:
        """Accept new runtime revisions while retaining a known cooldown."""
        self.health_projection = None

    def reset_observations(self) -> None:
        """Retain confirmed evidence, but require fresh reports for commands."""
        self.report_floor = monotonic()
        self.report_floor_wall = time()
        if self.observed is not True:
            if self.silence_since is not None:
                self.silence_since = monotonic()
            self.report_received = None
        self.motion_known = False
        self.motion_active = False
        self.check_policy()

    def observe(self, event: dict) -> None:  # noqa: PLR0912
        """Only advancing, timestamped RTC reports qualify as camera evidence."""
        if not self.enabled or self._stopping():
            return
        stamp, values = event.get("observedAtMs"), event.get("properties")
        now, wall = monotonic(), time()
        if (
            event.get("source") != "rtc"
            or not isinstance(values, dict)
            or type(stamp) not in (int, float)
            or not math.isfinite(stamp)
            or not 0 <= wall - stamp / 1000 <= REPORT_FRESH_SECONDS
            or stamp / 1000 == self.observed_at
            or (
                self.observed_mono is not None
                and stamp / 1000 < self.observed_at
                and wall - now
                >= self.observed_at - self.observed_mono - REPORT_TIME_EPSILON
            )
        ):
            return
        observed_mono = now - (wall - stamp / 1000)
        if not self._after_report_floor(
            observed_mono, stamp / 1000, self.report_floor, self.report_floor_wall
        ) or (
            self.observed_mono is not None
            and not self._after_report_floor(
                observed_mono, stamp / 1000, self.observed_mono, self.observed_at
            )
        ):
            return
        telemetry = [
            values.get(key)
            for key in ("motion", "person", "personDetected")
            if key in values
        ]
        if telemetry:
            self.motion_known = all(type(value) is bool for value in telemetry)
            self.motion_at = monotonic()
            self.motion_active = any(value is True for value in telemetry)
            if self.motion_active:
                self.last_motion = monotonic()
        value = values.get("light")
        if type(value) is bool:
            if value and self.on_since is None:
                self.watchdog_blocked = False
                self.blocked_since = None
                if self.observed is False and self.pending is None:
                    self.notice_kinds.difference_update(EPISODE_NOTICE_KINDS)
                    self.notice_problem = False
                self.on_since = monotonic()
                self.on_reported_at = stamp / 1000
            elif not value:
                self.watchdog_blocked = False
                self.blocked_since = None
                if self.on_since is not None:
                    self.confirmed_on_minutes = int((monotonic() - self.on_since) / 60)
                    self.confirmed_on_reported_at = self.on_reported_at
                self.on_since = None
                self.on_reported_at = None
            self.observed, self.observed_at = value, stamp / 1000
            self.observed_mono = observed_mono
            self.report_received = monotonic()
            self._save()
            if self.intent() is False and not self.paused:
                self._off_stuck_confirmed()
            if "camera_gap" in self.notice_kinds:
                self.notice_kinds.discard("camera_gap")
                self.notice_kinds.discard("camera_restored")
                self._notice("camera_restored", False)
            self._record("report", value=value, reported_at=self.observed_at)
            if self.pending is value and self._report_after(
                self.sent_at, self.sent_wall
            ):
                self._confirmed()
        self.check_policy()

    def motion(self) -> None:
        """Extend quiet time on detections without assuming telemetry health."""
        if self.enabled:
            self.last_motion = monotonic()
            self._record("detection")
            self.check_policy()

    def event(self, event: dict) -> None:
        """Accept settlement only for the outstanding SDK recovery token."""
        intent = event.get("intent")
        if not self.enabled or intent not in self.recoveries:
            return
        if (
            event.get("recoveryBusy") is False
            and monotonic() >= self.recoveries[intent]
        ):
            self.recoveries.pop(intent)
            self._recovery_settled()
        self.check_policy()

    def _recovery_settled(self) -> None:
        if not self.recoveries and not self.receipt_unknown:
            self.outcome_unknown_since = None
            if "outcome_gap" in self.notice_kinds:
                self.notice_kinds.discard("outcome_gap")
                self.notice_kinds.discard("outcome_restored")
                self._notice("outcome_restored", False)

    def _confirmed(self) -> None:
        desired = self.pending
        if desired is False:
            self.watchdog_blocked = False
            self.blocked_since = None
        self.pending = None
        problem = (
            "on_unconfirmed" if desired else "off_unconfirmed"
        ) in self.notice_kinds or (
            desired is False and "off_blocked" in self.notice_kinds
        )
        if desired is False and self.safety_off:
            self._notice("now_off" if problem else "safety_off", False)
        elif problem:
            self._notice("now_on" if desired else "now_off", False)
        self._record("confirmed", value=desired, confirmation_at=self.observed_at)
        self.safety_off = False
        self._save()

    def _fresh(self) -> bool:
        return (
            self.report_received is not None
            and self.observed_mono is not None
            and self._report_after(self.report_floor, self.report_floor_wall)
            and 0 <= monotonic() - self.observed_mono <= REPORT_FRESH_SECONDS
        )

    @staticmethod
    def _after_report_floor(
        observed: float, stamp: float, floor: float, wall: float
    ) -> bool:
        """Order within one clock tick without losing wall-clock-step tolerance."""
        return observed > floor + REPORT_TIME_EPSILON or (
            abs(observed - floor) <= REPORT_TIME_EPSILON and stamp > wall
        )

    def _report_after(self, floor: float, wall: float) -> bool:
        return self.observed_mono is not None and self._after_report_floor(
            self.observed_mono, self.observed_at, floor, wall
        )

    def _quiet(self) -> bool:
        now = monotonic()
        return (
            self._fresh()
            and not self._waiting()
            and not self.motion_active
            and now
            - (self.last_motion if self.last_motion is not None else self.started)
            >= QUIET_SECONDS
        )

    def _watchdog_due(self) -> bool:
        return self._watchdog_overdue() and self._fresh() and not self._waiting()

    def _watchdog_overdue(self) -> bool:
        """Keep the correction deadline independent of transport admission."""
        deadline = self._watchdog_deadline()
        return deadline is not None and monotonic() >= deadline

    def _watchdog_deadline(self) -> float | None:
        """Anchor correction and alert grace to the current reported-ON episode."""
        if (
            self.intent() is not False
            or self.observed is not True
            or self.on_since is None
            or self.off_since is None
        ):
            return None
        deadline = self.on_since + CAP_SECONDS
        if not self.motion_active:
            quiet = (
                max(
                    self.on_since,
                    self.last_motion if self.last_motion is not None else self.started,
                )
                + QUIET_SECONDS
            )
            deadline = min(deadline, quiet)
        return max(deadline, self.off_since + OFF_DWELL_SECONDS)

    def _waiting(self, *, desired: bool | None = False) -> bool:
        health = self.entity.device.get("cameraHealth") or {}
        return (
            self.paused
            or monotonic() < self.health_until
            or getattr(self.entity.client, "connected", False) is not True
            or (desired is True and health.get("connectivity") == "offline")
            or health.get("parentTransport") == "disconnected"
        )

    def _waiting_reason(self) -> str | None:
        """Project transport waits into finite, public incident categories."""
        health = self.entity.device.get("cameraHealth") or {}
        if monotonic() < self.health_until:
            return "cooldown"
        if getattr(self.entity.client, "connected", False) is not True:
            return "bridge_disconnected"
        if health.get("connectivity") == "offline":
            return "camera_offline"
        if health.get("parentTransport") == "disconnected":
            return "parent_disconnected"
        return None

    def _restore_off_stuck(self, saved: Any) -> None:
        """Drop malformed incidents without affecting the safety's saved state."""
        if not isinstance(saved, dict) or set(saved) != {
            "incident_id",
            "reason",
            "opened_at",
            "off_since",
            "escalate_at",
            "confirmation_after",
        }:
            return
        identity, reason = saved["incident_id"], saved["reason"]
        if (
            not isinstance(identity, str)
            or len(identity) != OFF_STUCK_ID_LENGTH
            or any(char not in "0123456789abcdef" for char in identity)
            or not isinstance(reason, str)
            or reason not in {"no_confirmation", "no_reports", "blocked"}
        ):
            return
        opened, off, escalate = (
            saved["opened_at"],
            saved["off_since"],
            saved["escalate_at"],
        )
        floor = saved["confirmation_after"]
        if (
            type(opened) is not int
            or not 0 <= opened <= time()
            or type(escalate) is not int
            or escalate < opened + OFF_STUCK_ESCALATE_SECONDS
            or (off is not None and (type(off) is not int or not 0 <= off <= opened))
            or type(floor) not in (int, float)
            or not opened <= floor < opened + 1
            or floor > time()
        ):
            return
        self.off_stuck = dict(saved)
        self.off_stuck_floor = monotonic() - (time() - floor)
        self.off_stuck_replay = True

    def _off_stuck_event(self, kind: str, **data: Any) -> None:
        """Make matching attributes visible before publishing incident evidence."""
        identity = self.entity.entity_id
        state = self.entity.hass.states.get(identity)
        name = getattr(state, "name", None) or self.entity.name or identity
        self.entity.async_write_ha_state()
        self.entity.hass.bus.async_fire(
            SAFETY_EVENT, {"entity": identity, "name": name, "kind": kind, **data}
        )

    def _close_off_stuck(self, outcome: str) -> None:
        if (
            self.off_stuck is None
            or self._stopping()
            or self.entity.hass.state is not CoreState.running
        ):
            return
        identity = self.off_stuck["incident_id"]
        self.off_stuck = None
        self.off_stuck_floor = None
        self.off_stuck_replay = False
        self._save()
        self._off_stuck_event(
            "off_stuck_closed",
            incident_id=identity,
            closed_at=int(time()),
            outcome=outcome,
        )

    def _off_stuck_confirmed(self) -> None:
        if (
            self.off_stuck is not None
            and self.observed is False
            and self._fresh()
            and self.off_stuck_floor is not None
            and self._report_after(
                self.off_stuck_floor, self.off_stuck["confirmation_after"]
            )
            and (
                self.pending is not False
                or self._report_after(self.sent_at, self.sent_wall)
            )
        ):
            self._close_off_stuck("confirmed_off")

    def _check_off_stuck(  # noqa: PLR0912
        self, now: float, intent: bool | None, paused: bool, last: float
    ) -> None:
        """Report unresolved OFF incidents without authorizing any command."""
        if self._stopping() or self.entity.hass.state is not CoreState.running:
            return
        waiting = self._waiting_reason()
        if waiting is None:
            self.waiting_since = None
        elif self.waiting_since is None:
            self.waiting_since = now
        if paused or intent is not False:
            self._close_off_stuck(
                "paused"
                if paused
                else "intent_on"
                if intent is True
                else "intent_unknown"
            )
            return
        self._off_stuck_confirmed()
        reason = None
        if self.pending is False and now - self.pending_since >= OFF_ALERT_SECONDS:
            reason = "no_confirmation"
        elif self.observed is not False:
            if self.silence_since is not None and now - last >= GAP_SECONDS:
                reason = "no_reports"
            elif (
                self.waiting_since is not None
                and now - self.waiting_since >= OFF_STUCK_BLOCKED_SECONDS
            ):
                reason = "blocked"
        changed = False
        if self.off_stuck is None and reason is not None:
            wall = time()
            opened = int(wall)
            escalate = opened + OFF_STUCK_ESCALATE_SECONDS
            if reason != "no_confirmation":
                onset = self.waiting_since if reason == "blocked" else last
                episode = (
                    int(self.on_reported_at)
                    if self.on_reported_at is not None
                    else int(wall - (now - onset))
                )
                escalate = max(escalate, episode + CAP_SECONDS)
            off = self.off_since
            if off is None and self.pending is False:
                off = self.pending_since
            self.off_stuck = {
                "incident_id": uuid4().hex,
                "reason": reason,
                "opened_at": opened,
                "off_since": int(wall - (now - off)) if off is not None else None,
                "escalate_at": escalate,
                # Retain subsecond ordering across restarts, with integer public times.
                "confirmation_after": wall,
            }
            self.off_stuck_floor = now
            changed = True
        elif (
            self.off_stuck is not None
            and reason == "no_confirmation"
            and self.off_stuck["reason"] != reason
        ):
            self.off_stuck["reason"] = reason
            self.off_stuck["escalate_at"] = min(
                self.off_stuck["escalate_at"],
                self.off_stuck["opened_at"] + OFF_STUCK_ESCALATE_SECONDS,
            )
            changed = True
        if self.off_stuck is not None and (changed or self.off_stuck_replay):
            self.off_stuck_replay = False
            self._save()
            self._off_stuck_event(
                "off_stuck",
                **{
                    key: value
                    for key, value in self.off_stuck.items()
                    if key != "confirmation_after"
                },
                observed="on"
                if self.observed is True
                else "off"
                if self.observed is False
                else "unknown",
                camera_reported_at=int(self.observed_at) if self.observed_at else None,
                attempts=self.attempts,
                waiting=waiting,
            )

    def _allowed(self, generation: int, desired: bool, watchdog: bool) -> bool:
        return (
            generation == self.generation
            and self.enabled
            and self.loaded
            and not self._stopping()
            and self.entity.hass.state is CoreState.running
            and not self._waiting(desired=desired)
            and not self.explicit_busy
            and self.intent() is desired
            and not self.recovery_pending
            and (not desired or self.on_authorized)
            and (not watchdog or self._watchdog_due())
            and (watchdog or self.pending is desired)
            and not (self._fresh() and self.observed is desired)
        )

    def check_policy(self) -> None:  # noqa: PLR0912, PLR0915
        """Reconcile authority, status and a single periodic wakeup."""
        if self._stopping() or self.entity.hass.state is not CoreState.running:
            return
        if not self.enabled:
            self.blocked_since = None
            if not self.entity._removed:
                self.waiting_since = None
                self._close_off_stuck("disabled")
            return
        if self.silence_since is None and self.entity.client.connected is True:
            self.silence_since = monotonic()
        now, intent = monotonic(), self.intent()
        self._expire_outcomes()
        paused = self.paused
        if intent is not self.current_intent:
            self._revoke_automatic()
            self.generation += 1
            self.current_intent = intent
            if self.intent_initialized:
                self.notice_kinds.difference_update(EPISODE_NOTICE_KINDS)
                self.notice_problem = False
                self.watchdog_blocked = False
                self.blocked_since = None
            self.intent_initialized = True
            self.off_since = now if intent is False else None
            self._record(
                "intent",
                source="binding" if self.binding else "ha_command",
                desired=intent,
            )
            self._save()
        if paused and not self.was_paused:
            self._revoke_automatic()
        self.was_paused = paused
        binding_state = (
            self.entity.hass.states.get(self.binding) if self.binding else None
        )
        binding_bad = self.binding and (
            binding_state is None or binding_state.state in {"unknown", "unavailable"}
        )
        if not binding_bad:
            self.intent_bad_since = None
            if "intent_gap" in self.notice_kinds:
                self.notice_kinds.discard("intent_gap")
                self.notice_kinds.discard("intent_restored")
                self._notice("intent_restored", False)
        elif self.intent_bad_since is None:
            self.intent_bad_since = now
        if self.pending is not intent:
            self.pending = None
        if intent is not True:
            self.on_authorized = False
        if self.off_since is None and intent is False:
            self.off_since = now
        if self.paused or intent is None:
            self.state = "paused"
            if binding_bad and now - self.intent_bad_since >= GAP_SECONDS:
                self.state = "protection gap"
                self._notice("intent_gap", True)
        elif not self.loaded:
            self.state = "protection gap"
        elif self.pending is not None:
            self.state = (
                "waiting for camera"
                if self._waiting(desired=intent)
                else "retrying ON"
                if self.pending
                else "retrying OFF"
            )
        elif self._waiting(desired=intent):
            self.state = "waiting for camera"
        else:
            self.state = (
                "waiting for quiet"
                if self.observed is True and intent is False
                else "watching"
            )
        self._check_command_notice(now, intent, paused=paused)
        last = (
            self.report_received if self.report_received is not None else self.started
        )
        if self.silence_since is not None:
            last = max(last, self.silence_since)
        if not self.paused and intent is not None and now - last >= GAP_SECONDS:
            self.state = "protection gap"
            self._notice("camera_gap", True)
        if self.recovery_pending:
            self.state = (
                "protection gap"
                if "outcome_gap" in self.notice_kinds
                else "waiting for SDK outcome"
            )
        if (
            not self.loaded
            and not self.loading
            and self.activation_task is None
            and now >= self.activation_retry_due
        ):
            self.activation_task = self.entity.hass.async_create_task(self._activate())
        if self.notice_pending is not None and now >= self.notice_retry_due:
            self._deliver_pending()
        if (
            self.loaded
            and self.task is None
            and not self._waiting(desired=intent)
            and (
                self.recovery_pending
                or (self.pending is not None and now >= self.retry_due)
                or self._watchdog_due()
            )
        ):
            self.task = self.entity.hass.async_create_task(self._run(self.generation))
        self._arm()
        self._check_off_stuck(
            now,
            intent,
            paused or getattr(binding_state, "state", None) == "paused",
            last,
        )
        self.entity.async_write_ha_state()

    def _check_command_notice(
        self, now: float, intent: bool | None, *, paused: bool
    ) -> None:
        """Keep alert deadlines running while command admission is blocked."""
        if not self.enabled or self._stopping() or paused or intent is None:
            self.blocked_since = None
            return
        if self.pending is not None:
            self.blocked_since = None
            if now - self.pending_since >= (
                ON_ALERT_SECONDS if self.pending else OFF_ALERT_SECONDS
            ):
                self._notice(
                    "on_unconfirmed" if self.pending else "off_unconfirmed", True
                )
            return
        if not self._watchdog_is_blocked():
            self.blocked_since = None
            return
        if self.blocked_since is None:
            self.blocked_since = now
        if now - self.blocked_since >= OFF_BLOCKED_GRACE_SECONDS:
            self.state = "protection gap"
            self._notice("off_blocked", True)

    def _watchdog_is_blocked(self) -> bool:
        """Separate a blocked OFF from this light's own admitted send."""
        return (
            self._watchdog_overdue()
            and not self.off_send_in_flight
            and (
                self.watchdog_blocked
                or self._waiting()
                or self.recovery_pending
                or (
                    self.queue is not None
                    and (
                        self.queue.busy()
                        or (self.queue.lock.locked() and not self.queue_owned)
                    )
                )
            )
        )

    def _arm(self) -> None:
        if self._stopping():
            return
        if self.timer is not None:
            self.timer()

        @callback
        def tick(_: Any) -> None:
            self.timer = None
            if self.enabled and not self.loaded:
                if (
                    not self.loading
                    and self.activation_task is None
                    and monotonic() >= self.activation_retry_due
                ):
                    self.activation_task = self.entity.hass.async_create_task(
                        self._activate()
                    )
                self._deliver_pending()
                self._arm()
            else:
                self.check_policy()

        delay = TICK_SECONDS
        if self.blocked_since is not None and "off_blocked" not in self.notice_kinds:
            remaining = self.blocked_since + OFF_BLOCKED_GRACE_SECONDS - monotonic()
            if remaining > 0:
                delay = min(delay, remaining)
        self.timer = async_call_later(self.entity.hass, delay, tick)

    async def _read(self, intent: int | None = None) -> None:
        intent = self.sdk_intent if intent is None else intent
        if intent is None:
            return
        generation = self.generation
        runtime_generation = self.runtime_generation
        try:
            result = await self.entity.client.confirm_light(self.entity._sn, intent)
            if runtime_generation != self.runtime_generation:
                return
            if result.get("intent") != intent:
                return
            if (
                result.get("recoveryBusy") is False
                and intent in self.recoveries
                and monotonic() >= self.recoveries[intent]
            ):
                self.recoveries.pop(intent)
                self._recovery_settled()
            # The read has no producer timestamp. It can suppress a duplicate,
            # while only timestamped reports can finish a confirmation episode.
            if (
                generation == self.generation
                and self.sdk_intent == intent
                and result.get("confirmed") is True
                and result.get("observedValue") is self.pending
            ):
                self.retry_due = monotonic() + TICK_SECONDS
        except Exception:  # noqa: BLE001 - read failure conveys no state evidence
            self._record("read_unavailable")
            if (
                runtime_generation == self.runtime_generation
                and intent in self.recoveries
                and self.outcome_unknown_since is None
            ):
                self.outcome_unknown_since = monotonic()
                self._record("outcome_unknown", reason="recovery_watch_unavailable")

    def _begin_automatic_send(self, desired: bool) -> None:
        """Mark admission before the transport's first send await."""
        self.automatic_owned = True
        if not desired:
            self.off_send_in_flight = True
            self.blocked_since = None

    async def _run(self, generation: int) -> None:  # noqa: PLR0911, PLR0912, PLR0915
        if self.queue is None:
            self.task = None
            return
        admitted = False
        runtime_generation = self.runtime_generation
        try:
            self._check_command_notice(monotonic(), self.intent(), paused=self.paused)
            async with self.queue.lock:
                self.queue_owned = True
                if generation != self.generation or not self.enabled:
                    return
                for intent, due in tuple(self.recoveries.items()):
                    if monotonic() >= due:
                        await self._read(intent)
                if self.queue.busy():
                    return
                desired = self.pending if self.pending is not None else False
                watchdog = self.pending is None
                await self._read()
                if not watchdog and monotonic() < self.retry_due:
                    return
                delay = self.queue.next_send - monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                if self.queue.busy() or not self._allowed(
                    generation, desired, watchdog
                ):
                    self._record(
                        "dispatch_aborted", reason="policy_or_evidence_changed"
                    )
                    return
                specs = await self.entity.client.get_properties(self.entity._sn)
                if not any(
                    spec.get("name") == "light"
                    and spec.get("boundedStateWrites") is True
                    for spec in specs
                ):
                    self._record("dispatch_aborted", reason="bounded_write_unavailable")
                    return

                def guard() -> bool:
                    nonlocal admitted
                    admitted = not self.queue.busy() and self._allowed(
                        generation, desired, watchdog
                    )
                    if admitted:
                        self._begin_automatic_send(desired)
                    return admitted

                sent_at = monotonic()
                sent_wall = time()
                receipt = await self.entity.client.set_property(
                    self.entity._sn,
                    "light",
                    desired,
                    bounded=True,
                    automatic=True,
                    _send_guard=guard,
                )
                self.queue.next_send = monotonic() + SPACING_SECONDS
                if runtime_generation != self.runtime_generation:
                    return
                self.track_recovery(receipt)
                if generation != self.generation or not self.enabled:
                    return
                if not admitted or (
                    isinstance(receipt, CommandReceipt) and not receipt.send_attempted
                ):
                    self.receipt(receipt)
                    self.retry_due = monotonic() + TICK_SECONDS
                    return
                if not desired:
                    self.watchdog_blocked = False
                    self.blocked_since = None
                if watchdog:
                    self.pending = False
                    self.pending_since = monotonic()
                    self.attempts = 0
                    self.safety_off = True
                if watchdog or desired:
                    self.sent_at = sent_at
                    self.sent_wall = sent_wall
                self.attempts += 1
                delays = ON_DELAYS if desired else OFF_DELAYS
                self.retry_due = (
                    monotonic() + delays[min(self.attempts - 1, len(delays) - 1)]
                )
                self._record(
                    "controller_request", desired=desired, attempts=self.attempts
                )
                self.receipt(receipt)
                if (
                    self._fresh()
                    and self.observed is desired
                    and self._report_after(self.sent_at, self.sent_wall)
                ):
                    self._confirmed()
                self._save()
        except asyncio.CancelledError as error:
            if (
                admitted
                and getattr(error, "send_attempted", None) is not False
                and runtime_generation == self.runtime_generation
            ):
                self.uncertain_outcome()
            raise
        except Exception as error:  # noqa: BLE001 - no transport exceptions in diagnostics
            if (
                admitted
                and getattr(error, "send_attempted", None) is not False
                and runtime_generation == self.runtime_generation
            ):
                self.uncertain_outcome()
            self.retry_due = monotonic() + TICK_SECONDS
            self._record("request_unconfirmed")
        finally:
            self.queue_owned = False
            self.off_send_in_flight = False
            self.task = None
            if self.enabled and not self._stopping():
                if self.pending is None and self._watchdog_overdue():
                    self.watchdog_blocked = True
                self._check_command_notice(
                    monotonic(), self.intent(), paused=self.paused
                )
                self._arm()

    def _notice(self, kind: str, problem: bool) -> None:
        if not self.enabled or kind in self.notice_kinds:
            return
        self.notice_kinds.add(kind)
        self.notice_kind, self.notice_problem = kind, problem
        notice = {
            "kind": kind,
            "problem": problem,
            "attempts": self.attempts,
        }
        if kind == "safety_off":
            notice.update(
                on_minutes=self.confirmed_on_minutes,
                on_since=self.confirmed_on_reported_at,
            )
        superseded = {
            "now_off": {"off_unconfirmed", "off_blocked"},
            "now_on": {"on_unconfirmed"},
            "activation_restored": {"activation_gap"},
        }.get(kind)
        if superseded:
            self.notice_queue = deque(
                row for row in self.notice_queue if row["kind"] not in superseded
            )
            if self.notice_pending and self.notice_pending["kind"] in superseded:
                self.notice_retry_due = 0.0
                self.notice_pending = (
                    self.notice_queue.popleft() if self.notice_queue else None
                )
        try:
            self._publish_notice(notice)
        except Exception:  # noqa: BLE001 - retry local notice delivery separately
            self._record("notice_unavailable")
        if not notice.get("emitted") or (
            kind in PHONE_KINDS and self.options.get("light_safety_notify_service")
        ):
            if self.notice_pending is None:
                self.notice_pending = notice
            else:
                self.notice_queue.append(notice)
        self._save()
        self._deliver_pending()

    def _deliver_pending(self) -> None:
        if (
            self.notice_task is not None
            or self.notice_pending is None
            or not self.enabled
            or self._stopping()
        ):
            return
        # Reuse the policy tick and retry schedule per notice. A waiting retry
        # yields to ready notices, which retain their queue order.
        notices = [self.notice_pending, *self.notice_queue]
        now = monotonic()
        selected = next(
            (row for row in notices if row.get("retry_due", 0.0) <= now),
            min(notices, key=lambda row: row.get("retry_due", 0.0)),
        )
        self.notice_pending = selected
        self.notice_queue = deque(row for row in notices if row is not selected)
        self.notice_retry_due = selected.get("retry_due", 0.0)
        if now >= self.notice_retry_due:
            self.notice_task = self.entity.hass.async_create_task(self._deliver())

    def _publish_notice(self, notice: dict) -> tuple[str, str, str]:
        """Publish bell and event evidence independently of mobile delivery."""
        kind, attempts = notice["kind"], notice["attempts"]
        minutes = notice.get("on_minutes", 0)
        duration = f"{minutes} minute" + ("" if minutes == 1 else "s")
        tries = f"{attempts} " + ("try" if attempts == 1 else "tries")
        messages = {
            "safety_off": (
                f"Turned off after {duration} on; the schedule wanted it off."
            ),
            "now_off": f"Off now (camera confirmed after {tries}).",
            "now_on": f"On now (camera confirmed after {tries}).",
            "off_unconfirmed": (
                "Still on. OFF was sent, but the camera hasn't confirmed it "
                "after 5 minutes. The safety keeps trying."
            ),
            "off_blocked": (
                "Still on past the correction deadline. The safety has not been able "
                "to send OFF and keeps trying. Check the floodlight."
            ),
            "activation_gap": (
                "Protection gap: floodlight safety could not start. "
                "Automatic activation retries continue. Check the floodlight."
            ),
            "activation_restored": "Floodlight safety is active again.",
            "on_unconfirmed": (
                "Didn't turn on. HA asked for ON 10 minutes ago and the camera "
                "hasn't confirmed it. The safety keeps trying while the schedule "
                "wants it on."
            ),
            "camera_gap": "Protection gap: no fresh camera reports for 5 minutes.",
            "intent_gap": (
                "Protection gap: intent has been unknown or unavailable for 5 minutes."
            ),
            "outcome_gap": (
                "Protection gap: the previous SDK outcome is unavailable; "
                "automatic commands are waiting."
            ),
            "camera_restored": "Fresh camera reports are available again.",
            "intent_restored": "Intent is available again or deliberately paused.",
            "outcome_restored": "The previous SDK outcome wait has ended.",
        }
        identity = self.entity.entity_id
        state = self.entity.hass.states.get(identity)
        name = getattr(state, "name", None) or self.entity.name or identity
        restored = kind.endswith("_restored")
        gap = kind.endswith("_gap") or restored
        message = messages.get(kind, "Protection gap: confirmation unavailable.")
        if gap:
            message = f"{name}: {message}"
            title = f"Floodlight safety: {name}"
        elif kind in {"off_unconfirmed", "off_blocked", "on_unconfirmed"}:
            title = f"Floodlight problem: {name}"
        else:
            title = f"Floodlight: {name}"
        tag = f"eufy_floodlight_safety_{identity}"
        bell_tag = f"{tag}_{kind.replace('_restored', '_gap')}" if gap else tag
        if kind in PHONE_KINDS and (
            not notice.get("emitted") or self.notice_bell_kind is None
        ):
            self.notice_bell_kind = kind
        if not notice.get("emitted"):
            if restored:
                persistent_notification.async_dismiss(self.entity.hass, bell_tag)
            elif kind != "safety_off":
                persistent_notification.async_create(
                    self.entity.hass,
                    message,
                    title=title,
                    notification_id=bell_tag,
                )
            self.entity.hass.bus.async_fire(
                SAFETY_EVENT,
                {
                    "entity": identity,
                    "name": name,
                    "kind": kind,
                    "attempts": attempts,
                    "on_since": notice.get("on_since", self.on_reported_at),
                    "reported_on_minutes": notice.get("on_minutes"),
                    "camera_reported_at": self.observed_at or None,
                    "confirmation_at": self.observed_at
                    if kind in {"safety_off", "now_off", "now_on"}
                    else None,
                },
            )
            notice["emitted"] = True
        return title, message, tag

    async def _deliver(self) -> None:
        notice = self.notice_pending
        phone_attempt = False
        try:
            if notice is None or not self.enabled:
                return
            kind, problem = notice["kind"], notice["problem"]
            title, message, tag = self._publish_notice(notice)
            service = self.options.get("light_safety_notify_service")
            if service and kind in PHONE_KINDS:
                previous = notice.get("phone_attempts", 0)
                notice["phone_attempts"] = (
                    previous if type(previous) is int and previous >= 0 else 0
                ) + 1
                phone_attempt = True
                await deliver_phone_notice(
                    self.entity.hass, service, title, message, tag, problem=problem
                )
            if (
                notice.get("phone_attempts", 0) > len(NOTICE_RETRY_DELAYS) + 1
                and self.notice_pending is notice
                and self.notice_bell_kind == kind
            ):
                persistent_notification.async_create(
                    self.entity.hass, message, title=title, notification_id=tag
                )
            notice.pop("phone_attempts", None)
            if self.notice_pending is notice:
                self.notice_retry_due = 0.0
                self.notice_pending = (
                    self.notice_queue.popleft() if self.notice_queue else None
                )
            self._save()
        except Exception:  # noqa: BLE001 - persistent notice is the delivery fallback
            self._record("notify_failed", reason="service_unavailable")
            if self.notice_pending is notice:
                attempts = notice.get("phone_attempts", 0)
                if (
                    phone_attempt
                    and attempts >= len(NOTICE_RETRY_DELAYS) + 1
                    and self.notice_bell_kind == kind
                ):
                    # Update the existing bell without repeating the notice event.
                    try:
                        persistent_notification.async_create(
                            self.entity.hass,
                            f"{message} {PHONE_FAILURE_SENTENCE}",
                            title=title,
                            notification_id=tag,
                        )
                    except Exception:  # noqa: BLE001 - preserve the pending retry
                        self._record("notice_unavailable")
                delay = notice_retry_delay(attempts)
                notice["retry_due"] = self.notice_retry_due = monotonic() + delay
                if self.notice_queue:
                    self.notice_pending = self.notice_queue.popleft()
                    self.notice_queue.append(notice)
            self._save()
        finally:
            self.notice_task = None
            self._deliver_pending()

    def stop(self, state: str) -> None:
        """Revoke unsent authority without touching the existing display timers."""
        self.blocked_since = None
        self._revoke_automatic()
        self.generation += 1
        self.state = state
        if state == "closed":
            self.stopped = True
            if self.start_unsub is not None:
                self.start_unsub()
                self.start_unsub = None
        if state == "disabled":
            self.waiting_since = None
            self._close_off_stuck("disabled")
        if self.timer is not None:
            self.timer()
            self.timer = None
        if self.pause_unsub is not None:
            self.pause_unsub()
            self.pause_unsub = None
        if self.task is not None and self.task is not asyncio.current_task():
            self.task.cancel()
        self.task = None
        if (
            self.activation_task is not None
            and self.activation_task is not asyncio.current_task()
        ):
            self.activation_task.cancel()
        self.activation_task = None
        self.on_authorized = False
        if self.entity._removed:
            getattr(
                self.entity.coordinator, "_light_safety_controllers", set()
            ).discard(self)
            if self.queue is not None:
                self.queue.controllers.discard(self)

    @property
    def attributes(self) -> dict:
        """Expose policy separately from command and display state."""
        result = {}
        if self.health_projection is not None:
            result["command_health"] = (
                "command_degraded" if monotonic() < self.health_until else "healthy"
            )
            # An absolute end time, not a countdown: a countdown differs on every
            # write and turns each coordinator update into a recorder row.
            result["command_health_cooldown_until_ms"] = (
                self.health_until_ms if monotonic() < self.health_until else None
            )
        if self.enabled:
            result.update(
                {
                    "floodlight_safety_status": self.state,
                    "floodlight_safety_attempts": self.attempts,
                    "floodlight_safety_intent_source": "binding"
                    if self.binding
                    else "ha_command",
                    "floodlight_safety_intent": self.intent(),
                    "floodlight_safety_camera_reported_value": self.observed,
                    "off_stuck_incident": (self.off_stuck or {}).get("incident_id"),
                    "off_stuck_reason": (self.off_stuck or {}).get("reason"),
                    "off_stuck_escalate_at": (self.off_stuck or {}).get("escalate_at"),
                    "off_stuck_opened_at": (self.off_stuck or {}).get("opened_at"),
                }
            )
        return result
