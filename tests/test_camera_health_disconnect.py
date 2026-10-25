"""Publish camera diagnostic socket loss through the real HA state machine offline."""

# ruff: noqa: ANN001, ANN002, ANN003, ANN201, ANN202, ANN204
# ruff: noqa: D102, D107, INP001, PT009, PT027, SLF001
import asyncio
import logging
import unittest
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import WSMsgType
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import EntityPlatform
from test_camera_health import event, projection

import custom_components.eufy_sdk as integration
from custom_components.eufy_sdk.api import EufySdkApiClient
from custom_components.eufy_sdk.binary_sensor import (
    EufyCameraConnectivityBinarySensor,
    EufySdkBinarySensor,
)
from custom_components.eufy_sdk.const import DOMAIN
from custom_components.eufy_sdk.coordinator import EufySdkDataUpdateCoordinator
from custom_components.eufy_sdk.sensor import EufyCameraConnectivityStatusSensor

ROOT = str(Path(__file__).resolve().parents[1])


class OfflineSocket:
    """Yield queued synthetic events or a socket EOF without allocating I/O."""

    def __init__(self):
        self.closed = False
        self.frames = asyncio.Queue()

    def __aiter__(self):
        """Expose the synthetic receive stream."""
        return self

    async def __anext__(self):
        """Wait for a synthetic frame or transport EOF."""
        frame = await self.frames.get()
        if frame is None:
            self.closed = True
            raise StopAsyncIteration
        return Mock(type=WSMsgType.TEXT, json=lambda: frame)

    async def close(self):
        self.closed = True


class CameraHealthDisconnectTests(unittest.IsolatedAsyncioTestCase):
    """Use production event/reconnect wiring and real coordinator subscriptions."""

    async def asyncSetUp(self):
        self.hass = HomeAssistant(ROOT)
        self.hass.config_entries = Mock(async_forward_entry_setups=AsyncMock())
        devices = dr.DeviceRegistry(self.hass)
        self.hass.data[dr.DATA_REGISTRY] = devices
        await devices.async_load(load_empty=True)
        registry = er.EntityRegistry(self.hass)
        self.hass.data[er.DATA_REGISTRY] = registry
        await registry.async_load()
        self.socket = OfflineSocket()
        self.session = Mock(ws_connect=AsyncMock())
        self.client = EufySdkApiClient("synthetic-bridge", 3000, self.session)
        self.client._ws = self.socket
        self.client._schedule_reconnect = Mock()
        self.client.set_poll_ms = AsyncMock()
        self.client.get_properties = AsyncMock(return_value=[])
        self.client.auth_status = AsyncMock(return_value={"state": "ok"})
        self.client.list_solix_devices = AsyncMock(return_value=[])
        self.entry = Mock(
            data={"host": "synthetic-bridge", "port": 3000},
            options={},
            domain=DOMAIN,
            pref_disable_polling=False,
        )
        self.coordinator = EufySdkDataUpdateCoordinator(
            self.hass, logging.getLogger(__name__), name=DOMAIN, config_entry=None
        )
        self.coordinator.config_entry = self.entry
        self.coordinator.data = {
            "station": {
                "name": "Synthetic camera",
                "state": {"motionDetected": True},
                "available": False,
                "cameraHealth": projection(connectivity="online", revision=20),
            }
        }
        self.coordinator.last_update_success = True
        self.coordinator._project_camera_health(
            "station", self.coordinator.data["station"]["cameraHealth"]
        )
        self.coordinator.async_config_entry_first_refresh = AsyncMock()

        def make_client(**kwargs):
            self.client._on_event = kwargs["on_event"]
            self.client._on_reconnect = kwargs["on_reconnect"]
            return self.client

        with (
            patch.object(integration, "async_reconcile_property_entities"),
            patch.object(
                integration,
                "EufySdkDataUpdateCoordinator",
                return_value=self.coordinator,
            ),
            patch.object(integration, "EufySdkApiClient", side_effect=make_client),
            patch.object(
                integration, "async_get_clientsession", return_value=self.session
            ),
            patch.object(
                integration, "async_get_loaded_integration", return_value=Mock()
            ),
        ):
            await integration.async_setup_entry(self.hass, self.entry)

        self.binary = EufyCameraConnectivityBinarySensor(self.coordinator, "station")
        self.status = EufyCameraConnectivityStatusSensor(self.coordinator, "station")
        self.operational = EufySdkBinarySensor(
            self.coordinator, "station", {"name": "motionDetected", "type": "bool"}
        )
        self.entities = [self.binary, self.status, self.operational]
        for entity, domain, key in (
            (self.binary, "binary_sensor", "connectivity"),
            (self.status, "sensor", "connectivity_status"),
            (self.operational, "binary_sensor", "motion"),
        ):
            platform = EntityPlatform(
                hass=self.hass,
                logger=logging.getLogger(__name__),
                domain=domain,
                platform_name=DOMAIN,
                platform=None,
                scan_interval=timedelta(seconds=30),
                entity_namespace=None,
            )
            entity.add_to_platform_start(self.hass, platform, None)
            entity.entity_id = f"{domain}.synthetic_{key}"
            await entity.add_to_platform_finish()
            entity.async_write_ha_state()
        self.client._recv_task = asyncio.create_task(self.client._receive_loop())
        self.initial_operational = self.hass.states.get(self.operational.entity_id)
        self.assert_states("on", "online")

    async def asyncTearDown(self):
        for entity in self.entities:
            if entity.hass is not None:
                await entity.async_remove()
        task = self.client._recv_task
        await self.client.close()
        if task is not None:
            await task
        await self.coordinator.async_shutdown()
        await self.hass.async_block_till_done()

    def assert_states(self, binary, status):
        self.assertEqual(self.hass.states.get(self.binary.entity_id).state, binary)
        self.assertEqual(self.hass.states.get(self.status.entity_id).state, status)
        self.assertEqual(
            self.hass.states.get(self.operational.entity_id), self.initial_operational
        )

    async def disconnect(self):
        await self.socket.frames.put(None)
        await self.client._recv_task
        self.assert_states("unavailable", "unavailable")

    async def test_idle_eof_publishes_without_poll_or_general_coordinator_update(self):
        with patch.object(self.coordinator, "async_update_listeners") as notify:
            await self.disconnect()
        notify.assert_not_called()
        self.client._schedule_reconnect.assert_called_once()
        self.client.auth_status.assert_not_called()
        self.session.ws_connect.assert_not_called()
        self.assertTrue(self.coordinator.last_update_success)

    async def test_disconnect_after_event_with_late_contrary_capability(self):
        self.client._late_contrary_capability = True
        processed = asyncio.Event()
        on_event = self.client._on_event

        def dispatch(value):
            on_event(value)
            processed.set()

        self.client._on_event = dispatch
        await self.socket.frames.put(event(connectivity="offline", revision=21))
        await processed.wait()
        self.assert_states("off", "offline")
        self.assertEqual(
            self.hass.states.get(self.status.entity_id).attributes["revision"], 21
        )
        await self.disconnect()

    async def test_reset_without_receive_task_publishes_before_socket_close(self):
        self.client._recv_task.cancel()
        await self.client._recv_task
        self.client._recv_task = None
        self.client._ws = self.socket
        self.coordinator.async_update_listeners()

        async def close_socket():
            self.assert_states("unavailable", "unavailable")

        with patch.object(self.socket, "close", side_effect=close_socket):
            await self.client.reset_connection()
        self.assert_states("unavailable", "unavailable")

    async def test_close_without_receive_task_publishes_even_if_socket_close_fails(
        self,
    ):
        self.client._recv_task.cancel()
        await self.client._recv_task
        self.client._recv_task = None
        self.client._ws = self.socket
        self.coordinator.async_update_listeners()

        async def close_socket():
            self.assert_states("unavailable", "unavailable")
            raise OSError("synthetic failure")  # noqa: EM101, TRY003

        with (
            patch.object(self.socket, "close", side_effect=close_socket),
            self.assertRaises(OSError),
        ):
            await self.client.close()
        self.assert_states("unavailable", "unavailable")

    async def test_failing_disconnect_listener_cannot_interrupt_socket_close(self):
        self.client._recv_task.cancel()
        await self.client._recv_task
        self.client._recv_task = None
        self.client._ws = self.socket
        self.coordinator.async_update_listeners()
        failing = Mock(side_effect=RuntimeError("synthetic failure"))
        self.client._disconnect_listeners.insert(0, failing)
        self.addCleanup(self.client._disconnect_listeners.remove, failing)
        close = AsyncMock()

        with (
            patch.object(self.socket, "close", close),
            self.assertLogs("custom_components.eufy_sdk", logging.ERROR),
        ):
            await self.client.close()
        failing.assert_called_once_with()
        close.assert_awaited_once()
        self.assert_states("unavailable", "unavailable")

    async def test_reconnect_uses_reset_then_fresh_snapshot_without_stale_projection(
        self,
    ):
        await self.disconnect()
        entered, release = asyncio.Event(), asyncio.Event()
        fresh = deepcopy(self.coordinator.data["station"])
        fresh["cameraHealth"] = projection(
            incarnation="replacement", revision=0, connectivity="offline"
        )

        async def reply(*_args):
            entered.set()
            await release.wait()
            return {"devices": [{"sn": "station", **fresh}]}

        self.client.rpc = AsyncMock(side_effect=reply)
        self.session.ws_connect.return_value = OfflineSocket()
        await self.client._reconnect()
        await entered.wait()
        self.assert_states("unknown", "unknown")
        self.assertNotIn(
            "revision", self.hass.states.get(self.status.entity_id).attributes
        )
        release.set()
        await self.hass.async_block_till_done()
        self.assert_states("off", "offline")
        self.assertEqual(
            self.hass.states.get(self.status.entity_id).attributes["revision"], 0
        )

    async def test_disconnect_during_event_and_snapshot_cannot_republish_available(
        self,
    ):
        entered, release = asyncio.Event(), asyncio.Event()
        stale = deepcopy(self.coordinator.data)

        async def snapshot():
            entered.set()
            await release.wait()
            return stale

        self.coordinator._read_snapshot = snapshot
        refresh = asyncio.create_task(self.coordinator.async_refresh())
        await entered.wait()
        await self.socket.frames.put(event(connectivity="suspect", revision=21))
        await self.disconnect()
        release.set()
        await refresh
        self.assert_states("unavailable", "unavailable")
        self.assertEqual(
            self.coordinator.data["station"]["cameraHealth"]["revision"], 21
        )

    async def test_entity_removal_unsubscribes_socket_notifications(self):
        self.assertEqual(len(self.client._disconnect_listeners), 2)
        await self.binary.async_remove()
        await self.status.async_remove()
        self.entities = [self.operational]
        self.assertEqual(self.client._disconnect_listeners, [])
        await self.socket.frames.put(None)
        await self.client._recv_task
        self.assertEqual(
            self.hass.states.get(self.operational.entity_id), self.initial_operational
        )
