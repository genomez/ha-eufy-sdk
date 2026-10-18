"""Bound optional light corrections to the latest explicit HA command."""

# This controller shares the entity's lifecycle and private target identity.
# ruff: noqa: SLF001

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.helpers.event import async_call_later

from .api import CommandReceipt, EufySdkApiClientError
from .const import CONF_LIGHT_CORRECTION, CONF_LIGHT_CORRECTION_PAUSE

WINDOW_SECONDS = 600
CORRECTION_DELAYS = (15, 60, 300)
FAILURE_EVENT = "eufy_sdk_light_correction_failed"
MAX_EARLY_EVENTS = 16
MIN_CORRECTION_SPACING = 45
FINAL_CONFIRMATION_DELAY = 15


class LightCorrection:
    """One monotonic lineage, one task, and at most three corrective writes."""

    def __init__(self, entity: Any) -> None:
        """Bind correction ownership to the entity lifecycle."""
        self.entity = entity
        self.generation = 0
        self.intent: int | None = None
        self.desired: bool | None = None
        self.born = 0.0
        self.first_contrary: float | None = None
        self.attempts = 0
        self.state = "idle"
        self.observed: bool | None = None
        self.recovery_busy = False
        self.failed = False
        self.pending_final = False
        self.transition = False
        self.early: list[dict] = []
        self.timer = None
        self.timer_generation = 0
        self.expiry = None
        self.task: asyncio.Task | None = None
        self.pause_unsub = None
        self.revision = 0
        self.last_event: dict | None = None
        self.last_correction: float | None = None
        self.final_confirmation_due = 0.0
        self.last_contrary_at: str | None = None
        self.window_until: str | None = None
        self.pause_requested = False

    @property
    def enabled(self) -> bool:
        """Require an explicit option and complete live protocol support."""
        return (
            getattr(self.entity.coordinator.config_entry, "options", {}).get(
                CONF_LIGHT_CORRECTION, False
            )
            is True
            and self.entity.client.late_contrary_effects_supported is True
        )

    @property
    def paused(self) -> bool:
        """A configured unavailable pause helper fails closed until it is off."""
        helper = getattr(self.entity.coordinator.config_entry, "options", {}).get(
            CONF_LIGHT_CORRECTION_PAUSE
        )
        if not helper:
            return False
        state = self.entity.hass.states.get(helper)
        return state is None or state.state != "off"

    def start(self, value: Any) -> None:
        """Every explicit command immediately revokes its predecessor's authority."""
        self.stop("superseded")
        self.intent = None
        self.desired = value if type(value) is bool else None
        self.born = monotonic()
        self.window_until = (
            datetime.now(UTC) + timedelta(seconds=WINDOW_SECONDS)
        ).isoformat()
        self.first_contrary = None
        self.attempts = 0
        self.last_correction = None
        self.last_contrary_at = None
        self.observed = None
        self.recovery_busy = False
        self.failed = self.pending_final = False
        self.pause_requested = False
        self.state = "watching"
        generation = self.generation
        self.expiry = async_call_later(
            self.entity.hass,
            WINDOW_SECONDS,
            lambda _: self.stop("expired") if generation == self.generation else None,
        )
        if self.pause_unsub is None:
            self.pause_unsub = self.entity.hass.bus.async_listen(
                "state_changed", self._pause_changed
            )

    def _pause_changed(self, event: Any) -> None:
        helper = getattr(self.entity.coordinator.config_entry, "options", {}).get(
            CONF_LIGHT_CORRECTION_PAUSE
        )
        if helper and event.data.get("entity_id") == helper:
            if self.paused and not self.pause_requested:
                self.pause_requested = True
                self.entity.client.pause_light_correction(self.entity._sn)
            self._schedule()

    def receipt(
        self, receipt: CommandReceipt | None, *, correction: bool = False
    ) -> None:
        """Map a fresh bounded receipt without renewing the lineage or budget."""
        paused_correction = (
            correction
            and self.pause_requested
            and isinstance(receipt, CommandReceipt)
            and receipt.status in {"cancelled_before_send", "cancelled_after_send"}
        )
        if (
            not isinstance(receipt, CommandReceipt)
            or (not receipt.send_attempted and not paused_correction)
            or receipt.superseded
            or type(receipt.intent) is not int
            or receipt.intent <= 0
            or (
                receipt.status
                not in {"acknowledged", "pending_accepted", "ack_timeout"}
                and not paused_correction
            )
        ):
            self.stop("unavailable")
            return
        self.intent = receipt.intent
        self.transition = False
        events, self.early = self.early, []
        for event in events:
            self.event(event)

    def event(self, event: dict) -> None:
        """Only SDK-confirmed structured events can arm corrective work."""
        self.last_event = event.copy()
        if not self._live():
            self.entity.async_write_ha_state()
            return
        if self.intent is None or self.transition:
            if event["intent"] == self.intent:
                # During the clock exchange the old SDK intent still owns the
                # target. A new match must revoke the unsent corrective write,
                # while its superseded event may be our own replacement.
                self.recovery_busy = event["recoveryBusy"]
                if event["state"] == "matched":
                    self.state = "matched"
                    self.observed = self.desired
                    self.pending_final = False
                    self.revision += 1
                    self._cancel_timer()
                    self.pause_requested = True
                    self.entity.client.pause_light_correction(self.entity._sn)
            if len(self.early) < MAX_EARLY_EVENTS:
                self.early.append(event.copy())
            return
        if event["intent"] != self.intent or event["desiredValue"] is not self.desired:
            return
        self.recovery_busy = event["recoveryBusy"]
        self.revision += 1
        state = event["state"]
        if state in {"expired", "superseded", "closed"}:
            self.stop(state)
            return
        self.observed = event.get("observedValue")
        if state == "matched" or self.observed is self.desired:
            self.state = "matched"
            self.pending_final = False
            self._cancel_timer()
        elif state == "contrary":
            self.last_contrary_at = datetime.now(UTC).isoformat()
            if self.first_contrary is None:
                self.first_contrary = monotonic()
            self.state = "contrary"
            if self.attempts == len(CORRECTION_DELAYS):
                self.pending_final = True
            self._schedule()
        elif state == "recovery_settled":
            self._schedule()
        self.entity.async_write_ha_state()

    def _live(self) -> bool:
        return (
            self.desired is not None
            and self.state
            not in {"expired", "superseded", "closed", "unavailable", "failed"}
            and not self.entity._removed
            and monotonic() < self.born + WINDOW_SECONDS
            and self.entity.client.late_contrary_effects_supported is True
        )

    def _cancel_timer(self) -> None:
        self.timer_generation += 1
        if self.timer is not None:
            self.timer()
            self.timer = None

    def _schedule(self) -> None:
        self._cancel_timer()
        if not self._live() or self.first_contrary is None or self.state == "matched":
            return
        if not self.enabled:
            return
        if self.paused:
            self.state = "paused"
            return
        if self.recovery_busy:
            self.state = "recovery_busy"
            return
        if self.task is not None and not self.task.done():
            return
        if self.attempts >= len(CORRECTION_DELAYS) and not self.pending_final:
            return
        due = (
            self.final_confirmation_due
            if self.pending_final
            else self.first_contrary + CORRECTION_DELAYS[self.attempts]
        )
        if not self.pending_final and self.last_correction is not None:
            due = max(due, self.last_correction + MIN_CORRECTION_SPACING)
        generation = self.generation
        timer_generation = self.timer_generation

        def run(_: Any) -> None:
            if timer_generation != self.timer_generation:
                return
            self.timer = None
            if (
                generation == self.generation
                and self._live()
                and (self.task is None or self.task.done())
            ):
                self.timer_generation += 1
                self.task = self.entity.hass.async_create_task(self._run(generation))

        self.timer = async_call_later(self.entity.hass, max(0, due - monotonic()), run)

    async def _read(self, generation: int) -> dict | None:  # noqa: PLR0911
        intent = self.intent
        revision = self.revision
        result = await self.entity.client.confirm_light(self.entity._sn, intent)
        if generation != self.generation or not self._live() or intent != self.intent:
            return None
        if result["desiredValue"] is not self.desired:
            self.stop("unavailable")
            return None
        if revision != self.revision and (
            self.state == "matched" or self.recovery_busy
        ):
            return None
        self.recovery_busy = result["recoveryBusy"]
        if self.recovery_busy:
            self.state = "recovery_busy"
            return None
        if not result["confirmed"] or type(result.get("observedValue")) is not bool:
            self.state = "unconfirmed"
            return None
        self.observed = result["observedValue"]
        if self.observed is self.desired:
            self.state = "matched"
            self.pending_final = False
            return None
        return result

    async def _run(self, generation: int) -> None:  # noqa: PLR0912
        previous_correction = self.last_correction
        attempt_started = False
        try:
            if not self.enabled or self.paused or not self._live():
                return
            result = await self._read(generation)
            if (
                result is None
                or self.paused
                or not self.enabled
                or self.state == "matched"
            ):
                return
            if self.pending_final:
                self._failure()
                return
            if self.attempts >= len(CORRECTION_DELAYS):
                return
            self.attempts += 1
            attempt_started = True
            self.last_correction = monotonic()
            self.state = "correcting"
            self.transition = True
            self.pause_requested = False
            receipt = await self.entity.client.set_property(
                self.entity._sn,
                "light",
                self.desired,
                bounded=True,
                correction=True,
                _send_guard=lambda: (
                    generation == self.generation
                    and self._live()
                    and self.enabled
                    and not self.paused
                    and not self.recovery_busy
                    and self.state != "matched"
                ),
            )
            if generation != self.generation or not self._live():
                return
            # Completion is a conservative spacing anchor after any queue and
            # ACK wait. Admission time alone cannot bound the physical send gap.
            self.last_correction = monotonic()
            self.receipt(receipt, correction=True)
            if (
                isinstance(receipt, CommandReceipt)
                and receipt.status == "cancelled_before_send"
                and not receipt.send_attempted
                and self.pause_requested
            ):
                self.attempts -= 1
                self.last_correction = previous_correction
            if not self._live() or self.state == "matched":
                return
            self.state = "contrary"
            if self.attempts == len(CORRECTION_DELAYS):
                self.pending_final = True
                # Allow settling after the receipt before the final fresh read.
                # Matching SDK evidence can cancel this timer in the meantime.
                self.final_confirmation_due = monotonic() + FINAL_CONFIRMATION_DELAY
        except EufySdkApiClientError as error:
            if generation == self.generation and self._live():
                paused_before_send = str(error) == "light_correction_paused_before_send"
                if paused_before_send and attempt_started:
                    self.attempts -= 1
                    self.last_correction = previous_correction
                elif attempt_started:
                    self.last_correction = monotonic()
                if self.state != "matched":
                    self.state = (
                        "recovery_busy"
                        if self.recovery_busy
                        else "paused"
                        if paused_before_send
                        else "read_or_write_failed"
                    )
        finally:
            if generation == self.generation:
                self.transition = False
                self.task = None
                # Failed/unconfirmed reads wait for new SDK evidence instead of
                # creating a zero-delay read loop at an overdue offset.
                if self.state in {"contrary", "paused"}:
                    self._schedule()
                self.entity.async_write_ha_state()

    def _failure(self) -> None:
        if self.failed or not self._live() or self.observed is self.desired:
            return
        self.failed = True
        self.state = "failed"
        self.pending_final = False
        self._cancel_timer()
        entity_id = self.entity.entity_id
        persistent_notification.async_create(
            self.entity.hass,
            f"{entity_id} still reports the opposite light state "
            "after three corrections.",
            title="Eufy light correction failed",
            notification_id=f"eufy_light_correction_{self.entity.unique_id}",
        )
        self.entity.hass.bus.async_fire(
            FAILURE_EVENT,
            {
                "entity_id": entity_id,
                "desired_value": self.desired,
                "observed_value": self.observed,
                "correction_attempts": self.attempts,
            },
        )

    def stop(self, state: str) -> None:
        """Revoke timers and in-flight work; callbacks cannot revive a lineage."""
        self.generation += 1
        self.state = state
        self._cancel_timer()
        if self.expiry is not None:
            self.expiry()
            self.expiry = None
        if self.task is not None and self.task is not asyncio.current_task():
            self.task.cancel()
        self.task = None
        self.transition = False
        self.early.clear()
        if state == "closed" and self.pause_unsub is not None:
            self.pause_unsub()
            self.pause_unsub = None

    @property
    def attributes(self) -> dict:
        """Keep contrary detection separate from command receipt/state presentation."""
        attributes = {
            "light_correction_enabled": self.enabled,
            "light_correction_state": self.state,
            "light_correction_attempts": self.attempts,
            "light_correction_desired_value": self.desired,
            "light_correction_observed_value": self.observed,
            "light_correction_recovery_busy": self.recovery_busy,
            "light_correction_failed": self.failed,
            "last_contrary_at": self.last_contrary_at,
            "correction_window_until": self.window_until,
        }
        if self.last_event is not None:
            attributes.update(
                {
                    "light_contrary_observation_state": self.last_event["state"],
                    "light_contrary_observed_value": self.last_event.get(
                        "observedValue"
                    ),
                    "light_contrary_desired_value": self.last_event["desiredValue"],
                    "light_contrary_observation_age_ms": self.last_event["ageMs"],
                }
            )
        return attributes
