"""
WebSocket client for the ha-eufy-sdk bridge.

The bridge holds the eufy account (and drives 2FA/captcha); this client speaks its
JSON protocol over one WebSocket. Request/response is `{id, cmd}` -> `{id, ok}`;
unsolicited `{event}` messages go to `on_event`. See the bridge's `docs/ws-protocol.md`.
"""

from __future__ import annotations

import asyncio
import math
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

import aiohttp
from homeassistant.exceptions import HomeAssistantError

from .manifest_reads import normalize_properties, normalize_snapshot

if TYPE_CHECKING:
    from collections.abc import Callable


class EufySdkApiClientError(HomeAssistantError):
    """A general bridge error."""


COMMAND_ERROR_CODES = frozenset(
    {
        "command_ack_timeout",
        "command_expired_before_send",
        "command_expired_after_send",
        "command_rejected",
        "command_not_sent",
        "command_session_closed",
        "command_connect_timeout",
        "command_unsupported",
        "command_other_error",
        "command_validation_failed",
        "command_clock_unavailable",
        "command_duplicate_request",
        "command_queue_full",
        "command_superseded_before_send",
        "command_superseded_after_send",
        "command_cancelled_before_send",
        "command_cancelled_after_send",
        "command_closed_before_send",
        "command_closed_after_send",
    }
)

BOUNDED_PROFILE = "bounded-state-v1"
BOUNDED_CALLER_SECONDS = 80
BOUNDED_CLOCK_MAX_MS = 1000
CLOCK_TOKEN_MAX_LENGTH = 128
DEVICE_SN_MAX_LENGTH = 128
LATE_CONTRARY_WINDOW_MS = 600000
MAX_COMMAND_HEALTH_TARGETS = 1024
LATE_CONTRARY_STATES = frozenset(
    {
        "contrary",
        "matched",
        "expired",
        "superseded",
        "closed",
        "recovery_settled",
        "suspended",
        "verification_exhausted",
        "detection_exhausted",
    }
)


def late_contrary_event(value: Any) -> dict[str, Any] | None:
    """Project the finite SDK-confirmed light observation contract."""
    if (
        not isinstance(value, dict)
        or value.get("event") != "lateContraryEffect"
        or not isinstance(value.get("deviceSn"), str)
        or not 1 <= len(value["deviceSn"]) <= DEVICE_SN_MAX_LENGTH
        or value.get("property") != "light"
        or type(value.get("intent")) is not int
        or not 0 < value["intent"] <= 2**53 - 1
        or type(value.get("desiredValue")) is not bool
        or type(value.get("recoveryBusy")) is not bool
        or not isinstance(value.get("state"), str)
        or value["state"] not in LATE_CONTRARY_STATES
        or not _finite_number(value.get("ageMs"))
        or not 0 <= value["ageMs"] <= LATE_CONTRARY_WINDOW_MS
        or ("observedValue" in value and type(value["observedValue"]) is not bool)
        or (
            value["state"] == "contrary"
            and (
                type(value.get("observedValue")) is not bool
                or value["observedValue"] is value["desiredValue"]
            )
        )
    ):
        return None
    return {
        key: value[key]
        for key in (
            "event",
            "deviceSn",
            "property",
            "intent",
            "desiredValue",
            "observedValue",
            "ageMs",
            "state",
            "recoveryBusy",
        )
        if key in value
    }


def command_health(value: Any) -> dict[str, Any] | None:
    """Project SDK-owned command-path health without implying device state."""
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("status"), str)
        or value["status"] not in {"command_degraded", "healthy"}
        or not _finite_number(value.get("cooldownRemainingMs"))
        or not 0 <= value["cooldownRemainingMs"] <= LATE_CONTRARY_WINDOW_MS
        or (
            "revision" in value
            and (
                type(value["revision"]) is not int
                or not 0 <= value["revision"] <= 2**53 - 1
            )
        )
    ):
        return None
    return {
        key: value[key]
        for key in ("status", "cooldownRemainingMs", "revision")
        if key in value
    }


def command_health_is_current(health: dict, previous: dict | None) -> bool:
    """Refuse older versions and conflicting or lengthened same-version state."""
    if previous is None or "revision" not in previous:
        return True
    revision = health.get("revision")
    previous_revision = previous["revision"]
    if revision is None or revision < previous_revision:
        return False
    return revision != previous_revision or (
        health["status"] == previous["status"]
        and health["cooldownRemainingMs"] <= previous["cooldownRemainingMs"]
    )


def command_health_event(value: Any) -> dict[str, Any] | None:
    """Validate the finite per-light health event and strip private fields."""
    if (
        not isinstance(value, dict)
        or value.get("event") != "commandHealth"
        or not isinstance(value.get("sn"), str)
        or not 1 <= len(value["sn"]) <= DEVICE_SN_MAX_LENGTH
        or value.get("property") != "light"
    ):
        return None
    health = command_health(value)
    return (
        {"event": "commandHealth", "sn": value["sn"], "property": "light", **health}
        if health
        else None
    )


COMMAND_RECEIPT_STATUSES = frozenset(
    {
        "validation_failed",
        "connect_timeout",
        "queue_full",
        "expired_before_send",
        "superseded_before_send",
        "cancelled_before_send",
        "closed_before_send",
        "acknowledged",
        "pending_accepted",
        "rejected",
        "ack_timeout",
        "cancelled_after_send",
        "closed_after_send",
        "not_sent",
        "expired_after_send",
    }
)
COMMAND_EFFECTS = frozenset({"unconfirmed", "unknown", "confirmed_report"})
type CommandStatus = Literal[
    "validation_failed",
    "connect_timeout",
    "queue_full",
    "expired_before_send",
    "superseded_before_send",
    "cancelled_before_send",
    "closed_before_send",
    "acknowledged",
    "pending_accepted",
    "rejected",
    "ack_timeout",
    "cancelled_after_send",
    "closed_after_send",
    "not_sent",
    "expired_after_send",
]
type CommandEffect = Literal["unconfirmed", "unknown", "confirmed_report"]


@dataclass(frozen=True)
class CommandReceipt:
    """Finite per-invocation evidence; peer acceptance is not a state report."""

    status: CommandStatus
    send_attempted: bool
    effect: CommandEffect
    attempt: int | None = None
    intent: int | None = None
    superseded: bool = False
    command_health: dict[str, Any] | None = None
    rejection_code: int | None = None
    response_latency_ms: float | None = None

    @property
    def accepted(self) -> bool:
        """Whether this invocation received transport acceptance."""
        return not self.superseded and self.status in {
            "acknowledged",
            "pending_accepted",
        }


@dataclass
class _BoundedPropertyInvocation:
    """Keep older preparations superseded after a newer caller has completed."""

    superseded: bool = False


def _finite_number(value: Any) -> bool:
    """Reject booleans, nonfinite values and numeric overflow."""
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _command_receipt(value: Any) -> CommandReceipt:
    """Keep a detached, closed receipt schema and discard untrusted fields."""
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("status"), str)
        or value["status"] not in COMMAND_RECEIPT_STATUSES
        or type(value.get("sendAttempted")) is not bool
        or not isinstance(value.get("effect"), str)
        or value["effect"] not in COMMAND_EFFECTS
        or (
            value["status"] in {"acknowledged", "pending_accepted"}
            and value["sendAttempted"] is not True
        )
        or (value["status"].endswith("_before_send") and value["sendAttempted"])
        or ("superseded" in value and type(value["superseded"]) is not bool)
    ):
        message = "bridge_invalid_receipt"
        raise EufySdkApiClientCommunicationError(message)
    ordinals = {
        key: value[key]
        if type(value.get(key)) is int and 0 <= value[key] <= 2**53 - 1
        else None
        for key in ("attempt", "intent")
    }
    return CommandReceipt(
        cast("CommandStatus", value["status"]),
        value["sendAttempted"],
        cast("CommandEffect", value["effect"]),
        superseded=value.get("superseded", False),
        command_health=command_health(value.get("commandHealth")),
        rejection_code=(
            value["rejectionCode"]
            if type(value.get("rejectionCode")) is int
            and _finite_number(value["rejectionCode"])
            else None
        ),
        response_latency_ms=(
            value["responseLatencyMs"]
            if _finite_number(value.get("responseLatencyMs"))
            and value["responseLatencyMs"] >= 0
            else None
        ),
        **ordinals,
    )


WEBRTC_ERROR_CODES = frozenset(
    {
        "webrtc_busy",
        "webrtc_releasing",
        "webrtc_unsupported",
        "webrtc_closed",
        "webrtc_failed",
    }
)


class EufySdkApiClientCommunicationError(EufySdkApiClientError):
    """The bridge could not be reached / spoke unexpectedly."""


class EufySdkApiClientAuthenticationError(EufySdkApiClientError):
    """The bridge is not authenticated (needs 2FA/captcha) or rejected a command."""


class EufySdkApiClient:
    """A connection to one ha-eufy-sdk bridge."""

    def __init__(
        self,
        host: str,
        port: int,
        session: aiohttp.ClientSession,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        on_reconnect: Callable[[], None] | None = None,
    ) -> None:
        """Store the bridge address; the connection is opened by `connect`."""
        # int() the port defensively: HA's NumberSelector yields a float, which
        # would make an invalid URL like ws://host:3012.0/ws.
        self._url = f"ws://{host}:{int(port)}/ws"
        self._session = session
        self._on_event = on_event
        # Called after the receive loop reconnects following a drop (e.g. a bridge
        # restart), so the coordinator can refresh at once instead of leaving entities
        # unavailable until the next poll.
        self._on_reconnect = on_reconnect
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._recv_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._connect_lock = asyncio.Lock()
        self._closing = False
        self._connection_generation = 0
        self._command_health_projections: dict[str, dict] = {}
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._webrtc_callbacks: dict[str, Callable[[dict[str, Any]], None]] = {}
        self._webrtc_relay_enabled = False
        self._webrtc_relay_listener: Callable[[bool], None] | None = None
        self._bounded_capability = False
        self._late_contrary_capability = False
        self._bounded_requests: dict[int, aiohttp.ClientWebSocketResponse] = {}
        self._observation_preserving_aborts: set[int] = set()
        self._bounded_invocations: dict[
            tuple[str, str], _BoundedPropertyInvocation
        ] = {}
        self._cancel_tasks: set[asyncio.Task] = set()
        self._correction_requests: dict[
            tuple[str, str], tuple[int, aiohttp.ClientWebSocketResponse, bool]
        ] = {}

    @property
    def late_contrary_effects_supported(self) -> bool:
        """Require versioned confirmation and the complete bounded write contract."""
        return self.bounded_state_writes_supported and self._late_contrary_capability

    def _set_late_contrary_capability(self, capability: Any) -> None:
        """Require explicit versioned correction confirmation support."""
        self._late_contrary_capability = (
            isinstance(capability, dict)
            and type(capability.get("version")) is int
            and capability["version"] == 1
            and type(capability.get("watchWindowMs")) is int
            and capability["watchWindowMs"] == LATE_CONTRARY_WINDOW_MS
            and capability.get("confirmation") is True
        )

    @property
    def bounded_state_writes_supported(self) -> bool:
        """Whether this socket advertises the complete versioned contract."""
        return self.connected and self._bounded_capability

    def _set_bounded_capability(self, capability: Any) -> None:
        """Require every negotiated bound; an older bridge stays legacy."""
        maxima = {
            "admissionMaxMs": 60000,
            "activeMaxMs": 12000,
            "sdkTotalMaxMs": 72000,
            "bridgeMaxMs": 75000,
            "callerMaxMs": 80000,
        }
        clock = capability.get("clock") if isinstance(capability, dict) else None
        self._bounded_capability = (
            isinstance(capability, dict)
            and type(capability.get("version")) is int
            and capability["version"] == 1
            and capability.get("profile") == BOUNDED_PROFILE
            and capability.get("cancellation") is True
            and capability.get("receipts") is True
            and all(
                type(capability.get(key)) is int and capability[key] == value
                for key, value in maxima.items()
            )
            and isinstance(clock, dict)
            and type(clock.get("version")) is int
            and clock["version"] == 1
            and type(clock.get("maxUncertaintyMs")) is int
            and 0 < clock["maxUncertaintyMs"] <= BOUNDED_CLOCK_MAX_MS
        )

    def _schedule_command_cancel(
        self, mid: int, transport: Any, *, keep_observation: bool = False
    ) -> None:
        """Send only to the owning socket, without renewing a caller's lifetime."""
        if transport.closed:
            return

        async def send() -> None:
            with suppress(aiohttp.ClientError, OSError, TimeoutError):
                async with asyncio.timeout(0.25):
                    self._next_id += 1
                    await transport.send_json(
                        {
                            "id": self._next_id,
                            "cmd": "boundedStateCancel",
                            "version": 1,
                            "requestId": mid,
                            **({"keepObservation": True} if keep_observation else {}),
                        }
                    )

        task = asyncio.create_task(send())
        self._cancel_tasks.add(task)
        task.add_done_callback(self._cancel_tasks.discard)

    def pause_light_correction(
        self, sn: str, *, keep_observation: bool = False
    ) -> None:
        """Cancel automatic work, preserving a watch explicitly during degradation."""
        request = self._correction_requests.get((sn, "light"))
        if request is not None:
            mid, transport, correction_observation = request
            keep_observation = keep_observation or correction_observation
            if keep_observation:
                self._observation_preserving_aborts.add(mid)
            self._schedule_command_cancel(
                mid, transport, keep_observation=keep_observation
            )

    @property
    def webrtc_relay_enabled(self) -> bool:
        """Whether the latest bridge hello explicitly enabled the relay."""
        return self._webrtc_relay_enabled

    def listen_webrtc_relay(
        self, listener: Callable[[bool], None]
    ) -> Callable[[], None]:
        """Watch capability changes after camera classes have been selected."""
        self._webrtc_relay_listener = listener

        def unsubscribe() -> None:
            if self._webrtc_relay_listener is listener:
                self._webrtc_relay_listener = None

        return unsubscribe

    def register_webrtc_session(
        self, session_id: str, callback: Callable[[dict[str, Any]], None]
    ) -> aiohttp.ClientWebSocketResponse | None:
        """Bind pushes to one session on the current transport, without connecting."""
        self._webrtc_callbacks[session_id] = callback
        return self._ws

    def remove_webrtc_session(self, session_id: str) -> None:
        """Retire pushes before scheduling close."""
        self._webrtc_callbacks.pop(session_id, None)

    def _invalidate_webrtc_sessions(self) -> None:
        """Retire sessions before a replacement transport can inherit them."""
        callbacks = tuple(self._webrtc_callbacks.values())
        self._webrtc_callbacks.clear()
        for callback in callbacks:
            callback({"event": "webrtc.closed", "code": "webrtc_closed"})

    @property
    def connected(self) -> bool:
        """Whether the WebSocket is open."""
        return self._ws is not None and not self._ws.closed

    def _reset_command_health(self) -> None:
        """Retire health watermarks and pending reply ownership for a runtime."""
        self._connection_generation += 1
        self._command_health_projections.clear()

    def _project_command_health(self, sn: str, health: Any) -> dict | None:
        """Keep one ordered effective projection independently of raw receipts."""
        health = command_health(health)
        previous = self._command_health_projections.get(sn)
        if health is not None and command_health_is_current(health, previous):
            if (
                previous is None
                and len(self._command_health_projections) >= MAX_COMMAND_HEALTH_TARGETS
            ):
                return None
            self._command_health_projections[sn] = health
            return health
        return previous

    def _require_connection(self, transport: Any, generation: int) -> None:
        """Reject replies from a retired connection even if already resolved."""
        if self._ws is not transport or generation != self._connection_generation:
            message = "bridge_connection_changed"
            raise EufySdkApiClientCommunicationError(message)

    async def reset_connection(self) -> None:
        """Retire a wedged bridge socket while preserving reconnect behavior."""
        async with self._connect_lock:
            self._reset_command_health()
            self._bounded_capability = False
            self._invalidate_webrtc_sessions()
            ws, task = self._ws, self._recv_task
            self._ws = None
            self._recv_task = None
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(
                        EufySdkApiClientCommunicationError("connection reset")
                    )
            self._pending.clear()
            if ws is not None:
                with suppress(aiohttp.ClientError, OSError, TimeoutError):
                    async with asyncio.timeout(5):
                        await ws.close()
            if not self._closing:
                self._schedule_reconnect()

    async def connect(self) -> None:
        """Open the WebSocket + receive loop (serialized against reconnect)."""
        self._closing = False
        async with self._connect_lock:
            if self.connected:
                return
            try:
                async with asyncio.timeout(15):
                    self._ws = await self._session.ws_connect(self._url, heartbeat=30)
            except (aiohttp.ClientError, TimeoutError, OSError):
                msg = "bridge_unreachable"
                raise EufySdkApiClientCommunicationError(msg) from None
            self._reset_command_health()
            self._recv_task = asyncio.ensure_future(self._receive_loop())

    async def close(self) -> None:
        """Close the WebSocket and stop reconnecting."""
        had_late_contrary = self._late_contrary_capability
        self._reset_command_health()
        self._closing = True
        self._bounded_capability = False
        self._late_contrary_capability = False
        if had_late_contrary and self._on_event:
            self._on_event({"event": "bridgeDisconnected"})
        for mid, transport in tuple(self._bounded_requests.items()):
            self._schedule_command_cancel(mid, transport)
        if self._cancel_tasks:
            await asyncio.gather(*tuple(self._cancel_tasks), return_exceptions=True)
        self._webrtc_relay_listener = None
        self._invalidate_webrtc_sessions()
        if self._reconnect_task:
            self._reconnect_task.cancel()
            self._reconnect_task = None
        if self._recv_task:
            self._recv_task.cancel()
            self._recv_task = None
        if self._ws:
            await self._ws.close()
            self._ws = None
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(
                    EufySdkApiClientCommunicationError("connection closed")
                )
        self._pending.clear()

    def _dispatch_event(self, data: dict) -> None:
        """Order health and lifecycle events before notifying integration consumers."""
        if data["event"] == "lateContraryEffect":
            data = late_contrary_event(data)
            if data is None or not self.late_contrary_effects_supported:
                return
        elif data["event"] == "commandHealth":
            data = command_health_event(data)
            if data is None:
                return
            health = command_health(data)
            previous = self._command_health_projections.get(data["sn"])
            if not command_health_is_current(health, previous):
                return
            if self._project_command_health(data["sn"], health) is None:
                return
        elif data["event"] == "ready":
            self._reset_command_health()
        if self._on_event:
            self._on_event(data)

    async def _receive_loop(self) -> None:  # noqa: PLR0912
        """Read frames: resolve pending requests by id, dispatch events."""
        if self._ws is None:
            return
        try:
            async for msg in self._ws:
                if msg.type is not aiohttp.WSMsgType.TEXT:
                    continue
                data = msg.json()
                if data.get("event") == "hello":
                    self._set_bounded_capability(data.get("boundedStateWrites"))
                    self._set_late_contrary_capability(data.get("lateContraryEffects"))
                    # Keep the last advertisement through a disconnect. The next
                    # hello (including an old bridge's absent field) replaces it.
                    enabled = data.get("webrtcRelay") is True
                    changed = enabled != self._webrtc_relay_enabled
                    self._webrtc_relay_enabled = enabled
                    if changed and self._webrtc_relay_listener is not None:
                        self._webrtc_relay_listener(enabled)
                mid = data.get("id")
                if mid is not None and mid in self._pending:
                    fut = self._pending.pop(mid)
                    if not fut.done():
                        fut.set_result(data)
                elif data.get("event") in ("webrtc.candidate", "webrtc.closed"):
                    # SDP and ICE content must never enter the general HA event bus.
                    handler = self._webrtc_callbacks.get(data.get("sessionId"))
                    if handler is not None:
                        handler(data)
                elif data.get("event"):
                    self._dispatch_event(data)
        except (aiohttp.ClientError, asyncio.CancelledError):
            pass
        finally:
            # Clear the socket so `connected` reports the drop, fail in-flight requests,
            # and (unless deliberately closing) reconnect so events resume promptly, not
            # only on the next poll.
            had_late_contrary = self._late_contrary_capability
            self._reset_command_health()
            self._ws = None
            self._bounded_capability = False
            self._late_contrary_capability = False
            if had_late_contrary and self._on_event:
                self._on_event({"event": "bridgeDisconnected"})
            self._invalidate_webrtc_sessions()
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(
                        EufySdkApiClientCommunicationError("connection lost")
                    )
            self._pending.clear()
            if not self._closing:
                self._schedule_reconnect()

    def _schedule_reconnect(self) -> None:
        """Start the reconnect supervisor if it isn't already running."""
        if self._reconnect_task and not self._reconnect_task.done():
            return
        self._reconnect_task = asyncio.ensure_future(self._reconnect())

    async def _reconnect(self) -> None:
        """Reopen the WebSocket with capped backoff until closed/connected."""
        delay = 1
        while not self._closing and not self.connected:
            try:
                await self.connect()
            except EufySdkApiClientCommunicationError:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)
            else:
                # Back up after a drop — let the coordinator recover entities now, not
                # at the next poll.
                if self._on_reconnect:
                    self._on_reconnect()
                return

    async def rpc(
        self,
        cmd: str,
        timeout: float = 15,  # noqa: ASYNC109 — deliberate per-call timeout API
        _transport: aiohttp.ClientWebSocketResponse | None = None,
        *,
        _cancel_on_abort: bool = False,
        _send_guard: Callable[[], bool] | None = None,
        _correction_key: tuple[str, str] | None = None,
        _correction_keep_observation: bool = True,
        **args: Any,
    ) -> dict[str, Any]:
        """Send a command and await its reply. Raises on `ok: false`."""
        if not self.connected or (
            _transport is not None and self._ws is not _transport
        ):
            msg = "not connected"
            raise EufySdkApiClientCommunicationError(msg)
        if _send_guard is not None and not _send_guard():
            message = "light_correction_paused_before_send"
            raise EufySdkApiClientCommunicationError(message)
        self._next_id += 1
        mid = self._next_id
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_event_loop().create_future()
        self._pending[mid] = fut
        transport = self._ws
        generation = self._connection_generation
        if _cancel_on_abort:
            self._bounded_requests[mid] = transport
        if _correction_key is not None:
            self._correction_requests[_correction_key] = (
                mid,
                transport,
                _correction_keep_observation,
            )
        try:
            await self._ws.send_json({"id": mid, "cmd": cmd, **args})  # type: ignore[union-attr]
            async with asyncio.timeout(timeout):
                reply = await fut
        except TimeoutError:
            if _cancel_on_abort and not (fut.done() and not fut.cancelled()):
                self._schedule_command_cancel(
                    mid,
                    transport,
                    keep_observation=mid in self._observation_preserving_aborts,
                )
            self._pending.pop(mid, None)
            msg = "bridge_request_timeout"
            raise EufySdkApiClientCommunicationError(msg) from None
        except asyncio.CancelledError:
            if _cancel_on_abort and not (fut.done() and not fut.cancelled()):
                self._schedule_command_cancel(
                    mid,
                    transport,
                    keep_observation=mid in self._observation_preserving_aborts,
                )
            raise
        except (aiohttp.ClientError, OSError):
            self._pending.pop(mid, None)
            msg = "bridge_send_failed"
            raise EufySdkApiClientCommunicationError(msg) from None
        finally:
            self._pending.pop(mid, None)
            self._bounded_requests.pop(mid, None)
            self._observation_preserving_aborts.discard(mid)
            if _correction_key is not None and self._correction_requests.get(
                _correction_key
            ) == (mid, transport, _correction_keep_observation):
                self._correction_requests.pop(_correction_key, None)
        self._require_connection(transport, generation)
        if not reply.get("ok"):
            code = reply.get("code")
            msg = (
                code
                if isinstance(code, str)
                and code
                in (
                    WEBRTC_ERROR_CODES
                    if cmd.startswith("webrtc.")
                    else COMMAND_ERROR_CODES
                )
                else "bridge_request_rejected"
            )
            raise EufySdkApiClientError(msg)
        return reply

    # ── auth (mirrors the bridge's auth.* protocol) ──
    async def auth_status(self) -> dict[str, Any]:
        """Return the current auth state (ok|require_2fa|require_captcha|pending)."""
        return (await self.rpc("auth.status"))["auth"]

    async def submit_2fa(self, code: str) -> dict[str, Any]:
        """Submit a 2FA code; returns the new auth state."""
        return (await self.rpc("auth.submit", code=code))["auth"]

    async def submit_captcha(self, answer: str) -> dict[str, Any]:
        """Submit a captcha answer; returns the new auth state."""
        return (await self.rpc("auth.submit", captcha=answer))["auth"]

    async def retrigger_auth(self) -> dict[str, Any]:
        """Request a fresh 2FA code / captcha; returns the new auth state."""
        return (await self.rpc("auth.retrigger"))["auth"]

    # ── devices ──
    async def list_devices(self) -> list[dict[str, Any]]:
        """Every device the bridge exposes (sn/name/model/codec/capabilities/state)."""
        transport, generation = self._ws, self._connection_generation
        reply = await self.rpc("devices.list")
        self._require_connection(transport, generation)
        devices = []
        for device in reply["devices"]:
            projected = {
                key: value
                for key, value in normalize_snapshot(device).items()
                if key != "commandHealth"
            }
            health = self._project_command_health(
                device.get("sn"), device.get("commandHealth")
            )
            if health is not None:
                projected["commandHealth"] = health
            devices.append(projected)
        return devices

    async def refresh_event_image(self, sn: str) -> bool:
        """Force a 'Last event' image refresh; returns True if a newer image landed."""
        return bool((await self.rpc("event.refresh", sn=sn)).get("changed"))

    async def list_solix_devices(self) -> list[dict[str, Any]]:
        """Return the Anker Solix devices (empty if Solix isn't configured)."""
        # Each: {sn, productCode, name, category, capabilities, values, firmware}.
        return (await self.rpc("solix.devices")).get("devices", [])

    async def set_solix_light(self, sn: str, *, on: bool) -> None:
        """Toggle a Solarbank's ambient light (encrypted set_device_attrs write)."""
        await self.rpc("solix.setLight", deviceSn=sn, on=on)

    async def get_solix_device_attrs(
        self, sn: str, keys: list[str] | None = None
    ) -> dict[str, Any]:
        """Read a Solix device's attributes (e.g. screen_off_time) as a flat map."""
        reply = await self.rpc("solix.getDeviceAttrs", deviceSn=sn, keys=keys or [])
        return reply.get("attributes") or {}

    async def set_solix_display_timeout(self, sn: str, index: int) -> None:
        """Set a Solarbank display screen-off timeout by 1-based index (MQTT)."""
        await self.rpc("solix.setDisplayTimeout", deviceSn=sn, index=index)

    async def get_solix_power_cutoff(
        self, sn: str, site_id: str = ""
    ) -> list[dict[str, Any]]:
        """Read the battery discharge-cutoff (minimum-SOC) preset options."""
        # Each: {id, output_cutoff_data (SOC %), is_selected}.
        reply = await self.rpc("solix.getPowerCutoff", deviceSn=sn, siteId=site_id)
        return reply.get("options") or []

    async def set_solix_power_cutoff(self, sn: str, cutoff_data_id: int) -> None:
        """Select a discharge-cutoff preset by its device id."""
        await self.rpc("solix.setPowerCutoff", deviceSn=sn, cutoffDataId=cutoff_data_id)

    async def get_solix_soc_params(self, sn: str) -> dict[str, Any]:
        """Read a Solarbank's SOC-limit block (discharge/charge limit, reserve)."""
        # {chargeUpperLimit, dischargeLowerLimit, backupReserve, backupReserveSwitch,
        #  socCalibrationEnable} — whole-percent ints, or {} if the site has no block.
        reply = await self.rpc("solix.getSocParams", deviceSn=sn)
        return reply.get("params") or {}

    async def set_solix_soc_limits(
        self,
        sn: str,
        *,
        discharge: int | None = None,
        charge: int | None = None,
    ) -> dict[str, Any]:
        """Write the discharge and/or charge limit (%); read-modify-write keeps rest."""
        kwargs: dict[str, Any] = {"deviceSn": sn}
        if discharge is not None:
            kwargs["dischargeLowerLimit"] = int(discharge)
        if charge is not None:
            kwargs["chargeUpperLimit"] = int(charge)
        reply = await self.rpc("solix.setSocLimits", **kwargs)
        return reply.get("params") or {}

    async def get_properties(self, sn: str) -> list[dict[str, Any]]:
        """Return a device's property manifest (name/type/unit/writable/enumValues)."""
        return normalize_properties(await self.rpc("device.properties", sn=sn))

    async def get_config(self) -> dict[str, Any]:
        """Return the bridge's runtime config (currently {pollMs})."""
        return await self.rpc("config.get")

    async def set_poll_ms(self, poll_ms: int) -> int:
        """Set the cloud poll interval (ms); returns the new effective value."""
        return (await self.rpc("config.set", pollMs=poll_ms))["pollMs"]

    async def list_effects(self) -> list[dict[str, Any]]:
        """
        Return the smart-light effect gallery ({id, name, colors}) for effect_list.

        Account-wide and cached by the bridge; the first call enumerates the catalogue
        over several HTTP round-trips, hence the longer timeout.
        """
        reply = await self.rpc("light.effects", timeout=60)
        return reply.get("effects", [])

    async def action(self, sn: str, action: str, *args: Any) -> Any:
        """
        Invoke a capability action (a typed method, not a scalar property) on a device.

        e.g. smart_light `setColor({red,green,blue})` / `setEffect(id)` — controls that
        `set_property` can't reach because they take structured arguments.
        """
        reply = await self.rpc("device.action", sn=sn, action=action, args=list(args))
        return reply.get("result")

    async def preset_slots(self, sn: str) -> list[dict[str, Any]]:
        """
        Read a camera's stored preset positions.

        A P2P request/reply rather than a cloud read, so it answers only while the
        camera is awake — a sleeping battery camera times out. Hence the longer
        timeout, and callers that treat a failure as "ask again later".
        """
        reply = await self.rpc(
            "device.action", timeout=45, sn=sn, action="preset.list", args=[]
        )
        return reply.get("result") or []

    async def confirm_light(self, sn: str, intent: int) -> dict[str, Any]:
        """Request a bounded, read-only confirmation on the existing SDK session."""
        if (
            not self.late_contrary_effects_supported
            or type(intent) is not int
            or not 0 < intent <= 2**53 - 1
        ):
            message = "light_confirmation_unavailable"
            raise EufySdkApiClientCommunicationError(message)
        response = await self.rpc(
            "device.confirmLight", timeout=8, sn=sn, intent=intent
        )
        reply = response.get("confirmation")
        if (
            not isinstance(reply, dict)
            or type(reply.get("intent")) is not int
            or reply["intent"] != intent
            or type(reply.get("desiredValue")) is not bool
            or type(reply.get("confirmed")) is not bool
            or type(reply.get("recoveryBusy")) is not bool
            or not _finite_number(reply.get("ageMs"))
            or not 0 <= reply["ageMs"] <= LATE_CONTRARY_WINDOW_MS
            or ("observedValue" in reply and type(reply["observedValue"]) is not bool)
            or (reply["confirmed"] and "observedValue" not in reply)
            or (
                "reason" in reply
                and (reply["reason"] != "read_budget_exhausted" or reply["confirmed"])
            )
        ):
            message = "light_confirmation_invalid"
            raise EufySdkApiClientCommunicationError(message)
        return {
            key: reply[key]
            for key in (
                "intent",
                "desiredValue",
                "observedValue",
                "confirmed",
                "recoveryBusy",
                "ageMs",
                "reason",
            )
            if key in reply
        }

    async def set_property(  # noqa: PLR0912, PLR0913, PLR0915
        self,
        sn: str,
        name: str,
        value: Any,
        *,
        bounded: bool = False,
        correction: bool = False,
        automatic: bool = False,
        correction_expires_at: float | None = None,
        correction_expires_at_ms: float | None = None,
        _send_guard: Callable[[], bool] | None = None,
    ) -> CommandReceipt | None:
        """Opt eligible scalar writes into one frozen caller lifetime."""
        if (correction_expires_at is None) != (correction_expires_at_ms is None) or (
            correction_expires_at is not None
            and (
                not correction
                or not _finite_number(correction_expires_at)
                or not _finite_number(correction_expires_at_ms)
            )
        ):
            message = "light_correction_invalid_deadline"
            raise EufySdkApiClientCommunicationError(message)
        automatic = automatic or correction
        if automatic and (
            not bounded
            or name != "light"
            or type(value) is not bool
            or not self.late_contrary_effects_supported
        ):
            message = "light_correction_unavailable"
            raise EufySdkApiClientCommunicationError(message)
        if (
            not bounded
            or name != "light"
            or type(value) is not bool
            or not self.bounded_state_writes_supported
        ):
            await self.rpc("device.set", sn=sn, name=name, value=value)
            return None
        loop = asyncio.get_running_loop()
        born = loop.time()
        born_wall_ms = time.time() * 1000
        expires_ms = born_wall_ms + BOUNDED_CALLER_SECONDS * 1000
        caller_deadline = born + BOUNDED_CALLER_SECONDS
        if correction_expires_at is not None:
            remaining_authority = correction_expires_at - born
            if not 0 < remaining_authority <= LATE_CONTRARY_WINDOW_MS / 1000:
                message = "command_expired_before_send"
                raise EufySdkApiClientCommunicationError(message)
            if not (
                0 < correction_expires_at_ms <= born_wall_ms + LATE_CONTRARY_WINDOW_MS
            ):
                message = "light_correction_invalid_deadline"
                raise EufySdkApiClientCommunicationError(message)
            caller_deadline = min(caller_deadline, correction_expires_at)
            expires_ms = min(
                expires_ms,
                correction_expires_at_ms,
                born_wall_ms + remaining_authority * 1000,
            )
        key = (sn, name)
        previous = self._bounded_invocations.get(key)
        if previous is not None:
            previous.superseded = True
        invocation = _BoundedPropertyInvocation()
        self._bounded_invocations[key] = invocation
        transport = self._ws
        generation = self._connection_generation
        clock_guard: asyncio.Task | None = None
        clock_changed = False
        caller = asyncio.current_task()

        async def watch_clock(uncertainty_ms: float) -> None:
            nonlocal clock_changed
            while True:
                await asyncio.sleep(0.1)
                elapsed = (loop.time() - born) * 1000
                if abs(time.time() * 1000 - born_wall_ms - elapsed) > uncertainty_ms:
                    clock_changed = True
                    caller.cancel()
                    return

        try:
            async with asyncio.timeout_at(caller_deadline):
                clock = await self.rpc(
                    "boundedStateClock",
                    version=1,
                    callerSentAtMs=born_wall_ms,
                    _transport=transport,
                )
                self._require_connection(transport, generation)
                if invocation.superseded:
                    return CommandReceipt(
                        "superseded_before_send",
                        send_attempted=False,
                        effect="unconfirmed",
                    )
                received_ms = time.time() * 1000
                elapsed_ms = (loop.time() - born) * 1000
                sent = clock.get("bridgeSentAtMs")
                received = clock.get("bridgeReceivedAtMs")
                if (
                    type(clock.get("version")) is not int
                    or clock["version"] != 1
                    or clock.get("callerSentAtMs") != born_wall_ms
                    or not _finite_number(sent)
                    or not _finite_number(received)
                    or sent < received
                    or received_ms < born_wall_ms
                    or not isinstance(clock.get("clockToken"), str)
                    or not 1 <= len(clock["clockToken"]) <= CLOCK_TOKEN_MAX_LENGTH
                    or abs(received_ms - born_wall_ms - elapsed_ms)
                    > BOUNDED_CLOCK_MAX_MS
                ):
                    message = "command_clock_unavailable"
                    raise EufySdkApiClientCommunicationError(message)
                uncertainty_ms = (
                    received_ms - born_wall_ms - (sent - received)
                ) / 2 + 1
                if not 0 < uncertainty_ms <= BOUNDED_CLOCK_MAX_MS:
                    message = "command_clock_unavailable"
                    raise EufySdkApiClientCommunicationError(message)
                remaining_ms = min(
                    expires_ms - received_ms, (caller_deadline - loop.time()) * 1000
                )
                if remaining_ms <= 0:
                    message = "command_expired_before_send"
                    raise EufySdkApiClientCommunicationError(message)
                clock_guard = asyncio.create_task(watch_clock(uncertainty_ms))
                reply = await self.rpc(
                    "device.set",
                    timeout=remaining_ms / 1000,
                    _transport=transport,
                    _cancel_on_abort=True,
                    **(
                        {
                            "_send_guard": _send_guard,
                            "_correction_key": key,
                            "_correction_keep_observation": correction,
                        }
                        if automatic or _send_guard is not None
                        else {}
                    ),
                    sn=sn,
                    name=name,
                    value=value,
                    execution={
                        "version": 1,
                        "profile": BOUNDED_PROFILE,
                        "callerExpiresAtMs": expires_ms,
                        "remainingBudgetMs": remaining_ms,
                        "clockToken": clock["clockToken"],
                        "callerReceivedAtMs": received_ms,
                        "clockUncertaintyMs": uncertainty_ms,
                        **({"automatic": True} if automatic else {}),
                        **({"correction": True} if correction else {}),
                        **(
                            {"correctionExpiresAtMs": correction_expires_at_ms}
                            if correction_expires_at_ms is not None
                            else {}
                        ),
                    },
                )
                self._require_connection(transport, generation)
                receipt = _command_receipt(reply.get("receipt"))
                previous = self._command_health_projections.get(sn)
                health = receipt.command_health
                if health is not None and command_health_is_current(health, previous):
                    health = self._project_command_health(sn, health)
                else:
                    health = None
                if health is not None and self._on_event:
                    self._on_event(
                        {
                            "event": "commandHealth",
                            "sn": sn,
                            "property": "light",
                            **health,
                        }
                    )
                return receipt
        except TimeoutError:
            message = "bridge_request_timeout"
            raise EufySdkApiClientCommunicationError(message) from None
        except asyncio.CancelledError:
            if clock_changed:
                caller.uncancel()
                message = "command_clock_unavailable"
                raise EufySdkApiClientCommunicationError(message) from None
            raise
        finally:
            if self._bounded_invocations.get(key) is invocation:
                self._bounded_invocations.pop(key)
            if clock_guard is not None:
                clock_guard.cancel()
                with suppress(asyncio.CancelledError):
                    await clock_guard

    async def reboot(self, sn: str) -> None:
        """Reboot a HomeBase (hub-only; it drops offline for a minute or two)."""
        await self.rpc("device.reboot", sn=sn)
