"""DataUpdateCoordinator for eufy_sdk — owns the bridge connection + the device list."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from copy import deepcopy
from typing import TYPE_CHECKING, ClassVar

from homeassistant.core import callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    MAX_CAMERA_HEALTH_TARGETS,
    MAX_COMMAND_HEALTH_TARGETS,
    EufySdkApiClientAuthenticationError,
    EufySdkApiClientCommunicationError,
    EufySdkApiClientError,
    EufySdkApiClientReplyTimeoutError,
    camera_health,
    camera_health_event,
    camera_health_is_current,
    command_health,
    command_health_event,
    command_health_is_current,
)
from .const import LOGGER

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
    solix_snapshot_generation: int = 0

    property_observations: dict | None = None

    # A queued refresh request is not a completed snapshot. Keep receipt order
    # separately so entities can reconcile without bypassing HA's debouncer.
    _command_health_generation: int = 0
    _camera_health_generation: int = 0
    snapshot_generation: int = 0
    completed_snapshot_generation: int = 0
    _completed_snapshot_transport_generation: int | None = None
    _snapshot_transport_generation: int | None = None
    snapshot_fresh: bool = False
    last_complete_snapshot_ms: int | None = None
    _consecutive_reply_timeouts: int = 0
    _reply_timeout_generation: int | None = None

    _RECOVERY_DELAYS_S = (45, 90, 180, 300)
    _REFRESH_MIN_GAP_S = 30
    _fast_retry_cancel = None
    _refresh_limit_cancel = None
    _recovery_step = 0
    _recovery_active = False
    _recovery_stopped = False
    _recovery_event_used = False
    _last_refresh_started: float | None = None
    _refresh_task: asyncio.Task | None = None

    @callback
    def _schedule_fast_retry(self) -> None:
        """Retry locally with bounded backoff until a complete snapshot succeeds."""
        if self._recovery_stopped:
            return
        self._recovery_active = True
        if self._fast_retry_cancel is not None:
            return
        delay = self._RECOVERY_DELAYS_S[self._recovery_step]
        interval = getattr(self, "update_interval", None)
        if interval is not None:
            delay = min(delay, interval.total_seconds())
        self._recovery_step = min(
            self._recovery_step + 1, len(self._RECOVERY_DELAYS_S) - 1
        )
        self._fast_retry_cancel = async_call_later(
            self.hass, delay, self._fast_retry_fire
        )

    @callback
    def _fast_retry_fire(self, _now: object) -> None:
        """Use HA's debouncer for every recovery attempt."""
        self._fast_retry_cancel = None
        if self._recovery_active:
            self.hass.async_create_task(self.async_request_refresh())

    @callback
    def request_recovery_refresh(self) -> None:
        """Use the first fresh device observation in a failure episode."""
        if not self._recovery_active or self._recovery_event_used:
            return
        self._recovery_event_used = True
        self.hass.async_create_task(self.async_request_refresh())

    @callback
    def _refresh_limit_fire(self, _now: object) -> None:
        """Retain one request suppressed by the shared start-rate limit."""
        self._refresh_limit_cancel = None
        if self._recovery_active:
            self.hass.async_create_task(self.async_request_refresh())

    @callback
    def _clear_fast_retry(self, *, stop: bool = False) -> None:
        """Cancel recovery timers on success, challenge or unload."""
        for name in ("_fast_retry_cancel", "_refresh_limit_cancel"):
            cancel = getattr(self, name)
            if cancel is not None:
                cancel()
                setattr(self, name, None)
        self._recovery_stopped = self._recovery_stopped or stop
        self._recovery_active = False
        self._recovery_step = 0
        self._recovery_event_used = False

    def record_state_event(self, serial: str, values: dict) -> None:
        """Protect only events received while a snapshot request is in progress."""
        pending = getattr(self, "_pending_state_events", None)
        if pending is not None:
            pending.setdefault(serial, {}).update(values)

    def reset_camera_health(self) -> None:
        """Retire camera projections when the bridge runtime is replaced."""
        self._camera_health_generation += 1
        self._camera_health_projections: dict[str, dict] = {}
        if getattr(self, "_pending_camera_health", None) is not None:
            self._pending_camera_health.clear()
        for device in (self.data or {}).values():
            device.pop("cameraHealth", None)
        if self.data:
            self.async_update_listeners()

    def _project_camera_health(self, serial: str, health: dict | None) -> dict | None:
        """Preserve the newest camera evidence across events and snapshots."""
        cache = getattr(self, "_camera_health_projections", None)
        if cache is None:
            self._camera_health_projections = cache = {}
        previous = cache.get(serial)
        health = camera_health(health)
        if health is not None and camera_health_is_current(health, previous):
            if previous is None and len(cache) >= MAX_CAMERA_HEALTH_TARGETS:
                return None
            cache[serial] = health
            return health
        return previous

    def apply_camera_health_event(self, event: dict) -> None:
        """Apply validated camera evidence without touching operational state."""
        event = camera_health_event(event)
        if event is None or event["sn"] not in (self.data or {}):
            return
        serial = event["sn"]
        previous = self.data[serial].get("cameraHealth")
        health = self._project_camera_health(serial, event)
        if health is None or health == previous:
            return
        pending = getattr(self, "_pending_camera_health", None)
        if pending is not None:
            pending[serial] = health
        self.data[serial]["cameraHealth"] = health
        self.async_update_listeners()

    def _project_snapshot_camera_health(self, snapshot: dict, generation: int) -> None:
        """Keep snapshot camera ordering separate from command-path ordering."""
        for serial, device in snapshot.items():
            raw = device.pop("cameraHealth", None)
            health = self._project_camera_health(
                serial, raw if generation == self._camera_health_generation else None
            )
            if health is not None:
                device["cameraHealth"] = health
        for serial, health in (self._pending_camera_health or {}).items():
            if serial in snapshot:
                snapshot[serial]["cameraHealth"] = self._project_camera_health(
                    serial, health
                )

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
        """Coalesce concurrent reads and limit starts across all refresh sources."""
        if self._recovery_stopped:
            msg = "bridge_refresh_stopped"
            raise UpdateFailed(msg)
        if self._refresh_task is not None:
            return await asyncio.shield(self._refresh_task)
        now = time.monotonic()
        if self._recovery_active and self._last_refresh_started is not None:
            remaining = self._REFRESH_MIN_GAP_S - (now - self._last_refresh_started)
            if remaining > 0:
                if self._refresh_limit_cancel is None:
                    self._refresh_limit_cancel = async_call_later(
                        self.hass, remaining, self._refresh_limit_fire
                    )
                # A throttled request must preserve the previous availability,
                # not masquerade as a successful replacement snapshot.
                if self._snapshot_tolerated:
                    return deepcopy(self.data)
                msg = "bridge_refresh_recovery_pending"
                raise UpdateFailed(msg)
        if self._refresh_limit_cancel is not None:
            self._refresh_limit_cancel()
            self._refresh_limit_cancel = None
        self._last_refresh_started = now
        task = self._refresh_task = asyncio.create_task(self._async_refresh_snapshot())
        try:
            return await task
        finally:
            if task.done():
                self._refresh_task = None

    async def _async_refresh_snapshot(self) -> dict[str, dict]:
        """Journal concurrent events until the replacement snapshot is ready."""
        self.snapshot_fresh = False
        self._snapshot_tolerated = False
        self._snapshot_transport_generation = None
        runtime = getattr(getattr(self, "config_entry", None), "runtime_data", None)
        if runtime is not None and not runtime.client.connected:
            # A coordinator poll can reconnect before the reconnect supervisor.
            # Retire old evidence before capturing this refresh's generation.
            self.reset_camera_health()
        self.snapshot_generation += 1
        generation = self.snapshot_generation
        health_generation = self._command_health_generation
        camera_generation = self._camera_health_generation
        self._pending_state_events: dict[str, dict] | None = {}
        self._pending_command_health: dict[str, dict] | None = {}
        self._pending_camera_health: dict[str, dict] | None = {}
        try:
            snapshot = await self._read_snapshot()
            self._merge_snapshot_events(snapshot, health_generation, camera_generation)
        except ConfigEntryAuthFailed:
            self._clear_fast_retry()
            raise
        except UpdateFailed:
            self._schedule_fast_retry()
            raise
        else:
            if self._snapshot_tolerated:
                self._schedule_fast_retry()
                return snapshot
            self._clear_fast_retry()
            self.completed_snapshot_generation = generation
            self._completed_snapshot_transport_generation = (
                self._snapshot_transport_generation
            )
            self.snapshot_fresh = True
            self.last_complete_snapshot_ms = int(time.time() * 1000)
            return snapshot
        finally:
            self._pending_state_events = None
            self._pending_command_health = None
            self._pending_camera_health = None

    def _merge_snapshot_events(
        self, snapshot: dict, health_generation: int, camera_generation: int
    ) -> None:
        """Apply identical lifecycle and event ordering to fresh and cached data."""
        self._project_snapshot_health(snapshot, health_generation)
        self._project_snapshot_camera_health(snapshot, camera_generation)
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

    def _record_refresh_failure(  # noqa: PLR0913 - fixed evidence plus outcome flags
        self,
        phase: str,
        method: str,
        error: Exception,
        started: float,
        *,
        reset: bool = False,
        tolerated: bool = False,
    ) -> str:
        """Retain fixed evidence independently of routine successful refreshes."""
        failure = error.failure if isinstance(error, EufySdkApiClientError) else None
        if failure is not None:
            category = failure.category
        elif isinstance(error, EufySdkApiClientCommunicationError):
            category = error.category
        elif isinstance(error, EufySdkApiClientError):
            category = "bridge_error"
        else:
            category = "other"
        elapsed_ms = (
            failure.elapsed_ms
            if failure is not None
            else max(0, int((time.monotonic() - started) * 1000))
        )
        failures = getattr(self, "refresh_failures", None)
        if failures is None:
            self.refresh_failures = failures = deque(maxlen=20)
        failures.append(
            {
                "timestamp_ms": int(time.time() * 1000),
                "phase": phase,
                "method": method,
                "category": category,
                "elapsed_ms": elapsed_ms,
                "reset": reset,
                "consecutive_count": self._consecutive_reply_timeouts,
            }
        )
        return (
            f"bridge_update_failed: phase={phase} method={method} reason={category} "
            f"elapsed_ms={elapsed_ms} reset={str(reset).lower()}"
            + (" tolerated=true" if tolerated else "")
        )

    async def _communication_failure(
        self,
        phase: str,
        error: EufySdkApiClientCommunicationError,
        started: float,
        transport_generation: int | None,
    ) -> dict[str, dict]:
        """Tolerate one slow reply only while its original transport is alive."""
        client = self.config_entry.runtime_data.client
        reply_timeout = isinstance(error, EufySdkApiClientReplyTimeoutError)
        if not reply_timeout or transport_generation != self._reply_timeout_generation:
            self._consecutive_reply_timeouts = 0
        if reply_timeout:
            self._consecutive_reply_timeouts += 1
        self._reply_timeout_generation = transport_generation if reply_timeout else None
        same_transport = (
            client.connected
            and transport_generation is not None
            and client.connection_generation == transport_generation
        )
        reset = not (
            reply_timeout
            and phase in {"auth_status", "list_devices"}
            and same_transport
            and self._consecutive_reply_timeouts == 1
        )
        tolerated = (
            not reset
            and self.data is not None
            and self.completed_snapshot_generation == self.snapshot_generation - 1
            and self._completed_snapshot_transport_generation == transport_generation
        )
        if reset:
            reset = bool(
                await client.reset_connection(expected_generation=transport_generation)
            )
        method = {
            "connect": "connect",
            "auth_status": "auth.status",
            "list_devices": "devices.list",
        }[phase]
        msg = self._record_refresh_failure(
            phase, method, error, started, reset=reset, tolerated=tolerated
        )
        if tolerated:
            self._snapshot_tolerated = True
            LOGGER.warning("%s", msg)
            return deepcopy(self.data)
        raise UpdateFailed(msg) from None

    def _check_auth_status(self, auth: dict, started: float) -> None:
        """Keep authentication outcomes unchanged while retaining fixed evidence."""
        state = auth.get("state")
        if state == "ok":
            return
        self._consecutive_reply_timeouts = 0
        self._record_refresh_failure(
            "auth_status", "auth.status", EufySdkApiClientError(), started
        )
        if state in ("require_2fa", "require_captcha"):
            msg = "bridge_auth_challenge_required"
            raise ConfigEntryAuthFailed(msg)
        # The bridge may still be booting/logging in. Keep retrying instead of
        # freezing a transient pending state in a reauthentication flow.
        msg = "bridge_auth_not_ready"
        raise UpdateFailed(msg)

    async def _read_snapshot(self) -> dict[str, dict]:
        """Ensure the connection is up, confirm we're authed, and return the devices."""
        client = self.config_entry.runtime_data.client
        phase, method = "connect", "connect"
        started = time.monotonic()
        # A failed connect must not retire a replacement that wins the lock next.
        transport_generation = client.connection_generation
        try:
            if not client.connected:
                await client.connect()
            transport_generation = client.connection_generation
            phase, method = "auth_status", "auth.status"
            started = time.monotonic()
            auth = await client.auth_status()
            self._check_auth_status(auth, started)
            phase, method = "list_devices", "devices.list"
            started = time.monotonic()
            devices = await client.list_devices()
        except EufySdkApiClientAuthenticationError as error:
            self._consecutive_reply_timeouts = 0
            self._record_refresh_failure(phase, method, error, started)
            msg = "bridge_authentication_failed"
            raise ConfigEntryAuthFailed(msg) from None
        except EufySdkApiClientCommunicationError as error:
            return await self._communication_failure(
                phase, error, started, transport_generation
            )
        except (ConfigEntryAuthFailed, UpdateFailed):
            raise
        except EufySdkApiClientError as error:
            # An expected bridge rejection keeps the socket and the fast retry.
            self._consecutive_reply_timeouts = 0
            msg = self._record_refresh_failure(phase, method, error, started)
            raise UpdateFailed(msg) from None
        except Exception as error:
            self._consecutive_reply_timeouts = 0
            self._record_refresh_failure(phase, method, error, started)
            raise
        self._consecutive_reply_timeouts = 0
        # Solix is optional + independent: a hiccup must not fail the eufy update.
        started = time.monotonic()
        try:
            solix = await client.list_solix_devices()
            self.solix_devices = {d["sn"]: d for d in solix if d.get("sn")}
            self.solix_snapshot_generation += 1
        except EufySdkApiClientError as error:
            msg = self._record_refresh_failure("solix", "solix.devices", error, started)
            LOGGER.debug("%s", msg)
            self.solix_devices = getattr(self, "solix_devices", {})
        # Retain multiplicity before the coordinator dictionary collapses duplicates.
        self.device_serial_counts = {}
        for device in devices:
            serial = device.get("sn")
            if isinstance(serial, str):
                self.device_serial_counts[serial] = (
                    self.device_serial_counts.get(serial, 0) + 1
                )
        self._snapshot_transport_generation = transport_generation
        return {d["sn"]: d for d in devices if d.get("sn")}
