"""Opt-in floodlight safety, independent of optimistic entity presentation."""

# The controller shares the property's private lifecycle and target identity.
# ruff: noqa: SLF001, FBT001, FBT003

from __future__ import annotations

import asyncio
import math
from collections import deque
from time import monotonic, time
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.core import callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store

from .api import CommandReceipt, command_health, command_health_is_current
from .const import CONF_LIGHT_CORRECTION, CONF_LIGHT_CORRECTION_PAUSE

SAFETY_EVENT = "eufy_sdk_floodlight_safety"
QUIET_SECONDS = 900
CAP_SECONDS = 1800
OFF_DWELL_SECONDS = 120
GAP_SECONDS = 300
OFF_ALERT_SECONDS = 300
ON_ALERT_SECONDS = 600
REPORT_FRESH_SECONDS = 120
OFF_DELAYS = (60, 120, 300)
ON_DELAYS = (30, 60, 120, 300)
SPACING_SECONDS = 3
TICK_SECONDS = 15
RECOVERY_SECONDS = 45
# SDK late-effect work ends at sentAt + 45 s; the bounded caller lasts 80 s
# (api.BOUNDED_CALLER_SECONDS). Allow a further conservative settlement margin.
OUTCOME_SETTLE_SECONDS = 180
NOTICE_RETRY_DELAYS = (60, 120, 300, 600)
PHONE_FAILURE_SENTENCE = (
    "Phone delivery failed; check the notification service in the integration options."
)
PHONE_KINDS = frozenset({"off_unconfirmed", "now_off", "on_unconfirmed", "now_on"})
NOTICE_KINDS = frozenset(
    {
        "safety_off",
        "now_off",
        "now_on",
        "off_unconfirmed",
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
        self.off_since: float | None = None
        self.intent_bad_since: float | None = None
        self.observed: bool | None = None
        self.observed_at = 0.0
        self.report_floor = time()
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
        self.timer = None
        self.task: asyncio.Task | None = None
        self.notice_task: asyncio.Task | None = None
        self.pause_unsub = None
        self.queue: HubQueue | None = None
        self.store: Store | None = None
        self.loaded = False
        self.loading = False
        self.bound_entity: str | None = None
        self.started = monotonic()
        self.notice_kind: str | None = None
        self.notice_kinds: set[str] = set()
        self.notice_problem = False
        self.notice_pending: dict | None = None
        self.notice_queue: deque[dict] = deque()
        self.notice_retry_due = 0.0
        self.journal: deque[dict] = deque(maxlen=80)

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
        if not self.enabled or self.loading:
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
            saved = await self.store.async_load() if not self.loaded else None
            if not self.enabled:
                return
            if isinstance(saved, dict):
                if not self.on_authorized and self.pending is None:
                    self.last_command = saved.get("last_command") is True
                    if saved.get("pending_off") is True:
                        self.pending = False
                        self.pending_since = monotonic()
                        self.sent_at = time()
                        self.retry_due = monotonic() + OFF_DELAYS[0]
                        attempts = saved.get("attempts", 0)
                        self.attempts = (
                            attempts if type(attempts) is int and attempts >= 0 else 0
                        )
                        self.safety_off = saved.get("safety_off") is True
                self.notice_kind = saved.get("notice_kind")
                kinds = saved.get("notice_kinds", [])
                if isinstance(kinds, list):
                    self.notice_kinds = {kind for kind in kinds if kind in NOTICE_KINDS}
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
            self.check_policy()
        except Exception:  # noqa: BLE001 - never retain raw storage exceptions
            self.loaded = False
            self.state = "protection gap"
            self._record("storage_unavailable")
        finally:
            self.loading = False

    def _save(self) -> None:
        if self.store is not None and self.loaded:
            self.store.async_delay_save(
                lambda: {
                    "last_command": self.last_command,
                    "pending_off": self.pending is False,
                    "attempts": self.attempts if self.pending is False else 0,
                    "safety_off": self.safety_off,
                    "notice_kind": self.notice_kind,
                    "notice_kinds": sorted(self.notice_kinds),
                    "notice_problem": self.notice_problem,
                    "notice_pending": self.notice_pending,
                    "notice_queue": list(self.notice_queue),
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
            self.notice_kinds.difference_update(PHONE_KINDS | {"safety_off"})
            self.notice_problem = False
        self.last_command = value
        self.on_authorized = value
        self.explicit_busy += 1
        self.pending = value
        self.pending_since = monotonic()
        self.sent_at = time()
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
        if self.enabled:
            self._expire_outcomes()
        return bool(self.recoveries) or self.outcome_unknown_since is not None

    def _expire_outcomes(self) -> None:
        now = monotonic()
        if (
            self.outcome_unknown_since is not None
            and now - self.outcome_unknown_since >= OUTCOME_SETTLE_SECONDS
        ):
            self.receipt_unknown = False
            self.outcome_unknown_since = None
            self._record("outcome_wait_expired")
        for intent, due in tuple(self.recoveries.items()):
            if now >= due + OUTCOME_SETTLE_SECONDS:
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
        self.health_projection = health
        self.health_until = until if health["status"] == "command_degraded" else 0.0
        self.check_policy()

    def reset_health(self) -> None:
        """Accept new runtime revisions while retaining a known cooldown."""
        self.health_projection = None

    def reset_observations(self) -> None:
        """Require a fresh report and new observed ON age after reconnect."""
        self.report_floor = time()
        self.observed = None
        self.report_received = self.on_since = None
        self.on_reported_at = None
        self.motion_known = False
        self.motion_active = False
        self.check_policy()

    def observe(self, event: dict) -> None:
        """Only advancing, timestamped RTC reports qualify as camera evidence."""
        if not self.enabled:
            return
        stamp, values = event.get("observedAtMs"), event.get("properties")
        if (
            event.get("source") != "rtc"
            or not isinstance(values, dict)
            or type(stamp) not in (int, float)
            or not math.isfinite(stamp)
            or not 0 <= time() - stamp / 1000 <= REPORT_FRESH_SECONDS
            or stamp / 1000 <= max(self.report_floor, self.observed_at)
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
                if self.observed is False and self.pending is None:
                    self.notice_kinds.difference_update(PHONE_KINDS | {"safety_off"})
                    self.notice_problem = False
                self.on_since = monotonic()
                self.on_reported_at = stamp / 1000
            elif not value:
                if self.on_since is not None:
                    self.confirmed_on_minutes = int((monotonic() - self.on_since) / 60)
                    self.confirmed_on_reported_at = self.on_reported_at
                self.on_since = None
                self.on_reported_at = None
            self.observed, self.observed_at = value, stamp / 1000
            self.report_received = monotonic()
            if "camera_gap" in self.notice_kinds:
                self.notice_kinds.discard("camera_gap")
                self.notice_kinds.discard("camera_restored")
                self._notice("camera_restored", False)
            self._record("report", value=value, reported_at=self.observed_at)
            if self.pending is value and self.observed_at > self.sent_at:
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
        self.pending = None
        problem = (
            "on_unconfirmed" if desired else "off_unconfirmed"
        ) in self.notice_kinds
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
            and 0 <= time() - self.observed_at <= REPORT_FRESH_SECONDS
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
        now = monotonic()
        return (
            self.intent() is False
            and self.observed is True
            and self._fresh()
            and self.on_since is not None
            and self.off_since is not None
            and now - self.off_since >= OFF_DWELL_SECONDS
            and (
                now - self.on_since >= CAP_SECONDS
                or (now - self.on_since >= QUIET_SECONDS and self._quiet())
            )
        )

    def _waiting(self) -> bool:
        health = self.entity.device.get("cameraHealth") or {}
        return (
            self.paused
            or monotonic() < self.health_until
            or getattr(self.entity.client, "connected", False) is not True
            or health.get("connectivity") == "offline"
            or health.get("parentTransport") == "disconnected"
        )

    def _allowed(self, generation: int, desired: bool, watchdog: bool) -> bool:
        return (
            generation == self.generation
            and self.enabled
            and self.loaded
            and not self._waiting()
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
        if not self.enabled:
            return
        now, intent = monotonic(), self.intent()
        self._expire_outcomes()
        paused = self.paused
        if intent is not self.current_intent:
            self._revoke_automatic()
            self.generation += 1
            self.current_intent = intent
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
        elif self._waiting():
            self.state = "waiting for camera"
        elif self.pending is not None:
            self.state = "retrying ON" if self.pending else "retrying OFF"
            if now - self.pending_since >= (
                ON_ALERT_SECONDS if self.pending else OFF_ALERT_SECONDS
            ):
                self._notice(
                    "on_unconfirmed" if self.pending else "off_unconfirmed", True
                )
        else:
            self.state = (
                "waiting for quiet"
                if self.observed is True and intent is False
                else "watching"
            )
        last = (
            self.report_received if self.report_received is not None else self.started
        )
        if not self.paused and intent is not None and now - last >= GAP_SECONDS:
            self.state = "protection gap"
            self._notice("camera_gap", True)
        if self.recovery_pending:
            self.state = "waiting for SDK outcome"
            if (
                self.outcome_unknown_since is not None
                and now - self.outcome_unknown_since > OUTCOME_SETTLE_SECONDS
            ):
                self.state = "protection gap"
                self._notice("outcome_gap", True)
        if self.notice_pending is not None and now >= self.notice_retry_due:
            self._deliver_pending()
        if (
            self.loaded
            and self.task is None
            and not self._waiting()
            and (
                self.recovery_pending
                or (self.pending is not None and now >= self.retry_due)
                or self._watchdog_due()
            )
        ):
            self.task = self.entity.hass.async_create_task(self._run(self.generation))
        self._arm()
        self.entity.async_write_ha_state()

    def _arm(self) -> None:
        if self.timer is not None:
            self.timer()

        @callback
        def tick(_: Any) -> None:
            self.timer = None
            self.check_policy()

        self.timer = async_call_later(self.entity.hass, TICK_SECONDS, tick)

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

    async def _run(self, generation: int) -> None:  # noqa: PLR0911, PLR0912, PLR0915
        if self.queue is None:
            self.task = None
            return
        admitted = False
        runtime_generation = self.runtime_generation
        try:
            async with self.queue.lock:
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
                        self.automatic_owned = True
                    return admitted

                sent_at = time()
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
                self.receipt(receipt)
                if not admitted or (
                    isinstance(receipt, CommandReceipt) and not receipt.send_attempted
                ):
                    self.retry_due = monotonic() + TICK_SECONDS
                    return
                if watchdog:
                    self.pending = False
                    self.pending_since = monotonic()
                    self.attempts = 0
                    self.safety_off = True
                self.sent_at = sent_at
                self.attempts += 1
                delays = ON_DELAYS if desired else OFF_DELAYS
                self.retry_due = (
                    monotonic() + delays[min(self.attempts - 1, len(delays) - 1)]
                )
                self._record(
                    "controller_request", desired=desired, attempts=self.attempts
                )
                if (
                    self._fresh()
                    and self.observed is desired
                    and self.observed_at > self.sent_at
                ):
                    self._confirmed()
                self._save()
        except asyncio.CancelledError:
            if admitted and runtime_generation == self.runtime_generation:
                self.uncertain_outcome()
            raise
        except Exception:  # noqa: BLE001 - no transport exceptions in diagnostics
            if admitted and runtime_generation == self.runtime_generation:
                self.uncertain_outcome()
            self.retry_due = monotonic() + TICK_SECONDS
            self._record("request_unconfirmed")
        finally:
            self.task = None
            if self.enabled:
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
        superseded = {"now_off": "off_unconfirmed", "now_on": "on_unconfirmed"}.get(
            kind
        )
        if superseded:
            self.notice_queue = deque(
                row for row in self.notice_queue if row["kind"] != superseded
            )
            if self.notice_pending and self.notice_pending["kind"] == superseded:
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
            self.notice_task is None
            and self.notice_pending is not None
            and self.loaded
            and monotonic() >= self.notice_retry_due
        ):
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
        elif kind in {"off_unconfirmed", "on_unconfirmed"}:
            title = f"Floodlight problem: {name}"
        else:
            title = f"Floodlight: {name}"
        tag = f"eufy_floodlight_safety_{identity}"
        bell_tag = f"{tag}_{kind.replace('_restored', '_gap')}" if gap else tag
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
                domain, action = service.split(".", 1)
                data = {"tag": tag, "channel": "Eufy"}
                if problem:
                    data.update({"importance": "high", "sticky": True})
                await self.entity.hass.services.async_call(
                    domain,
                    action,
                    {
                        "title": title,
                        "message": message,
                        "data": data,
                    },
                    blocking=True,
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
                if phone_attempt and attempts >= len(NOTICE_RETRY_DELAYS) + 1:
                    self._record("notify_gave_up", attempts=attempts)
                    # Update the existing bell without repeating the notice event.
                    try:
                        persistent_notification.async_create(
                            self.entity.hass,
                            f"{message} {PHONE_FAILURE_SENTENCE}",
                            title=title,
                            notification_id=tag,
                        )
                    except Exception:  # noqa: BLE001 - keep the phone cap if HA fails
                        self._record("notice_unavailable")
                    self.notice_retry_due = 0.0
                    self.notice_pending = (
                        self.notice_queue.popleft() if self.notice_queue else None
                    )
                else:
                    delay = NOTICE_RETRY_DELAYS[
                        min(max(0, attempts - 1), len(NOTICE_RETRY_DELAYS) - 1)
                    ]
                    self.notice_retry_due = monotonic() + delay
            self._save()
        finally:
            self.notice_task = None
            if self.notice_pending is not notice:
                self._deliver_pending()

    def stop(self, state: str) -> None:
        """Revoke unsent authority without touching the existing display timers."""
        self._revoke_automatic()
        self.generation += 1
        self.state = state
        if self.timer is not None:
            self.timer()
            self.timer = None
        if self.pause_unsub is not None:
            self.pause_unsub()
            self.pause_unsub = None
        if self.task is not None and self.task is not asyncio.current_task():
            self.task.cancel()
        self.task = None
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
            result["command_health_cooldown_remaining_ms"] = max(
                0, (self.health_until - monotonic()) * 1000
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
                }
            )
        return result
