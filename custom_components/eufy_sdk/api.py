"""
WebSocket client for the ha-eufy-sdk bridge.

The bridge holds the eufy account (and drives 2FA/captcha); this client speaks its
JSON protocol over one WebSocket. Request/response is `{id, cmd}` -> `{id, ok}`;
unsolicited `{event}` messages go to `on_event`. See the bridge's `docs/ws-protocol.md`.
"""

from __future__ import annotations

import asyncio
import math
import re
import time
from collections import deque
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

import aiohttp
from homeassistant.exceptions import HomeAssistantError

from .const import LOGGER
from .manifest_reads import normalize_properties, normalize_snapshot

if TYPE_CHECKING:
    from collections.abc import Callable


class EufySdkApiClientError(HomeAssistantError):
    """A general bridge error."""

    failure: RpcFailure | None = None


@dataclass(frozen=True)
class RpcFailure:
    """Request-local evidence with no arguments, payloads or exception text."""

    method: str
    category: str
    elapsed_ms: int


SLOW_RPC_MS = 5000


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
GUARD_MODE_TIMEOUT_SECONDS = 60
BOUNDED_CLOCK_MAX_MS = 1000
CLOCK_TOKEN_MAX_LENGTH = 128
DEVICE_SN_MAX_LENGTH = 128
LATE_CONTRARY_WINDOW_MS = 600000
MAX_COMMAND_HEALTH_TARGETS = 1024
MAX_CAMERA_HEALTH_TARGETS = 1024
AUTOMATIC_LIGHT_CANCEL_SECONDS = 45
MAX_AUTOMATIC_LIGHT_REQUESTS = 1024
CAMERA_CONNECTIVITY = ("online", "suspect", "offline", "unknown")
CAMERA_OUTCOMES = ("acknowledged", "rejected", "timeout", "other")
CAMERA_HEALTH_BASES = (
    "startup",
    "child_contact",
    "media",
    "hub_reported_offline",
    "vendor_status",
    "contact_expired",
    "suspect_expired",
    "parent_unassessable",
)
CAMERA_HEALTH_TIMES = (
    "sinceMs",
    "lastContactMs",
    "lastHubOfflineMs",
    "lastHubReportMs",
    "lastCloudParamMs",
)
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


def _safe_ordinal(value: Any) -> bool:
    """Accept nonnegative JavaScript-safe integers, excluding booleans."""
    return type(value) is int and 0 <= value <= 2**53 - 1


def camera_health(value: Any) -> dict[str, Any] | None:
    """Validate the complete observe-only contract and detach its finite fields."""
    if not isinstance(value, dict):
        return None
    ceiling = int(time.time() * 1000) + 600000

    def timestamp(item: Any) -> bool:
        return item is None or (_safe_ordinal(item) and item <= ceiling)

    enums = {
        "connectivity": CAMERA_CONNECTIVITY,
        "basis": CAMERA_HEALTH_BASES,
        "evidence": ("vendor_verified", "hub_reported", "unqualified", "none"),
        "lastContactSource": ("push", "media", None),
        "parentTransport": ("connected", "disconnected", "unknown"),
        "enforcement": ("observe_only",),
    }
    if (
        type(value.get("version")) is not int
        or value["version"] != 1
        or not isinstance(value.get("incarnation"), str)
        or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value["incarnation"]) is None
        or not _safe_ordinal(value.get("revision"))
        or not _safe_ordinal(value.get("hubOfflineCount"))
        or value.get("commandsBlocked") is not False
        or "lastOutcome" not in value
        or any(
            key not in value or value[key] not in options
            for key, options in enums.items()
        )
        or any(
            key not in value or not timestamp(value[key]) for key in CAMERA_HEALTH_TIMES
        )
    ):
        return None
    rssi, raw = value.get("rssi"), value.get("rawStatus")
    if (
        not isinstance(rssi, dict)
        or not {"value", "valid", "lastValidMs"} <= rssi.keys()
        or (rssi["value"] is not None and type(rssi["value"]) is not int)
        or type(rssi["valid"]) is not bool
        or not timestamp(rssi["lastValidMs"])
        or not isinstance(raw, dict)
        or not {"p1131", "p1182", "observedMs"} <= raw.keys()
        or not timestamp(raw["observedMs"])
        or any(
            raw[key] is not None
            and type(raw[key]) is not bool
            and not _finite_number(raw[key])
            for key in ("p1131", "p1182")
        )
    ):
        return None
    outcomes = value.get("outcomes")
    if not isinstance(outcomes, dict) or any(
        not isinstance(outcomes.get(state), dict)
        or any(not _safe_ordinal(outcomes[state].get(key)) for key in CAMERA_OUTCOMES)
        for state in CAMERA_CONNECTIVITY
    ):
        return None
    outcome = value["lastOutcome"]
    if outcome is not None and (
        not isinstance(outcome, dict)
        or not {"atMs", "connectivity", "status", "code"} <= outcome.keys()
        or outcome["atMs"] is None
        or not timestamp(outcome["atMs"])
        or outcome["connectivity"] not in CAMERA_CONNECTIVITY
        or outcome["status"] not in CAMERA_OUTCOMES
        or (outcome["code"] is not None and type(outcome["code"]) is not int)
    ):
        return None
    return deepcopy(
        {
            **{
                key: value[key]
                for key in (
                    "version",
                    "incarnation",
                    "revision",
                    "hubOfflineCount",
                    "commandsBlocked",
                    *enums,
                    *CAMERA_HEALTH_TIMES,
                )
            },
            "rssi": {key: rssi[key] for key in ("value", "valid", "lastValidMs")},
            "rawStatus": {key: raw[key] for key in ("p1131", "p1182", "observedMs")},
            "outcomes": {
                state: {key: outcomes[state][key] for key in CAMERA_OUTCOMES}
                for state in CAMERA_CONNECTIVITY
            },
            "lastOutcome": None
            if outcome is None
            else {
                key: outcome[key] for key in ("atMs", "connectivity", "status", "code")
            },
        }
    )


def camera_health_is_current(health: dict, previous: dict | None) -> bool:
    """Accept only a changed incarnation or a strictly higher revision."""
    return (
        previous is None
        or health["incarnation"] != previous["incarnation"]
        or health["revision"] > previous["revision"]
    )


def camera_health_event(value: Any) -> dict[str, Any] | None:
    """Validate a camera-scoped event and strip unrelated wire fields."""
    if (
        not isinstance(value, dict)
        or value.get("event") != "cameraHealth"
        or not isinstance(value.get("sn"), str)
        or not 1 <= len(value["sn"]) <= DEVICE_SN_MAX_LENGTH
    ):
        return None
    health = camera_health(value)
    return {"event": "cameraHealth", "sn": value["sn"], **health} if health else None


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

    category = "other"

    def __init__(self, message: str, *, category: str = "other") -> None:
        """Keep the public message separate from a fixed transport category."""
        super().__init__(message)
        self.category = category


class EufySdkApiClientReplyTimeoutError(EufySdkApiClientCommunicationError):
    """The request was sent but its reply deadline expired."""

    def __init__(self) -> None:
        """Keep the existing public error compatible with HA service handling."""
        super().__init__("bridge_request_timeout", category="reply_timeout")


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
        self._disconnect_listeners: list[Callable[[], None]] = []
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._recv_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._connect_lock = asyncio.Lock()
        self._closing = False
        self._connection_generation = 0
        self._command_health_projections: dict[str, dict] = {}
        self._camera_health_projections: dict[str, dict] = {}
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self.slow_successful_rpcs: deque[dict] = deque(maxlen=20)
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
            int, tuple[tuple[str, str], aiohttp.ClientWebSocketResponse, bool]
        ] = {}
        self._retained_automatic_light_requests: dict[
            int, tuple[tuple[str, str], aiohttp.ClientWebSocketResponse, float]
        ] = {}
        self._cancelled_correction_requests: set[int] = set()
        self._safety_cancel_inflight: set[int] = set()

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
        self,
        mid: int,
        transport: Any,
        *,
        keep_observation: bool = False,
        retry_until: float | None = None,
        on_sent: Callable[[], None] | None = None,
    ) -> asyncio.Task | None:
        """Send only to the owning socket, without renewing a caller's lifetime."""
        if transport.closed:
            return None
        generation = self._connection_generation

        async def send() -> None:
            while True:
                if retry_until is not None and (
                    self._closing
                    or generation != self._connection_generation
                    or transport is not self._ws
                    or transport.closed
                ):
                    return
                try:
                    async with asyncio.timeout(0.25):
                        self._next_id += 1
                        await transport.send_json(
                            {
                                "id": self._next_id,
                                "cmd": "boundedStateCancel",
                                "version": 1,
                                "requestId": mid,
                                **(
                                    {"keepObservation": True}
                                    if keep_observation
                                    else {}
                                ),
                            }
                        )
                except (aiohttp.ClientError, OSError, TimeoutError):
                    if retry_until is None or time.monotonic() >= retry_until:
                        return
                    await asyncio.sleep(min(1, retry_until - time.monotonic()))
                else:
                    if on_sent is not None:
                        on_sent()
                    return

        task = asyncio.create_task(send())
        self._cancel_tasks.add(task)
        task.add_done_callback(self._cancel_tasks.discard)
        return task

    def pause_light_correction(
        self, sn: str, *, keep_observation: bool = False
    ) -> None:
        """Cancel automatic work, preserving a watch explicitly during degradation."""
        self._prune_automatic_light_requests()
        key = (sn, "light")
        requests = {
            mid: (transport, False)
            for mid, (
                target,
                transport,
                _,
            ) in self._retained_automatic_light_requests.items()
            if target == key
        }
        requests.update(
            (mid, (transport, observation))
            for mid, (
                target,
                transport,
                observation,
            ) in self._correction_requests.items()
            if target == key
        )
        for mid, (transport, correction_observation) in requests.items():
            if (
                mid in self._safety_cancel_inflight
                or transport is not self._ws
                or transport.closed
            ):
                continue
            preserve = keep_observation or correction_observation
            if mid in self._bounded_requests:
                self._cancelled_correction_requests.add(mid)
            if preserve and mid in self._bounded_requests:
                self._observation_preserving_aborts.add(mid)
            retained = self._retained_automatic_light_requests.get(mid)
            deadline = (
                retained[2]
                if retained
                else time.monotonic()
                + BOUNDED_CALLER_SECONDS
                + AUTOMATIC_LIGHT_CANCEL_SECONDS
            )

            def delivered(request_id: int = mid) -> None:
                self._retained_automatic_light_requests.pop(request_id, None)
                self._correction_requests.pop(request_id, None)

            self._safety_cancel_inflight.add(mid)
            task = self._schedule_command_cancel(
                mid,
                transport,
                keep_observation=preserve,
                retry_until=deadline,
                on_sent=delivered,
            )
            if task is not None:
                task.add_done_callback(
                    lambda _, request_id=mid: self._safety_cancel_inflight.discard(
                        request_id
                    )
                )
            else:
                self._safety_cancel_inflight.discard(mid)

    def _prune_automatic_light_requests(self) -> None:
        """Retain cancellation authority only for the owning socket's child window."""
        now = time.monotonic()
        for mid, (_, transport, expires) in tuple(
            self._retained_automatic_light_requests.items()
        ):
            if expires <= now or transport is not self._ws or transport.closed:
                self._retained_automatic_light_requests.pop(mid, None)

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

    @property
    def connection_generation(self) -> int:
        """Identify a transport lifetime, including disconnects and replacements."""
        return self._connection_generation

    def _reset_command_health(self) -> None:
        """Retire health watermarks and pending reply ownership for a runtime."""
        self._connection_generation += 1
        self._command_health_projections.clear()
        self._camera_health_projections.clear()
        self._retained_automatic_light_requests.clear()

    def _project_camera_health(self, sn: str, health: Any) -> dict | None:
        """Keep a bounded camera projection independently of command health."""
        health = camera_health(health)
        previous = self._camera_health_projections.get(sn)
        if health is not None and camera_health_is_current(health, previous):
            if (
                previous is None
                and len(self._camera_health_projections) >= MAX_CAMERA_HEALTH_TARGETS
            ):
                return None
            self._camera_health_projections[sn] = health
            return health
        return previous

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
            raise EufySdkApiClientCommunicationError(
                message, category="connection_reset"
            )

    def add_disconnect_listener(
        self, listener: Callable[[], None]
    ) -> Callable[[], None]:
        """Subscribe diagnostics to socket loss without publishing operational state."""
        self._disconnect_listeners.append(listener)
        return lambda: self._disconnect_listeners.remove(listener)

    def _notify_disconnected(self) -> None:
        """Publish socket unavailability independently of negotiated capabilities."""
        for listener in tuple(self._disconnect_listeners):
            try:
                listener()
            except Exception:  # noqa: BLE001 - a diagnostic must not interrupt socket teardown
                LOGGER.exception("Camera diagnostic disconnect publication failed")

    async def reset_connection(self, *, expected_generation: int | None = None) -> bool:
        """Retire a wedged bridge socket while preserving reconnect behavior."""
        async with self._connect_lock:
            if (
                expected_generation is not None
                and self.connection_generation != expected_generation
            ):
                return False
            self._reset_command_health()
            self._bounded_capability = False
            self._invalidate_webrtc_sessions()
            ws, task = self._ws, self._recv_task
            self._ws = None
            self._recv_task = None
            self._notify_disconnected()
            # Resolve explicit-reset failures before the receiver's teardown can
            # otherwise classify the same requests as an unsolicited socket loss.
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(
                        EufySdkApiClientCommunicationError(
                            "connection reset", category="connection_reset"
                        )
                    )
            self._pending.clear()
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            if ws is not None:
                with suppress(aiohttp.ClientError, OSError, TimeoutError):
                    async with asyncio.timeout(5):
                        await ws.close()
            if not self._closing:
                self._schedule_reconnect()
            return True

    async def connect(self) -> None:
        """Open the WebSocket + receive loop (serialized against reconnect)."""
        started = time.monotonic()
        try:
            await self._connect()
        except EufySdkApiClientCommunicationError as error:
            error.failure = RpcFailure(
                "connect", "not_connected", int((time.monotonic() - started) * 1000)
            )
            raise

    async def _connect(self) -> None:
        """Serialize opening the transport against the reconnect supervisor."""
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
        ws, self._ws = self._ws, None
        self._notify_disconnected()
        if ws:
            await ws.close()
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(
                    EufySdkApiClientCommunicationError(
                        "connection closed", category="connection_closed"
                    )
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
        elif data["event"] == "cameraHealth":
            data = camera_health_event(data)
            if data is None:
                return
            health = camera_health(data)
            if (
                not camera_health_is_current(
                    health, self._camera_health_projections.get(data["sn"])
                )
                or self._project_camera_health(data["sn"], health) is None
            ):
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
            self._notify_disconnected()
            self._bounded_capability = False
            self._late_contrary_capability = False
            if had_late_contrary and self._on_event:
                self._on_event({"event": "bridgeDisconnected"})
            self._invalidate_webrtc_sessions()
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(
                        EufySdkApiClientCommunicationError(
                            "connection lost", category="connection_closed"
                        )
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
        timeout: float = 15,  # noqa: ASYNC109 - deliberate per-call timeout API
        _transport: aiohttp.ClientWebSocketResponse | None = None,
        **args: Any,
    ) -> dict[str, Any]:
        """Attach sanitized, request-local timing without mixing concurrent calls."""
        started = time.monotonic()
        try:
            reply = await self._rpc(cmd, timeout, _transport, **args)
        except EufySdkApiClientError as error:
            category = (
                error.category
                if isinstance(error, EufySdkApiClientCommunicationError)
                else "bridge_error"
            )
            error.failure = RpcFailure(
                cmd, category, max(0, int((time.monotonic() - started) * 1000))
            )
            raise
        elapsed_ms = max(0, int((time.monotonic() - started) * 1000))
        if elapsed_ms > SLOW_RPC_MS:
            self.slow_successful_rpcs.append(
                {
                    "method": cmd,
                    "elapsed_ms": elapsed_ms,
                    "timestamp_ms": int(time.time() * 1000),
                }
            )
        return reply

    @staticmethod
    async def _send_rpc(transport: Any, payload: dict) -> None:
        """Distinguish a send timeout from expiry of the reply deadline."""
        try:
            await transport.send_json(payload)
        except (aiohttp.ClientError, OSError, TimeoutError):
            message = "bridge_send_failed"
            raise EufySdkApiClientCommunicationError(
                message, category="send_failed"
            ) from None

    async def _rpc(
        self,
        cmd: str,
        timeout: float = 15,  # noqa: ASYNC109 — deliberate per-call timeout API
        _transport: aiohttp.ClientWebSocketResponse | None = None,
        *,
        _cancel_on_abort: bool = False,
        _send_guard: Callable[[], bool] | None = None,
        _correction_key: tuple[str, str] | None = None,
        _correction_keep_observation: bool = True,
        _retain_automatic_light: bool = False,
        **args: Any,
    ) -> dict[str, Any]:
        """Send a command and await its reply. Raises on `ok: false`."""
        if not self.connected or (
            _transport is not None and self._ws is not _transport
        ):
            msg = "not connected"
            raise EufySdkApiClientCommunicationError(msg, category="not_connected")
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
            self._prune_automatic_light_requests()
            self._correction_requests[mid] = (
                _correction_key,
                transport,
                _correction_keep_observation,
            )
        try:
            await self._send_rpc(transport, {"id": mid, "cmd": cmd, **args})
            async with asyncio.timeout(timeout):
                reply = await fut
        except TimeoutError:
            if (
                _cancel_on_abort
                and mid not in self._cancelled_correction_requests
                and not (fut.done() and not fut.cancelled())
            ):
                self._schedule_command_cancel(
                    mid,
                    transport,
                    keep_observation=mid in self._observation_preserving_aborts,
                )
            self._pending.pop(mid, None)
            raise EufySdkApiClientReplyTimeoutError from None
        except asyncio.CancelledError:
            if (
                _cancel_on_abort
                and mid not in self._cancelled_correction_requests
                and not (fut.done() and not fut.cancelled())
            ):
                self._schedule_command_cancel(
                    mid,
                    transport,
                    keep_observation=mid in self._observation_preserving_aborts,
                )
            raise
        finally:
            if (
                _retain_automatic_light
                and _correction_key is not None
                and mid not in self._cancelled_correction_requests
                and transport is self._ws
                and generation == self._connection_generation
                and not transport.closed
                and fut.done()
                and not fut.cancelled()
                and fut.exception() is None
                and fut.result().get("ok")
            ):
                self._prune_automatic_light_requests()
                if (
                    len(self._retained_automatic_light_requests)
                    >= MAX_AUTOMATIC_LIGHT_REQUESTS
                ):
                    self._retained_automatic_light_requests.pop(
                        next(iter(self._retained_automatic_light_requests))
                    )
                self._retained_automatic_light_requests[mid] = (
                    _correction_key,
                    transport,
                    time.monotonic() + AUTOMATIC_LIGHT_CANCEL_SECONDS,
                )
            self._pending.pop(mid, None)
            self._bounded_requests.pop(mid, None)
            self._observation_preserving_aborts.discard(mid)
            self._cancelled_correction_requests.discard(mid)
            self._correction_requests.pop(mid, None)
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
                if key not in {"commandHealth", "cameraHealth"}
            }
            health = self._project_command_health(
                device.get("sn"), device.get("commandHealth")
            )
            if health is not None:
                projected["commandHealth"] = health
            camera = self._project_camera_health(
                device.get("sn"), device.get("cameraHealth")
            )
            if camera is not None:
                projected["cameraHealth"] = camera
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
        # Automatic bootstrap callers share the original authority deadline,
        # but only corrections can inherit an existing SDK observation watch.
        if (correction_expires_at is None) != (correction_expires_at_ms is None) or (
            correction_expires_at is not None
            and (
                not (automatic or correction)
                or not _finite_number(correction_expires_at)
                or not _finite_number(correction_expires_at_ms)
            )
        ):
            message = "light_correction_invalid_deadline"
            raise EufySdkApiClientCommunicationError(message)
        retain_automatic_light = automatic
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
            if name == "armingMode":
                # Guard mode is exposed as armingMode. Allow preparation time
                # before the SDK's per-command RTC/P2P waits begin.
                await self.rpc(
                    "device.set",
                    timeout=GUARD_MODE_TIMEOUT_SECONDS,
                    sn=sn,
                    name=name,
                    value=value,
                )
            else:
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
                            "_retain_automatic_light": retain_automatic_light,
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
                            if correction and correction_expires_at_ms is not None
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
