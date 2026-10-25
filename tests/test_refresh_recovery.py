"""Verify recovery timing and trigger storms with offline HA and virtual time."""

# ruff: noqa: ANN001, ANN002, ANN003, ANN201, ANN202, D102, INP001, PT009, PT027, SLF001

import asyncio
import logging
import time
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed

import custom_components.eufy_sdk as integration
from custom_components.eufy_sdk.api import EufySdkApiClientReplyTimeoutError
from custom_components.eufy_sdk.const import CONF_HOST, CONF_PORT
from custom_components.eufy_sdk.coordinator import EufySdkDataUpdateCoordinator
from custom_components.eufy_sdk.entity import EufySdkDeviceEntity

ROOT = str(Path(__file__).resolve().parents[1])


class RefreshRecoveryTests(unittest.IsolatedAsyncioTestCase):
    """Exercise the actual coordinator boundary shared by every refresh source."""

    async def asyncSetUp(self):
        self.now = 1000.0
        self.observed_base = time.time() * 1000 - 1000
        self.tasks = []
        self.timers = []
        self.clock = patch(
            "custom_components.eufy_sdk.coordinator.time.monotonic",
            side_effect=lambda: self.now,
        )
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.timer_patch = patch(
            "custom_components.eufy_sdk.coordinator.async_call_later",
            side_effect=self.call_later,
        )
        self.timer_patch.start()
        self.addCleanup(self.timer_patch.stop)
        self.hass = HomeAssistant(ROOT)
        self.hass.config_entries = Mock(async_forward_entry_setups=AsyncMock())
        self.hass.async_create_task = self.create_task
        self.client = Mock(connected=True, connection_generation=1)
        self.client.auth_status = AsyncMock(return_value={"state": "ok"})
        self.client.list_devices = AsyncMock(
            return_value=[{"sn": "fixture-device", "state": {"enabled": False}}]
        )
        self.client.list_solix_devices = AsyncMock(return_value=[])
        self.client.reset_connection = AsyncMock(return_value=True)
        self.coordinator = EufySdkDataUpdateCoordinator(
            hass=self.hass,
            logger=logging.getLogger("recovery-test"),
            name="fixture",
            update_interval=timedelta(minutes=10),
            config_entry=None,
        )
        self.coordinator.config_entry = SimpleNamespace(
            runtime_data=SimpleNamespace(client=self.client)
        )
        # Keep actual refresh/availability semantics; remove only HA's cooldown
        # so a debouncer cannot conceal a missing integration rate limit.
        self.coordinator.async_request_refresh = self.coordinator.async_refresh
        self.addAsyncCleanup(self.cleanup)
        await self.coordinator.async_refresh()

    async def cleanup(self):
        self.coordinator._clear_fast_retry()
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    def create_task(self, coro, *_args, **_kwargs):
        task = asyncio.create_task(coro)
        self.tasks.append(task)
        return task

    def call_later(self, _hass, delay, callback):
        timer = SimpleNamespace(
            deadline=self.now + delay, delay=delay, callback=callback, cancelled=False
        )

        def cancel():
            timer.cancelled = True

        timer.cancel = cancel
        self.timers.append(timer)
        return cancel

    async def drain(self):
        for _ in range(12):
            await asyncio.sleep(0)

    async def advance(self, seconds):
        target = self.now + seconds
        while True:
            pending = [
                timer
                for timer in self.timers
                if not timer.cancelled and timer.deadline <= target
            ]
            if not pending:
                break
            timer = min(pending, key=lambda item: item.deadline)
            self.now = timer.deadline
            timer.cancelled = True
            timer.callback(None)
            await self.drain()
        self.now = target
        await self.drain()

    async def induce_failure(self):
        self.client.auth_status.return_value = {"state": "pending"}
        await self.coordinator.async_refresh()
        self.assertFalse(self.coordinator.last_update_success)

    async def setup_callbacks(self):
        entry = Mock()
        entry.options = {}
        entry.data = {CONF_HOST: "fixture-bridge", CONF_PORT: 3000}
        entry.domain = "eufy_sdk"
        entry.async_on_unload = Mock()
        self.client.set_poll_ms = AsyncMock()
        self.client.get_properties = AsyncMock(return_value=[])
        callbacks = {}

        def make_client(**kwargs):
            callbacks.update(kwargs)
            return self.client

        with (
            patch.object(
                integration,
                "EufySdkDataUpdateCoordinator",
                return_value=self.coordinator,
            ),
            patch.object(integration, "EufySdkApiClient", side_effect=make_client),
            patch.object(integration, "async_get_clientsession", return_value=Mock()),
            patch.object(
                integration, "async_get_loaded_integration", return_value=Mock()
            ),
            patch.object(integration, "async_reconcile_property_entities"),
            patch.object(
                self.coordinator, "async_config_entry_first_refresh", new=AsyncMock()
            ),
        ):
            await integration.async_setup_entry(self.hass, entry)
        self.entry = entry
        return callbacks

    def observation(self, index=0):
        return {
            "event": "propertiesObserved",
            "deviceSn": "fixture-device",
            "source": "rtc",
            "observedAtMs": self.observed_base + index,
            "properties": {"enabled": True},
        }

    async def test_two_timeouts_then_bounded_retries_restore_availability(self):
        coordinator = self.coordinator
        entity = EufySdkDeviceEntity(coordinator, "fixture-device")
        self.client.list_devices.side_effect = EufySdkApiClientReplyTimeoutError()
        # Tolerated first reply, with the real timeout/reset/availability path.
        await coordinator.async_refresh()
        self.assertTrue(entity.available)
        self.assertTrue(coordinator._recovery_active)
        self.assertFalse(coordinator.snapshot_fresh)
        await self.advance(45)  # first retry encounters the second timeout
        self.assertFalse(entity.available)
        self.client.reset_connection.assert_awaited_once()
        self.assertEqual(coordinator._consecutive_reply_timeouts, 2)
        starts = []

        async def failing_read():
            starts.append(self.now)
            msg = "fixture-unavailable"
            raise UpdateFailed(msg)

        coordinator._read_snapshot = AsyncMock(side_effect=failing_read)
        await self.advance(90)
        await self.advance(180)
        self.assertEqual(starts, [1135, 1315])
        self.assertFalse(entity.available)
        coordinator._read_snapshot.side_effect = None
        coordinator._read_snapshot.return_value = {
            "fixture-device": {"state": {"enabled": True}}
        }
        await self.advance(300)
        self.assertTrue(entity.available)
        self.assertTrue(coordinator.snapshot_fresh)
        self.assertFalse(coordinator._recovery_active)
        self.assertEqual(coordinator._recovery_step, 0)

    async def test_schedule_repeats_300_until_complete_success(self):
        await self.induce_failure()
        for delay in (45, 90, 180, 300, 300, 300):
            timer = [item for item in self.timers if not item.cancelled]
            self.assertEqual(len(timer), 1)
            self.assertEqual(timer[0].delay, delay)
            before = self.client.auth_status.await_count
            await self.advance(delay)
            self.assertEqual(self.client.auth_status.await_count, before + 1)
        self.client.auth_status.return_value = {"state": "ok"}
        await self.advance(300)
        self.assertFalse(self.coordinator._recovery_active)
        self.assertFalse(any(not timer.cancelled for timer in self.timers))
        await self.induce_failure()
        self.assertEqual(self.timers[-1].delay, 45)

    async def test_retry_never_exceeds_normal_poll_interval(self):
        self.coordinator.update_interval = timedelta(seconds=60)
        await self.induce_failure()
        await self.advance(45)
        self.assertEqual(self.timers[-1].delay, 60)
        await self.advance(60)
        self.assertEqual(self.timers[-1].delay, 60)

    async def test_fresh_event_burst_requests_one_refresh_and_healthy_requests_none(
        self,
    ):
        callbacks = await self.setup_callbacks()
        coordinator = self.coordinator
        on_event = callbacks["on_event"]
        request = coordinator.async_request_refresh = AsyncMock()
        on_event(self.observation())
        await self.drain()
        request.assert_not_awaited()
        await self.induce_failure()
        for index in range(20):
            on_event(self.observation(index + 1))
        await self.drain()
        request.assert_awaited_once()
        await self.advance(31)
        on_event(self.observation(50))
        await self.drain()
        request.assert_awaited_once()
        request.reset_mock()
        self.client.auth_status.return_value = {"state": "ok"}
        await coordinator.async_refresh()
        on_event(self.observation(60))
        await self.drain()
        request.assert_not_awaited()
        await self.induce_failure()
        on_event(self.observation(70))
        await self.drain()
        request.assert_awaited_once()

    async def test_untrusted_or_stale_observations_do_not_trigger(self):
        callbacks = await self.setup_callbacks()
        await self.induce_failure()
        self.coordinator.async_request_refresh = AsyncMock()
        for changes in (
            {"source": "cloud"},
            {"observedAtMs": 0},
            {"properties": {}},
            {"deviceSn": "unknown-fixture"},
        ):
            callbacks["on_event"]({**self.observation(), **changes})
        await self.drain()
        self.coordinator.async_request_refresh.assert_not_awaited()

    async def test_event_inside_limit_is_deferred_and_recovers_before_retry(self):
        callbacks = await self.setup_callbacks()
        await self.induce_failure()
        before = self.client.auth_status.await_count
        self.client.auth_status.return_value = {"state": "ok"}
        await self.advance(5)
        for index in range(20):
            callbacks["on_event"](self.observation(index))
        await self.drain()
        self.assertEqual(self.client.auth_status.await_count, before)
        self.assertFalse(self.coordinator.last_update_success)
        await self.advance(24)
        self.assertEqual(self.client.auth_status.await_count, before)
        await self.advance(1)
        self.assertEqual(self.client.auth_status.await_count, before + 1)
        self.assertTrue(self.coordinator.last_update_success)
        self.assertFalse(any(not timer.cancelled for timer in self.timers))

    async def test_timer_reconnect_ready_and_poll_share_start_rate_limit(self):
        callbacks = await self.setup_callbacks()
        await self.induce_failure()
        before = self.client.auth_status.await_count
        for _ in range(20):
            callbacks["on_reconnect"]()
            callbacks["on_event"]({"event": "ready"})
            self.coordinator._fast_retry_fire(None)
        await self.coordinator.async_refresh()  # regular poll is also gated
        await self.drain()
        self.assertEqual(self.client.auth_status.await_count, before)
        self.assertEqual(sum(not timer.cancelled for timer in self.timers), 2)
        await self.advance(29)
        self.assertEqual(self.client.auth_status.await_count, before)
        await self.advance(1)
        self.assertEqual(self.client.auth_status.await_count, before + 1)
        for _ in range(10):
            callbacks["on_reconnect"]()
            callbacks["on_event"]({"event": "ready"})
        await self.drain()
        await self.advance(29)
        self.assertEqual(self.client.auth_status.await_count, before + 1)
        await self.advance(1)
        self.assertEqual(self.client.auth_status.await_count, before + 2)

    async def test_reset_reconnect_feedback_cannot_start_faster_than_30_seconds(self):
        callbacks = await self.setup_callbacks()
        starts = []
        self.client.list_devices.side_effect = EufySdkApiClientReplyTimeoutError()

        async def reset(**_kwargs):
            callbacks["on_reconnect"]()
            return True

        self.client.reset_connection.side_effect = reset
        await self.coordinator.async_refresh()  # tolerated
        await self.advance(30)
        await self.coordinator.async_refresh()  # reset schedules reconnect refresh
        await self.drain()
        self.assertEqual(self.client.list_devices.await_count, 3)
        self.assertFalse(self.coordinator.last_update_success)
        self.assertEqual(self.client.reset_connection.await_count, 1)
        for _ in range(3):
            previous = self.client.list_devices.await_count
            await self.advance(29)
            self.assertEqual(self.client.list_devices.await_count, previous)
            await self.advance(1)
            starts.append(self.now)
            self.assertEqual(self.client.list_devices.await_count, previous + 1)
        self.assertEqual(starts, [1060, 1090, 1120])

    async def test_no_overlapping_snapshot_even_across_multiple_requests(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def read():
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return {"fixture-device": {"state": {"enabled": True}}}

        self.coordinator._read_snapshot = read
        tasks = [
            asyncio.create_task(self.coordinator._async_update_data())
            for _ in range(10)
        ]
        await entered.wait()
        await self.drain()
        self.assertEqual(calls, 1)
        release.set()
        snapshots = await asyncio.gather(*tasks)
        self.assertTrue(all(snapshot == snapshots[0] for snapshot in snapshots))
        self.assertEqual(calls, 1)

    async def test_tolerated_refresh_remains_available_when_throttled(self):
        self.client.list_devices.side_effect = EufySdkApiClientReplyTimeoutError()
        await self.coordinator.async_refresh()
        before = self.client.list_devices.await_count
        await self.coordinator.async_refresh()
        self.assertTrue(self.coordinator.last_update_success)
        self.assertFalse(self.coordinator.snapshot_fresh)
        self.assertEqual(self.client.list_devices.await_count, before)
        await self.advance(30)
        self.assertFalse(self.coordinator.last_update_success)

    async def test_auth_failure_cancels_retry_and_deferred_timer(self):
        await self.induce_failure()
        await self.coordinator.async_refresh()
        self.assertEqual(sum(not timer.cancelled for timer in self.timers), 2)
        self.now += 30
        self.client.auth_status.return_value = {"state": "require_2fa"}
        with self.assertRaises(ConfigEntryAuthFailed):
            await self.coordinator._async_update_data()
        self.assertFalse(self.coordinator._recovery_active)
        self.assertFalse(any(not timer.cancelled for timer in self.timers))
        before = self.client.auth_status.await_count
        await self.advance(1000)
        self.assertEqual(self.client.auth_status.await_count, before)

    async def test_unload_cancels_all_timers(self):
        await self.setup_callbacks()
        await self.induce_failure()
        await self.coordinator.async_refresh()
        unload_callbacks = [
            call.args[0] for call in self.entry.async_on_unload.call_args_list
        ]
        unload_callbacks[0]()
        self.assertTrue(self.coordinator._recovery_stopped)
        self.assertFalse(any(not timer.cancelled for timer in self.timers))
        before = self.client.auth_status.await_count
        await self.advance(1000)
        self.coordinator.request_recovery_refresh()
        self.coordinator._fast_retry_fire(None)
        self.coordinator._refresh_limit_fire(None)
        await self.drain()
        self.assertEqual(self.client.auth_status.await_count, before)

    async def test_unload_during_snapshot_prevents_rearming_and_queued_reads(self):
        await self.setup_callbacks()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def failing_read():
            entered.set()
            await release.wait()
            msg = "fixture-unavailable"
            raise UpdateFailed(msg)

        self.coordinator._read_snapshot = AsyncMock(side_effect=failing_read)
        task = asyncio.create_task(self.coordinator.async_refresh())
        await entered.wait()
        self.entry.async_on_unload.call_args_list[0].args[0]()
        release.set()
        await task
        await self.coordinator.async_refresh()
        self.coordinator._read_snapshot.assert_awaited_once()
        self.assertFalse(any(not timer.cancelled for timer in self.timers))
        self.assertTrue(self.coordinator._recovery_stopped)
