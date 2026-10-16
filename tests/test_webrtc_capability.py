# ruff: noqa: ANN201, D100, D101, D102, INP001, PT009, PT027, SLF001

"""Offline hello ordering, camera fallback and reconnect reload checks."""

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

import aiohttp

from custom_components.eufy_sdk.api import EufySdkApiClient
from custom_components.eufy_sdk.camera import (
    EufySdkCamera,
    EufySdkS1Camera,
    async_setup_entry,
)


class HelloSocket:
    def __init__(self, hello):
        self.closed = False
        self.frames = asyncio.Queue()
        self.frames.put_nowait({"event": "hello", **hello})

    def __aiter__(self):
        return self

    async def __anext__(self):
        frame = await self.frames.get()
        if frame is None:
            raise StopAsyncIteration
        return SimpleNamespace(type=aiohttp.WSMsgType.TEXT, json=lambda: frame)

    async def send_json(self, request):
        self.frames.put_nowait({"id": request["id"], "ok": True, "auth": {"state": "ok"}})

    async def close(self):
        self.closed = True
        self.frames.put_nowait(None)


def camera_entry(client):
    coordinator = SimpleNamespace(
        data={
            "SYNTHETIC_CAMERA": {"stationSn": "SYNTHETIC_STATION", "stream": "/stream/camera"},
            "SYNTHETIC_STATION": {"model": "T9000-synthetic"},
            "SYNTHETIC_OTHER": {"stream": "/stream/other"},
        },
        device_serial_counts={"SYNTHETIC_CAMERA": 1, "SYNTHETIC_STATION": 1, "SYNTHETIC_OTHER": 1},
    )
    callbacks = []
    entry = SimpleNamespace(
        entry_id="synthetic-entry",
        data={"host": "synthetic-host", "port": 3000},
        runtime_data=SimpleNamespace(coordinator=coordinator, client=client),
        async_on_unload=callbacks.append,
    )
    coordinator.config_entry = entry
    return entry, callbacks


class WebRTCCapabilityTests(unittest.IsolatedAsyncioTestCase):
    async def client_for(self, hellos):
        sockets = [HelloSocket(hello) for hello in hellos]
        reconnected = asyncio.Event()
        session = Mock(ws_connect=AsyncMock(side_effect=sockets))
        client = EufySdkApiClient("synthetic-host", 3000, session, on_reconnect=reconnected.set)
        self.addAsyncCleanup(client.close)
        await client.connect()
        # The bridge sends hello before accepting this RPC. Successful setup's
        # first refresh uses this same ordered receive loop before camera setup.
        self.assertEqual(await client.auth_status(), {"state": "ok"})
        return client, sockets, reconnected

    async def reconnect(self, client, socket, reconnected):
        reconnected.clear()
        await socket.close()
        async with asyncio.timeout(1):
            await reconnected.wait()
            await client.auth_status()

    async def cameras(self, client):
        entry, callbacks = camera_entry(client)
        hass = Mock()
        entities = []
        await async_setup_entry(hass, entry, lambda items: entities.extend(items))
        return hass, entities, callbacks

    async def test_true_hello_precedes_rpc_reply_and_selects_native_camera(self):
        client, _, _ = await self.client_for([{"webrtcRelay": True}])
        hass, entities, _ = await self.cameras(client)
        self.assertTrue(client.webrtc_relay_enabled)
        self.assertIs(type(entities[0]), EufySdkS1Camera)
        self.assertTrue(entities[0]._supports_native_async_webrtc)
        self.assertIs(type(entities[1]), EufySdkCamera)
        hass.config_entries.async_schedule_reload.assert_not_called()

    async def test_false_absent_and_malformed_hello_keep_existing_stream_class(self):
        for hello in ({}, {"webrtcRelay": False}, {"webrtcRelay": 1}, {"webrtcRelay": "true"}, {"webrtcRelay": None}):
            with self.subTest(hello=hello):
                client, _, _ = await self.client_for([hello])
                hass, entities, _ = await self.cameras(client)
                self.assertFalse(client.webrtc_relay_enabled)
                self.assertTrue(all(type(entity) is EufySdkCamera for entity in entities))
                self.assertFalse(entities[0]._supports_native_async_webrtc)
                self.assertEqual(entities[0].camera_capabilities.frontend_stream_types, {"hls"})
                hass.config_entries.async_schedule_reload.assert_not_called()

    async def test_reconnect_capability_change_schedules_one_reload_in_both_directions(self):
        for initial, changed in (({"webrtcRelay": False}, {"webrtcRelay": True}), ({"webrtcRelay": True}, {})):
            with self.subTest(initial=initial):
                client, sockets, reconnected = await self.client_for([initial, initial, changed, changed, initial])
                hass, _, callbacks = await self.cameras(client)
                await self.reconnect(client, sockets[0], reconnected)
                hass.config_entries.async_schedule_reload.assert_not_called()
                await self.reconnect(client, sockets[1], reconnected)
                hass.config_entries.async_schedule_reload.assert_called_once_with("synthetic-entry")
                await self.reconnect(client, sockets[2], reconnected)
                await self.reconnect(client, sockets[3], reconnected)
                hass.config_entries.async_schedule_reload.assert_called_once_with("synthetic-entry")
                for callback in callbacks:
                    callback()
                self.assertIsNone(client._webrtc_relay_listener)

    async def test_fresh_setup_with_new_capability_does_not_reload_loop(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                old_client, old_sockets, old_reconnected = await self.client_for([{"webrtcRelay": not enabled}, {"webrtcRelay": enabled}])
                old_hass, _, callbacks = await self.cameras(old_client)
                await self.reconnect(old_client, old_sockets[0], old_reconnected)
                old_hass.config_entries.async_schedule_reload.assert_called_once_with("synthetic-entry")
                for callback in callbacks:
                    callback()
                await old_client.close()
                client, sockets, reconnected = await self.client_for([{"webrtcRelay": enabled}, {"webrtcRelay": enabled}])
                hass, entities, _ = await self.cameras(client)
                self.assertIs(type(entities[0]), EufySdkS1Camera if enabled else EufySdkCamera)
                await self.reconnect(client, sockets[0], reconnected)
                hass.config_entries.async_schedule_reload.assert_not_called()

    async def test_unload_and_client_close_remove_capability_listener(self):
        for action in ("unload", "close"):
            with self.subTest(action=action):
                client, sockets, reconnected = await self.client_for([{}, {"webrtcRelay": True}])
                hass, _, callbacks = await self.cameras(client)
                if action == "unload":
                    for callback in callbacks:
                        callback()
                    await self.reconnect(client, sockets[0], reconnected)
                else:
                    await client.close()
                self.assertIsNone(client._webrtc_relay_listener)
                hass.config_entries.async_schedule_reload.assert_not_called()
