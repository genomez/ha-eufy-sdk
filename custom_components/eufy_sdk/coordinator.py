"""DataUpdateCoordinator for eufy_sdk — owns the bridge connection + the device list."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from homeassistant.core import callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    MAX_COMMAND_HEALTH_TARGETS,
    EufySdkApiClientAuthenticationError,
    EufySdkApiClientCommunicationError,
    EufySdkApiClientError,
    command_health,
    command_health_event,
    command_health_is_current,
)

if TYPE_CHECKING:
    from .data import EufySdkConfigEntry


class EufySdkDataUpdateCoordinator(DataUpdateCoordinator[dict[str, dict]]):
    """Keep the bridge connected and expose the device list as `{sn: device}`."""

    config_entry: EufySdkConfigEntry

    # Anker Solix devices (separate account/backend), kept apart from the eufy `data`
    # so the eufy platforms never iterate them: `{sn: {productCode, name, category,
    # capabilities, values, ...}}`. Empty unless the bridge has SOLIX_* configured;
    # reassigned per-update, so the class-level {} is only an initial fallback. Live
    # values arrive via `solixReading` events.
    solix_devices: ClassVar[dict[str, dict]] = {}

    property_observations: dict | None = None

    # A queued refresh request is not a completed snapshot. Keep receipt order
    # separately so entities can reconcile without bypassing HA's debouncer.
    _command_health_generation: int = 0
    snapshot_generation: int = 0
    completed_snapshot_generation: int = 0

    _FAST_RETRY_S = 45
    _fast_retry_cancel = None
    _fast_retry_used = False

    @callback
    def _schedule_fast_retry(self) -> None:
        """Allow one quick retry per failure episode, without cloud login."""
        if self._fast_retry_cancel is not None or self._fast_retry_used:
            return
        self._fast_retry_used = True
        self._fast_retry_cancel = async_call_later(
            self.hass, self._FAST_RETRY_S, self._fast_retry_fire
        )

    @callback
    def _fast_retry_fire(self, _now: object) -> None:
        """Request a refresh once; regular polling owns later attempts."""
        self._fast_retry_cancel = None
        self.hass.async_create_task(self.async_request_refresh())

    @callback
    def _clear_fast_retry(self) -> None:
        """Cancel the pending timer on recovery, challenge or unload."""
        if self._fast_retry_cancel is not None:
            self._fast_retry_cancel()
            self._fast_retry_cancel = None

    def record_state_event(self, serial: str, values: dict) -> None:
        """Protect only events received while a snapshot request is in progress."""
        pending = getattr(self, "_pending_state_events", None)
        if pending is not None:
            pending.setdefault(serial, {}).update(values)

    def reset_command_health(self) -> None:
        """Allow a replacement SDK runtime to restart its health revisions."""
        self._command_health_generation += 1
        self._command_health_projections: dict[str, dict] = {}
        if getattr(self, "_pending_command_health", None) is not None:
            self._pending_command_health.clear()
        for device in (self.data or {}).values():
            device.pop("commandHealth", None)

    def _project_command_health(self, serial: str, health: dict | None) -> dict | None:
        """Keep the newest revision across events, receipts, and snapshots."""
        cache = getattr(self, "_command_health_projections", None)
        if cache is None:
            self._command_health_projections = cache = {}
        previous = cache.get(serial)
        health = command_health(health)
        if health is not None and command_health_is_current(health, previous):
            if previous is None and len(cache) >= MAX_COMMAND_HEALTH_TARGETS:
                return None
            cache[serial] = health
            return health
        return previous

    def apply_command_health_event(self, event: dict) -> None:
        """Validate SDK health before updating the cache or pending snapshot."""
        if event.get("event") in {"bridgeDisconnected", "ready"}:
            self.reset_command_health()
            return
        event = command_health_event(event)
        if event is not None and event["sn"] in (self.data or {}):
            self.record_command_health(event["sn"], command_health(event))

    def record_command_health(self, serial: str, health: dict) -> None:
        """Retain ordered health events that race an in-progress snapshot."""
        health = self._project_command_health(serial, health)
        if health is None:
            return
        pending = getattr(self, "_pending_command_health", None)
        if pending is not None:
            pending[serial] = health
        if serial in (self.data or {}):
            self.data[serial]["commandHealth"] = health
            self.async_update_listeners()

    def _project_snapshot_health(self, snapshot: dict, generation: int) -> None:
        """Apply health ordering separately from ordinary snapshot state."""
        for serial, device in snapshot.items():
            raw_health = device.pop("commandHealth", None)
            health = self._project_command_health(
                serial,
                raw_health if generation == self._command_health_generation else None,
            )
            if health is not None:
                device["commandHealth"] = health

    async def _async_update_data(self) -> dict[str, dict]:
        """Journal concurrent events until the replacement snapshot is ready."""
        self.snapshot_generation += 1
        generation = self.snapshot_generation
        health_generation = self._command_health_generation
        self._pending_state_events: dict[str, dict] | None = {}
        self._pending_command_health: dict[str, dict] | None = {}
        try:
            snapshot = await self._read_snapshot()
            self._project_snapshot_health(snapshot, health_generation)
            # Alarm lifecycle state belongs to this loaded entry and its bounded
            # stop/auto-clear timers. An ordinary list omitting it is not a stop.
            for serial, device in snapshot.items():
                previous = (self.data or {}).get(serial, {}).get("state", {})
                state = device.setdefault("state", {})
                for key in ("alarmTriggered", "alarmPending", "alarmType"):
                    if key not in state and key in previous:
                        state[key] = previous[key]
            for serial, values in self._pending_state_events.items():
                if serial in snapshot:
                    snapshot[serial].setdefault("state", {}).update(values)
            for serial, health in self._pending_command_health.items():
                if serial in snapshot:
                    snapshot[serial]["commandHealth"] = self._project_command_health(
                        serial, health
                    )
        except ConfigEntryAuthFailed:
            self._clear_fast_retry()
            raise
        except UpdateFailed:
            self._schedule_fast_retry()
            raise
        else:
            self._clear_fast_retry()
            self._fast_retry_used = False
            self.completed_snapshot_generation = generation
            return snapshot
        finally:
            self._pending_state_events = None
            self._pending_command_health = None

    async def _read_snapshot(self) -> dict[str, dict]:
        """Ensure the connection is up, confirm we're authed, and return the devices."""
        client = self.config_entry.runtime_data.client
        try:
            if not client.connected:
                await client.connect()
            auth = await client.auth_status()
            state = auth.get("state")
            if state in ("require_2fa", "require_captcha"):
                # Genuinely needs the user — start the reauth flow.
                msg = "bridge_auth_challenge_required"
                raise ConfigEntryAuthFailed(msg)
            if state != "ok":
                # Transient: the bridge is still booting/logging in ("pending" after a
                # restart). Retry next interval instead of freezing the entry in reauth;
                # one boot-window poll must not stop updates indefinitely.
                msg = "bridge_auth_not_ready"
                raise UpdateFailed(msg)
            devices = await client.list_devices()
        except EufySdkApiClientAuthenticationError:
            msg = "bridge_authentication_failed"
            raise ConfigEntryAuthFailed(msg) from None
        except EufySdkApiClientCommunicationError:
            await client.reset_connection()
            msg = "bridge_update_failed"
            raise UpdateFailed(msg) from None
        except EufySdkApiClientError:
            msg = "bridge_update_failed"
            raise UpdateFailed(msg) from None
        # Solix is optional + independent: a hiccup must not fail the eufy update.
        try:
            solix = await client.list_solix_devices()
            self.solix_devices = {d["sn"]: d for d in solix if d.get("sn")}
        except EufySdkApiClientError:
            self.solix_devices = getattr(self, "solix_devices", {})
        # Retain multiplicity before the coordinator dictionary collapses duplicates.
        self.device_serial_counts = {}
        for device in devices:
            serial = device.get("sn")
            if isinstance(serial, str):
                self.device_serial_counts[serial] = (
                    self.device_serial_counts.get(serial, 0) + 1
                )
        return {d["sn"]: d for d in devices if d.get("sn")}
