"""S1 signaling ownership; media lifetime remains the bridge/go2rtc concern."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import re

from homeassistant.components.camera.webrtc import (
    WebRTCAnswer, WebRTCCandidate, WebRTCError,
)
from homeassistant.core import callback
from webrtc_models import RTCIceCandidateInit

from .api import EufySdkApiClientError, WEBRTC_ERROR_CODES

MAX_PENDING_CANDIDATES = 64
# Relay admission (12 s) + stock go2rtc producer startup (30 s) + signaling (3 s).
WEBRTC_OFFER_TIMEOUT = 45


def is_s1_camera(coordinator, sn):
    """Match the bridge's positive camera/unique parent binding, fail closed."""
    def serial(value):
        return isinstance(value, str) and bool(value) and value.strip() == value

    device = (coordinator.data or {}).get(sn, {})
    parent_sn = device.get("stationSn")
    counts = getattr(coordinator, "device_serial_counts", {})
    parent = (coordinator.data or {}).get(parent_sn, {}) if serial(parent_sn) else {}
    return bool(
        serial(sn) and serial(parent_sn) and sn != parent_sn
        and counts.get(sn) == 1 and counts.get(parent_sn) == 1
        and isinstance(parent.get("model"), str)
        and re.match(r"^T9000", parent["model"], re.IGNORECASE)
    )


@dataclass
class SignalingSession:
    session_id: str
    send_message: object
    task: asyncio.Task
    transport: object = None
    forwarded: bool = False
    answered: bool = False
    closed_before_answer: bool = False
    closed: bool = False
    candidates: list[str] = field(default_factory=list)
    remote_candidates: list[str] = field(default_factory=list)


class NativeWebRTCMixin:
    """Register synchronously, tombstone synchronously, never reconnect for ICE."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._signaling_sessions = {}
        self._signaling_removed = False
        self._signaling_close_tasks = set()

    @property
    def _signaling_client(self):
        return self.coordinator.config_entry.runtime_data.client

    def _live(self, session):
        return not session.closed and self._signaling_sessions.get(session.session_id) is session

    @callback
    def _close_signaling(self, session, code=None):
        if not self._live(session):
            return
        session.closed = True
        self._signaling_sessions.pop(session.session_id)
        client = self._signaling_client
        client.remove_webrtc_session(session.session_id)
        session.candidates.clear()
        session.remote_candidates.clear()
        if code is not None:
            session.send_message(WebRTCError(code, code))
        if session.task is not asyncio.current_task() and not session.task.done():
            session.task.cancel()
        if session.forwarded and session.transport is not None:
            task = self.hass.async_create_task(self._close_bridge(session))
            self._signaling_close_tasks.add(task)
            task.add_done_callback(self._signaling_close_tasks.discard)

    async def _close_bridge(self, session):
        try:
            await self._signaling_client.rpc(
                "webrtc.close", sessionId=session.session_id, _transport=session.transport
            )
        except EufySdkApiClientError:
            pass  # Transport loss itself retires owner-scoped bridge sessions.

    @callback
    def close_webrtc_session(self, session_id):
        if (session := self._signaling_sessions.get(session_id)) is not None:
            self._close_signaling(session)

    @callback
    def _bridge_event(self, session, event):
        if not self._live(session):
            return
        if event.get("event") == "webrtc.closed":
            if session.answered:
                self._close_signaling(session, "webrtc_closed")
            else:
                # The pending open RPC may carry a more specific admission error.
                session.closed_before_answer = True
        elif session.closed_before_answer:
            return
        elif isinstance(candidate := event.get("candidate"), str):
            if session.answered:
                session.send_message(WebRTCCandidate(
                    RTCIceCandidateInit(candidate=candidate, sdp_mid="0")
                ))
            elif len(session.remote_candidates) < MAX_PENDING_CANDIDATES:
                session.remote_candidates.append(candidate)
            else:
                self._close_signaling(session, "webrtc_failed")

    async def async_handle_async_webrtc_offer(self, offer_sdp, session_id, send_message):
        session = SignalingSession(session_id, send_message, asyncio.current_task())
        self._signaling_sessions[session_id] = session
        client = self._signaling_client
        session.transport = client.register_webrtc_session(
            session_id, lambda event: self._bridge_event(session, event)
        )
        try:
            if self._signaling_removed or not is_s1_camera(self.coordinator, self._sn):
                self._close_signaling(session, "webrtc_unsupported")
                return
            if session.transport is None or not client.connected:
                self._close_signaling(session, "webrtc_closed")
                return
            config = self.async_get_webrtc_client_configuration().configuration.to_dict()
            # No await between the live check and dispatch; close cancels this task.
            session.forwarded = True
            reply = await client.rpc("webrtc.open", sessionId=session_id, sn=self._sn,
                                     sdp=offer_sdp, iceServers=config.get("iceServers", []),
                                     timeout=WEBRTC_OFFER_TIMEOUT,
                                     _transport=session.transport)
            if not self._live(session):
                return
            if session.closed_before_answer:
                self._close_signaling(session, "webrtc_closed")
                return
            if not isinstance(reply.get("sdp"), str):
                self._close_signaling(session, "webrtc_failed")
                return
            session.send_message(WebRTCAnswer(reply["sdp"]))
            session.answered = True
            for candidate in session.remote_candidates:
                session.send_message(WebRTCCandidate(
                    RTCIceCandidateInit(candidate=candidate, sdp_mid="0")
                ))
            session.remote_candidates.clear()
            while session.candidates and self._live(session):
                candidate = session.candidates.pop(0)
                await client.rpc("webrtc.candidate", sessionId=session_id,
                                 candidate=candidate, _transport=session.transport)
                if not self._live(session):
                    return
        except asyncio.CancelledError:
            self._close_signaling(session)
            # Core owns the wrapper task: cancellation is fully handled here.
        except EufySdkApiClientError as error:
            code = str(error) if str(error) in WEBRTC_ERROR_CODES else "webrtc_failed"
            if session.closed_before_answer and code not in {"webrtc_busy", "webrtc_releasing"}:
                code = "webrtc_closed"
            self._close_signaling(session, code)

    async def async_on_webrtc_candidate(self, session_id, candidate):
        session = self._signaling_sessions.get(session_id)
        if session is None or not self._live(session) or session.closed_before_answer:
            return
        if not session.answered:
            if len(session.candidates) >= MAX_PENDING_CANDIDATES:
                self._close_signaling(session, "webrtc_failed")
            else:
                session.candidates.append(candidate.candidate)
            return
        try:
            await self._signaling_client.rpc("webrtc.candidate", sessionId=session_id,
                candidate=candidate.candidate, _transport=session.transport)
            if not self._live(session):
                return
        except EufySdkApiClientError:
            self._close_signaling(session, "webrtc_failed")

    async def async_will_remove_from_hass(self):
        self._signaling_removed = True
        for session in tuple(self._signaling_sessions.values()):
            self._close_signaling(session, "webrtc_closed")
        if self._signaling_close_tasks:
            await asyncio.gather(*tuple(self._signaling_close_tasks))
        await super().async_will_remove_from_hass()
