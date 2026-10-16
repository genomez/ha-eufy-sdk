# ruff: noqa: ANN201, D100, D101, D102, INP001, PT009, PT027, SLF001

"""Offline counterparts of the retained P1 signaling cases; no HA server is started."""

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

import aiohttp
from homeassistant.components.camera.webrtc import (
    WebRTCAnswer,
    WebRTCCandidate,
    WebRTCError,
)
from webrtc_models import RTCIceCandidateInit

from custom_components.eufy_sdk.api import (
    EufySdkApiClient,
    EufySdkApiClientCommunicationError,
    EufySdkApiClientError,
    WEBRTC_ERROR_CODES,
)
from custom_components.eufy_sdk.camera import (
    EufySdkCamera,
    EufySdkS1Camera,
    async_setup_entry,
)
from custom_components.eufy_sdk.coordinator import EufySdkDataUpdateCoordinator
from custom_components.eufy_sdk.native_webrtc import (
    MAX_PENDING_CANDIDATES,
    NativeWebRTCMixin,
    is_s1_camera,
)


def roster():
    return SimpleNamespace(
        data={
            "SYNTHETIC_CAMERA": {"stationSn": "SYNTHETIC_STATION", "stream": "/stream/camera"},
            "SYNTHETIC_STATION": {"model": "t9000-synthetic"},
            "SYNTHETIC_OTHER": {"stream": "/stream/other"},
        },
        device_serial_counts={"SYNTHETIC_CAMERA": 1, "SYNTHETIC_STATION": 1, "SYNTHETIC_OTHER": 1},
    )


class FakeClient:
    def __init__(self):
        self.transport = object()
        self.connected = True
        self.callbacks = {}
        self.calls = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.error = None
        self.keep_late_answer = False

    def register_webrtc_session(self, session_id, callback):
        self.callbacks[session_id] = callback
        return self.transport

    def remove_webrtc_session(self, session_id):
        self.callbacks.pop(session_id, None)

    async def rpc(self, command, **args):
        if args["_transport"] is not self.transport or not self.connected:
            raise EufySdkApiClientCommunicationError("not connected")
        self.calls.append((command, args))
        if command == "webrtc.open":
            self.started.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                if not self.keep_late_answer:
                    raise
                await self.release.wait()
            if self.error:
                raise EufySdkApiClientError(self.error)
            return {"sdp": "synthetic-answer"}
        return {}

    def push(self, session_id, event="webrtc.candidate"):
        if callback := self.callbacks.get(session_id):
            callback({"event": event, "candidate": "synthetic-remote-candidate"})


class FakeBase:
    def __init__(self, client):
        self.coordinator = roster()
        self.coordinator.config_entry = SimpleNamespace(runtime_data=SimpleNamespace(client=client))
        self._sn = "SYNTHETIC_CAMERA"
        self.hass = SimpleNamespace(async_create_task=asyncio.create_task)
        self.removed = False
        self.ice_servers = [{"urls": ["stun:stun.home-assistant.io:3478", "stun:stun.home-assistant.io:80"]}]

    def async_get_webrtc_client_configuration(self):
        return SimpleNamespace(configuration=SimpleNamespace(to_dict=lambda: {"iceServers": self.ice_servers}))

    async def async_will_remove_from_hass(self):
        self.removed = True


class FakeCamera(NativeWebRTCMixin, FakeBase):
    pass


class NativeWebRTCTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = FakeClient()
        self.camera = FakeCamera(self.client)
        self.messages = []

    async def offer(self, session_id="synthetic-session", messages=None):
        task = asyncio.create_task(self.camera.async_handle_async_webrtc_offer(
            "synthetic-offer", session_id, (self.messages if messages is None else messages).append
        ))
        await self.client.started.wait()
        return task

    async def drain_closes(self):
        if self.camera._signaling_close_tasks:
            await asyncio.gather(*tuple(self.camera._signaling_close_tasks))

    def commands(self, name):
        return [args for command, args in self.client.calls if command == name]

    async def asyncTearDown(self):
        self.client.release.set()
        await self.camera.async_will_remove_from_hass()

    async def test_p1_01_close_before_answer_is_exactly_once(self):
        self.client.release.clear()
        task = await self.offer()
        self.camera.close_webrtc_session("synthetic-session")
        self.camera.close_webrtc_session("synthetic-session")
        await task
        await self.drain_closes()
        self.assertEqual(len(self.commands("webrtc.close")), 1)
        self.assertEqual(self.messages, [])
        self.assertEqual(self.camera._signaling_sessions, {})

    async def test_p1_02_offer_task_cancellation_closes_forwarded_session(self):
        self.client.release.clear()
        task = await self.offer()
        task.cancel()
        await task
        await self.drain_closes()
        self.assertEqual(len(self.commands("webrtc.close")), 1)
        self.assertFalse(self.camera._signaling_sessions)
        self.assertEqual(self.messages, [])

    async def test_p1_03_late_answer_and_candidate_after_close_are_discarded(self):
        self.client.release.clear()
        self.client.keep_late_answer = True
        task = await self.offer()
        callback = self.client.callbacks["synthetic-session"]
        self.camera.close_webrtc_session("synthetic-session")
        self.client.release.set()
        await task
        callback({"event": "webrtc.candidate", "candidate": "synthetic-late"})
        await self.drain_closes()
        self.assertEqual(self.messages, [])
        self.assertEqual(len(self.commands("webrtc.close")), 1)

    async def test_p1_04_candidates_buffer_both_directions_and_stop_after_close(self):
        self.client.release.clear()
        task = await self.offer()
        await self.camera.async_on_webrtc_candidate("synthetic-session", RTCIceCandidateInit(candidate="synthetic-local"))
        self.client.push("synthetic-session")
        self.assertEqual(self.messages, [])
        self.assertEqual(self.commands("webrtc.candidate"), [])
        self.client.release.set()
        await task
        self.assertIsInstance(self.messages[0], WebRTCAnswer)
        self.assertIsInstance(self.messages[1], WebRTCCandidate)
        self.assertEqual(len(self.commands("webrtc.candidate")), 1)
        self.camera.close_webrtc_session("synthetic-session")
        await self.drain_closes()
        await self.camera.async_on_webrtc_candidate("synthetic-session", RTCIceCandidateInit(candidate="synthetic-late"))
        self.client.push("synthetic-session")
        self.assertEqual(len(self.commands("webrtc.candidate")), 1)
        self.assertEqual(len(self.messages), 2)

    async def test_p1_05_closing_one_viewer_preserves_another(self):
        await (await self.offer())
        other_messages = []
        await (await self.offer("synthetic-other-session", other_messages))
        self.camera.close_webrtc_session("synthetic-session")
        await self.drain_closes()
        self.client.push("synthetic-other-session")
        await self.camera.async_on_webrtc_candidate("synthetic-other-session", RTCIceCandidateInit(candidate="synthetic-local"))
        self.assertIn("synthetic-other-session", self.camera._signaling_sessions)
        self.assertIsInstance(other_messages[-1], WebRTCCandidate)
        self.assertEqual(len(self.commands("webrtc.candidate")), 1)

    async def test_p1_06_bridge_loss_invalidates_without_offer_retry(self):
        await (await self.offer())
        self.client.connected = False
        self.client.push("synthetic-session", "webrtc.closed")
        await self.drain_closes()
        await self.camera.async_on_webrtc_candidate("synthetic-session", RTCIceCandidateInit(candidate="synthetic-late"))
        self.assertFalse(self.camera._signaling_sessions)
        self.assertIsInstance(self.messages[-1], WebRTCError)
        self.assertEqual(self.messages[-1].code, "webrtc_closed")
        self.assertEqual(len(self.commands("webrtc.open")), 1)
        self.assertEqual(self.commands("webrtc.candidate"), [])

    async def test_p1_07_entity_removal_closes_and_old_callback_cannot_affect_replacement(self):
        await (await self.offer())
        old = self.camera
        await old.async_will_remove_from_hass()
        self.assertTrue(old.removed)
        self.assertEqual(self.messages[-1].code, "webrtc_closed")
        self.camera = FakeCamera(self.client)
        await (await self.offer("synthetic-replacement"))
        old.close_webrtc_session("synthetic-session")
        self.assertIn("synthetic-replacement", self.camera._signaling_sessions)
        self.assertEqual(len(self.commands("webrtc.close")), 1)

    async def test_p1_08_bridge_busy_keeps_closed_error_schema(self):
        self.client.error = "webrtc_busy"
        await (await self.offer())
        await self.drain_closes()
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0], WebRTCError("webrtc_busy", "webrtc_busy"))
        self.assertFalse(self.camera._signaling_sessions)

    async def test_ha_ice_configuration_is_forwarded_unchanged(self):
        await (await self.offer())
        self.assertEqual(self.commands("webrtc.open")[0]["iceServers"], self.camera.ice_servers)

    async def test_candidate_bounds_fail_closed_in_both_directions(self):
        for direction in ("local", "remote"):
            with self.subTest(direction=direction):
                self.client.started.clear()
                self.client.release.clear()
                task = await self.offer(direction)
                for _ in range(MAX_PENDING_CANDIDATES + 1):
                    if direction == "local":
                        await self.camera.async_on_webrtc_candidate(direction, RTCIceCandidateInit(candidate="synthetic"))
                    else:
                        self.client.push(direction)
                await task
                await self.drain_closes()
                self.assertNotIn(direction, self.camera._signaling_sessions)
                self.assertEqual(self.messages[-1].code, "webrtc_failed")
        self.assertEqual(self.commands("webrtc.candidate"), [])

    async def test_lost_binding_refuses_before_forwarding(self):
        self.camera.coordinator.device_serial_counts["SYNTHETIC_STATION"] = 2
        await self.camera.async_handle_async_webrtc_offer("synthetic", "synthetic-session", self.messages.append)
        self.assertEqual(self.messages, [WebRTCError("webrtc_unsupported", "webrtc_unsupported")])
        self.assertEqual(self.client.calls, [])


class NativeWebRTCSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_and_ambiguous_bindings_fail_closed(self):
        for label, data, counts in [
            ("missing parent", {"C": {"stationSn": "P"}}, {"C": 1}),
            ("duplicate parent", {"C": {"stationSn": "P"}, "P": {"model": "T9000"}}, {"C": 1, "P": 2}),
            ("self parent", {"C": {"stationSn": "C", "model": "T9000"}}, {"C": 1}),
            ("blank parent", {"C": {"stationSn": " P"}, " P": {"model": "T9000"}}, {"C": 1, " P": 1}),
            ("duplicate camera", {"C": {"stationSn": "P"}, "P": {"model": "T9000"}}, {"C": 2, "P": 1}),
            ("other station", {"C": {"stationSn": "P"}, "P": {"model": "T8000"}}, {"C": 1, "P": 1}),
        ]:
            with self.subTest(label=label):
                self.assertFalse(is_s1_camera(SimpleNamespace(data=data, device_serial_counts=counts), "C"))
        self.assertTrue(is_s1_camera(roster(), "SYNTHETIC_CAMERA"))

    async def test_p1_09_10_actual_camera_classes_preserve_native_and_legacy_capabilities(self):
        coordinator = roster()
        coordinator.config_entry = SimpleNamespace(runtime_data=SimpleNamespace(client=FakeClient()))
        entry = SimpleNamespace(data={"host": "synthetic-host", "port": 3000}, runtime_data=SimpleNamespace(coordinator=coordinator))
        entities = []
        await async_setup_entry(Mock(), entry, lambda items: entities.extend(items))
        native, legacy = entities
        self.assertIs(type(native), EufySdkS1Camera)
        self.assertIs(type(legacy), EufySdkCamera)
        self.assertTrue(native._supports_native_async_webrtc)
        self.assertFalse(legacy._supports_native_async_webrtc)
        native._async_get_supported_webrtc_provider = AsyncMock()
        await native.async_refresh_providers(write_state=False)
        native._async_get_supported_webrtc_provider.assert_not_awaited()
        self.assertIsNone(native._webrtc_provider)
        self.assertIsNone(native.stream)
        self.assertEqual(native.camera_capabilities.frontend_stream_types, {"web_rtc"})
        legacy._webrtc_provider = object()
        self.assertEqual(legacy.camera_capabilities.frontend_stream_types, {"hls", "web_rtc"})

    async def test_coordinator_retains_duplicates_before_dictionary_collapse(self):
        coordinator = EufySdkDataUpdateCoordinator.__new__(EufySdkDataUpdateCoordinator)
        client = Mock(connected=True)
        client.auth_status = AsyncMock(return_value={"state": "ok"})
        client.list_devices = AsyncMock(return_value=[{"sn": "C"}, {"sn": "P"}, {"sn": "P"}])
        client.list_solix_devices = AsyncMock(return_value=[])
        coordinator.config_entry = SimpleNamespace(runtime_data=SimpleNamespace(client=client))
        result = await coordinator._read_snapshot()
        self.assertEqual(set(result), {"C", "P"})
        self.assertEqual(coordinator.device_serial_counts, {"C": 1, "P": 2})


class NativeWebRTCApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_retired_transport_cannot_send_on_replacement(self):
        client = EufySdkApiClient("synthetic-host", 3000, Mock())
        client._ws = Mock(closed=False)
        transport = client.register_webrtc_session("synthetic", Mock())
        client._ws = Mock(closed=False, send_json=AsyncMock())
        with self.assertRaises(EufySdkApiClientCommunicationError):
            await client.rpc("webrtc.candidate", _transport=transport, sessionId="synthetic", candidate="synthetic")
        client._ws.send_json.assert_not_awaited()

    async def test_webrtc_errors_are_allowlisted_without_raw_detail(self):
        for code in (*WEBRTC_ERROR_CODES, "synthetic-private-detail", "command_rejected"):
            with self.subTest(code=code):
                client = EufySdkApiClient("synthetic-host", 3000, Mock())
                client._ws = Mock(closed=False)
                async def reply(payload):
                    client._pending[payload["id"]].set_result({"ok": False, "code": code, "error": "synthetic-private-detail"})
                client._ws.send_json = AsyncMock(side_effect=reply)
                with self.assertRaises(EufySdkApiClientError) as caught:
                    await client.rpc("webrtc.open", sessionId="synthetic")
                self.assertEqual(str(caught.exception), code if code in WEBRTC_ERROR_CODES else "bridge_request_rejected")

    async def test_private_pushes_never_enter_general_event_callback(self):
        general = Mock()
        signaling = Mock()
        client = EufySdkApiClient("synthetic-host", 3000, Mock(), on_event=general)
        frames = [
            {"event": "webrtc.candidate", "sessionId": "synthetic", "candidate": "synthetic"},
            {"event": "webrtc.closed", "sessionId": "synthetic"},
            {"event": "webrtc.candidate", "sessionId": "unowned", "candidate": "synthetic"},
            {"event": "synthetic-other-event"},
        ]
        class Frames:
            closed = False
            def __aiter__(self):
                return self.read()
            async def read(self):
                for frame in frames:
                    yield SimpleNamespace(type=aiohttp.WSMsgType.TEXT, json=lambda frame=frame: frame)
        client._ws = Frames()
        client._closing = True
        client.register_webrtc_session("synthetic", signaling)
        await client._receive_loop()
        general.assert_called_once_with({"event": "synthetic-other-event"})
        self.assertEqual(signaling.call_count, 3)
        self.assertEqual(client._webrtc_callbacks, {})

    async def test_close_and_reset_invalidate_each_callback_once(self):
        for action in ("close", "reset_connection"):
            with self.subTest(action=action):
                client = EufySdkApiClient("synthetic-host", 3000, Mock())
                client._closing = True
                client._ws = Mock(closed=False, close=AsyncMock())
                callback = Mock()
                client.register_webrtc_session("synthetic", callback)
                await getattr(client, action)()
                callback.assert_called_once_with({"event": "webrtc.closed", "code": "webrtc_closed"})
                self.assertEqual(client._webrtc_callbacks, {})
