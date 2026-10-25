"""Exercise guard-mode service waits without network or device access."""

# ruff: noqa: ANN001, ANN201, ANN202, D102, INP001, PT009, PT027, SLF001

import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.core import HomeAssistant

import custom_components.eufy_sdk as integration
from custom_components.eufy_sdk.alarm_control_panel import EufySdkAlarmControlPanel
from custom_components.eufy_sdk.api import (
    EufySdkApiClient,
    EufySdkApiClientCommunicationError,
    EufySdkApiClientError,
)
from custom_components.eufy_sdk.coordinator import EufySdkDataUpdateCoordinator
from custom_components.eufy_sdk.select import EufySdkSelect

ROOT = str(Path(__file__).resolve().parents[1])


class GuardModeTimeoutTests(unittest.IsolatedAsyncioTestCase):
    """Use the real RPC deadline and entity entry points on a virtual clock."""

    async def asyncSetUp(self):
        self.loop = asyncio.get_running_loop()
        self.now = self.loop.time()
        clock = patch.object(self.loop, "time", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.client = EufySdkApiClient("synthetic-bridge", 3000, Mock())
        self.frames = []
        self.client._ws = Mock(closed=False)
        self.client._ws.send_json = AsyncMock(side_effect=self.frames.append)
        self.client._ws.close = AsyncMock()
        self.coordinator = SimpleNamespace(
            data={"synthetic-station": {"state": {"armingMode": 0}}},
            config_entry=SimpleNamespace(
                runtime_data=SimpleNamespace(client=self.client)
            ),
            last_update_success=True,
        )
        timer = patch(
            "custom_components.eufy_sdk.entity.async_call_later", return_value=Mock()
        )
        timer.start()
        self.addCleanup(timer.stop)

    async def advance(self, seconds=0):
        self.now += seconds
        for _ in range(4):
            await asyncio.sleep(0)

    def reply(self, frame, **fields: object):
        future = self.client._pending[frame["id"]]
        future.set_result({"id": frame["id"], "ok": True, **fields})

    def select(self):
        entity = EufySdkSelect(
            self.coordinator,
            "synthetic-station",
            {
                "name": "armingMode",
                "type": "enum",
                "writable": True,
                "enumValues": {"0": "Away", "1": "Home", "63": "Disarmed"},
            },
        )
        entity.hass = Mock()
        entity.async_write_ha_state = Mock()
        return entity

    async def test_select_uses_guard_timeout(self):
        entity = self.select()
        with patch.object(self.client, "rpc", new=AsyncMock()) as rpc:
            await entity.async_select_option("Home")
        rpc.assert_awaited_once_with(
            "device.set", timeout=60, sn="synthetic-station", name="armingMode", value=1
        )

    async def test_every_alarm_arm_and_disarm_uses_guard_timeout(self):
        panel = EufySdkAlarmControlPanel(self.coordinator, "synthetic-station")
        for method, value in (
            ("async_alarm_arm_home", 1),
            ("async_alarm_arm_away", 0),
            ("async_alarm_disarm", 63),
            ("async_alarm_arm_custom_bypass", 3),
            ("async_alarm_arm_night", 4),
            ("async_alarm_arm_vacation", 5),
        ):
            with (
                self.subTest(method=method),
                patch.object(self.client, "rpc", new=AsyncMock()) as rpc,
            ):
                await getattr(panel, method)()
                rpc.assert_awaited_once_with(
                    "device.set",
                    timeout=60,
                    sn="synthetic-station",
                    name="armingMode",
                    value=value,
                )

    async def test_other_properties_and_actions_still_time_out_at_15_seconds(self):
        for name, value in (("light", True), ("lightPower", True), ("volume", 20)):
            with self.subTest(name=name):
                task = asyncio.create_task(
                    self.client.set_property("synthetic-station", name, value)
                )
                await self.advance()
                await self.advance(14)
                self.assertFalse(task.done())
                await self.advance(2)
                with self.assertRaisesRegex(
                    EufySdkApiClientCommunicationError, "^bridge_request_timeout$"
                ):
                    await task
                self.assertEqual(self.client._pending, {})
        task = asyncio.create_task(self.client.action("synthetic-station", "siren"))
        await self.advance()
        await self.advance(16)
        with self.assertRaisesRegex(
            EufySdkApiClientCommunicationError, "^bridge_request_timeout$"
        ):
            await task

    async def test_blocking_ha_service_accepts_a_reply_after_26_seconds(self):
        hass = HomeAssistant(ROOT)
        entity = self.select()

        async def select_option(call):
            await entity.async_select_option(call.data["option"])

        hass.services.async_register("select", "select_option", select_option)
        task = asyncio.create_task(
            hass.services.async_call(
                "select", "select_option", {"option": "Home"}, blocking=True
            )
        )
        await self.advance()
        frame = self.frames[-1]
        await self.advance(26)
        self.assertFalse(task.done())
        self.reply(frame)
        await task
        self.assertEqual(entity.current_option, "Home")
        self.assertEqual(self.client._pending, {})

    async def test_real_guard_timeout_keeps_error_and_cleans_pending_request(self):
        task = asyncio.create_task(
            self.client.set_property("synthetic-station", "armingMode", 1)
        )
        await self.advance()
        await self.advance(59)
        self.assertFalse(task.done())
        await self.advance(2)
        with self.assertRaisesRegex(
            EufySdkApiClientCommunicationError, "^bridge_request_timeout$"
        ):
            await task
        self.assertEqual(self.client._pending, {})
        self.assertEqual([frame["cmd"] for frame in self.frames], ["device.set"])

    async def test_bridge_failure_keeps_existing_rejection_translation(self):
        task = asyncio.create_task(
            self.client.set_property("synthetic-station", "armingMode", 1)
        )
        await self.advance()
        await self.advance(26)
        frame = self.frames[-1]
        self.client._pending[frame["id"]].set_result(
            {"id": frame["id"], "ok": False, "error": "synthetic command timeout"}
        )
        with self.assertRaisesRegex(EufySdkApiClientError, "^bridge_request_rejected$"):
            await task
        self.assertEqual(self.client._pending, {})

    async def test_snapshot_and_other_command_finish_while_guard_write_waits(self):
        task = asyncio.create_task(
            self.client.set_property("synthetic-station", "armingMode", 1)
        )
        await self.advance()
        guard_frame = self.frames[-1]
        original_send = self.client._ws.send_json.side_effect

        def respond(frame):
            original_send(frame)
            if frame["cmd"] == "auth.status":
                self.reply(frame, auth={"state": "ok"})
            elif frame["cmd"] == "devices.list":
                self.reply(frame, devices=[{"sn": "synthetic-station", "state": {}}])
            else:
                self.reply(frame)

        self.client._ws.send_json.side_effect = respond
        coordinator = EufySdkDataUpdateCoordinator.__new__(EufySdkDataUpdateCoordinator)
        coordinator.config_entry = self.coordinator.config_entry
        coordinator.data = self.coordinator.data
        coordinator._clear_fast_retry = Mock()
        snapshot = await coordinator._async_update_data()
        await self.client.set_property("synthetic-station", "light", value=False)
        self.assertIn("synthetic-station", snapshot)
        self.assertEqual(coordinator.completed_snapshot_generation, 1)
        self.assertFalse(task.done())
        self.reply(guard_frame)
        await task
        self.assertEqual(self.client._pending, {})

    async def test_unload_releases_pending_guard_wait_immediately(self):
        task = asyncio.create_task(
            self.client.set_property("synthetic-station", "armingMode", 1)
        )
        await self.advance()
        hass = Mock()
        hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
        entry = SimpleNamespace(runtime_data=SimpleNamespace(client=self.client))
        self.assertTrue(await integration.async_unload_entry(hass, entry))
        with self.assertRaisesRegex(
            EufySdkApiClientCommunicationError, "^connection closed$"
        ):
            await task
        self.assertTrue(self.client._closing)
        self.assertIsNone(self.client._ws)
        self.assertEqual(self.client._pending, {})

    async def test_caller_cancellation_propagates_and_cleans_pending_request(self):
        task = asyncio.create_task(
            self.client.set_property("synthetic-station", "armingMode", 1)
        )
        await self.advance()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.client._pending, {})
        self.assertEqual(self.client._bounded_requests, {})
        self.assertEqual([frame["cmd"] for frame in self.frames], ["device.set"])
