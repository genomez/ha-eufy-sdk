"""Exercise slow replies and retained failure evidence without external I/O."""

# ruff: noqa: ANN001, ANN201, ANN202, D102, INP001, PT009, PT027, SLF001

import asyncio
import json
import logging
import traceback
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from test_camera_health import projection as camera_projection
from test_storage_telemetry import NOW
from test_storage_telemetry import device as storage_device

import custom_components.eufy_sdk as integration
from custom_components.eufy_sdk.api import (
    EufySdkApiClient,
    EufySdkApiClientAuthenticationError,
    EufySdkApiClientCommunicationError,
    EufySdkApiClientError,
    EufySdkApiClientReplyTimeoutError,
)
from custom_components.eufy_sdk.coordinator import EufySdkDataUpdateCoordinator
from custom_components.eufy_sdk.diagnostics import async_get_config_entry_diagnostics
from custom_components.eufy_sdk.entity import EufySdkDeviceEntity, EufySolixEntity
from custom_components.eufy_sdk.storage_telemetry import FRESHNESS_MS, storage_reading

ROOT = str(Path(__file__).resolve().parents[1])
LOGGER = logging.getLogger("custom_components.eufy_sdk")
PRIVATE = "synthetic-private-marker"


class SlowReplyIsolationTests(unittest.IsolatedAsyncioTestCase):
    """Run actual RPC deadlines and HA coordinator availability on a virtual clock."""

    async def asyncSetUp(self):
        self.loop = asyncio.get_running_loop()
        self.now = self.loop.time()
        for clock in (
            patch.object(self.loop, "time", side_effect=lambda: self.now),
            patch(
                "custom_components.eufy_sdk.api.time.monotonic",
                side_effect=lambda: self.now,
            ),
        ):
            clock.start()
            self.addCleanup(clock.stop)
        self.client = EufySdkApiClient("synthetic-bridge", 3000, Mock())
        self.transport = Mock(closed=False, close=AsyncMock())
        self.client._ws = self.transport
        self.transport.send_json = AsyncMock(side_effect=self.send)
        self.client._schedule_reconnect = Mock()
        self.frames = []
        self.slow_method = None
        self.auth_state = "ok"
        self.devices = [{"sn": "synthetic-device", "state": {"enabled": False}}]
        self.solix = [{"sn": "synthetic-solix", "values": {"batterySoc": 40}}]
        self.hass = HomeAssistant(ROOT)
        self.coordinator = EufySdkDataUpdateCoordinator(
            hass=self.hass,
            logger=LOGGER,
            name="synthetic",
            update_interval=None,
            config_entry=None,
        )
        self.entry = SimpleNamespace(
            runtime_data=SimpleNamespace(
                client=self.client, coordinator=self.coordinator, properties={}
            )
        )
        self.coordinator.config_entry = self.entry
        self.cancel_retry = Mock()
        self.retry = patch(
            "custom_components.eufy_sdk.coordinator.async_call_later",
            return_value=self.cancel_retry,
        ).start()
        self.addCleanup(patch.stopall)

    def send(self, frame):
        self.frames.append(frame)
        if frame["cmd"] in {self.slow_method, "device.set"}:
            return
        reply = {"ok": True}
        if frame["cmd"] == "auth.status":
            reply["auth"] = {"state": self.auth_state}
        elif frame["cmd"] == "devices.list":
            reply["devices"] = deepcopy(self.devices)
        elif frame["cmd"] == "solix.devices":
            reply["devices"] = deepcopy(self.solix)
        self.client._pending[frame["id"]].set_result(reply)

    async def advance(self, seconds=0):
        self.now += seconds
        for _ in range(6):
            await asyncio.sleep(0)

    async def next_refresh(self):
        """Exercise the next real read after the shared recovery start limit."""
        coordinator = self.coordinator
        if (
            coordinator._recovery_active
            and coordinator._last_refresh_started is not None
        ):
            await self.advance(
                max(0, 30.001 - (self.now - coordinator._last_refresh_started))
            )

    async def complete(self):
        await self.next_refresh()
        await self.coordinator.async_refresh()
        self.assertTrue(self.coordinator.last_update_success)
        self.assertTrue(self.coordinator.snapshot_fresh)

    async def slow_refresh(self, method="devices.list"):
        self.slow_method = method
        await self.next_refresh()
        task = asyncio.create_task(self.coordinator.async_refresh())
        await self.advance()
        await self.advance(16)
        await task

    async def test_slow_list_preserves_pending_guard_rpc_and_default_deadlines(self):
        await self.complete()
        guard = asyncio.create_task(
            self.client.set_property("synthetic-device", "armingMode", 1)
        )
        await self.advance()
        guard_frame = self.frames[-1]
        generation = self.client.connection_generation
        await self.slow_refresh()
        self.assertTrue(self.client.connected)
        self.assertEqual(self.client.connection_generation, generation)
        self.assertFalse(guard.done())
        self.assertIn(guard_frame["id"], self.client._pending)
        await self.advance(10)
        self.client._pending[guard_frame["id"]].set_result({"ok": True})
        await guard
        self.assertEqual(self.client._pending, {})
        self.assertEqual(self.coordinator.refresh_failures[-1]["elapsed_ms"], 16000)
        self.assertEqual(self.client.slow_successful_rpcs[-1]["method"], "device.set")
        self.transport.close.assert_not_awaited()

    async def test_first_slow_refresh_merges_journals_and_preserves_availability(self):
        await self.complete()
        coordinator = self.coordinator
        completed = coordinator.completed_snapshot_generation
        completed_transport = coordinator._completed_snapshot_transport_generation
        self.assertEqual(completed_transport, self.client.connection_generation)
        last_complete = coordinator.last_complete_snapshot_ms
        coordinator.data["synthetic-device"]["state"].update(
            alarmTriggered=False, alarmPending=False, contact=True
        )
        old_data = coordinator.data
        entity = EufySdkDeviceEntity(coordinator, "synthetic-device")
        self.slow_method = "devices.list"
        task = asyncio.create_task(coordinator.async_refresh())
        await self.advance()
        coordinator.record_state_event("synthetic-device", {"enabled": True})
        coordinator.record_command_health(
            "synthetic-device",
            {
                "status": "command_degraded",
                "cooldownRemainingMs": 120000,
                "revision": 2,
            },
        )
        camera = camera_projection(revision=2)
        coordinator.apply_camera_health_event(
            {"event": "cameraHealth", "sn": "synthetic-device", **camera}
        )
        with self.assertLogs(LOGGER, level="WARNING") as logs:
            await self.advance(16)
            await task
        self.assertEqual(len(logs.output), 1)
        self.assertRegex(
            logs.output[0],
            r"bridge_update_failed: phase=list_devices "
            r"method=devices.list reason=reply_timeout elapsed_ms=16000 "
            r"reset=false tolerated=true$",
        )
        self.assertTrue(entity.available)
        self.assertFalse(coordinator.snapshot_fresh)
        self.assertEqual(coordinator.last_complete_snapshot_ms, last_complete)
        self.assertEqual(coordinator.completed_snapshot_generation, completed)
        self.assertEqual(
            coordinator._completed_snapshot_transport_generation, completed_transport
        )
        self.assertEqual(coordinator.snapshot_generation, completed + 1)
        self.assertIsNot(coordinator.data, old_data)
        self.assertIsNot(
            coordinator.data["synthetic-device"]["state"],
            old_data["synthetic-device"]["state"],
        )
        state = coordinator.data["synthetic-device"]["state"]
        self.assertTrue(state["enabled"])
        self.assertTrue(state["contact"])
        self.assertFalse(state["alarmTriggered"])
        self.assertEqual(
            coordinator.data["synthetic-device"]["commandHealth"]["revision"], 2
        )
        self.assertEqual(coordinator.data["synthetic-device"]["cameraHealth"], camera)
        self.retry.assert_called_once_with(self.hass, 45, coordinator._fast_retry_fire)
        self.cancel_retry.assert_not_called()
        self.assertIsNone(coordinator._pending_state_events)

    async def test_second_slow_refresh_resets_and_marks_entities_unavailable(self):
        await self.complete()
        entity = EufySdkDeviceEntity(self.coordinator, "synthetic-device")
        await self.slow_refresh()
        self.assertTrue(entity.available)
        await self.slow_refresh()
        self.assertFalse(entity.available)
        self.assertFalse(self.client.connected)
        self.transport.close.assert_awaited_once()
        self.assertEqual(self.coordinator.completed_snapshot_generation, 1)
        self.assertEqual(self.coordinator.refresh_failures[-1]["consecutive_count"], 2)
        self.assertTrue(self.coordinator.refresh_failures[-1]["reset"])
        self.retry.assert_called_once()
        self.assertRegex(
            str(self.coordinator.last_exception), r"reason=reply_timeout .*reset=true$"
        )

    async def test_auth_and_list_timeout_streak_spans_both_phases(self):
        for first, second in (
            ("auth.status", "devices.list"),
            ("devices.list", "auth.status"),
        ):
            with self.subTest(first=first):
                self.client._ws = self.transport
                self.slow_method = None
                await self.complete()
                await self.slow_refresh(first)
                self.assertTrue(self.coordinator.last_update_success)
                await self.slow_refresh(second)
                self.assertFalse(self.coordinator.last_update_success)
                self.assertEqual(
                    self.coordinator.refresh_failures[-1]["consecutive_count"], 2
                )

    async def test_no_previous_complete_snapshot_raises_without_reset(self):
        await self.slow_refresh()
        self.assertFalse(self.coordinator.last_update_success)
        self.assertIsInstance(self.coordinator.last_exception, UpdateFailed)
        self.assertTrue(self.client.connected)
        self.transport.close.assert_not_awaited()
        self.assertEqual(self.coordinator.completed_snapshot_generation, 0)
        self.retry.assert_called_once()

    async def test_prior_empty_snapshot_is_eligible_and_duplicate_counts_survive(self):
        self.devices = []
        await self.complete()
        await self.slow_refresh()
        self.assertTrue(self.coordinator.last_update_success)
        self.assertEqual(self.coordinator.data, {})
        self.slow_method = None
        self.devices = [{"sn": "synthetic-device"}, {"sn": "synthetic-device"}]
        await self.complete()
        await self.slow_refresh()
        self.assertEqual(self.coordinator.device_serial_counts, {"synthetic-device": 2})

    async def test_complete_recovery_clears_retry_and_replenishes_tolerance(self):
        await self.complete()
        await self.slow_refresh()
        self.slow_method = None
        await self.complete()
        self.cancel_retry.assert_called_once()
        self.assertEqual(self.coordinator._consecutive_reply_timeouts, 0)
        self.assertEqual(self.coordinator.completed_snapshot_generation, 3)
        await self.slow_refresh()
        self.assertTrue(self.coordinator.last_update_success)
        self.assertEqual(self.retry.call_count, 2)

    async def test_transport_send_failures_reset_with_sanitized_message(self):
        for error in (OSError(PRIVATE), TimeoutError(PRIVATE)):
            with self.subTest(error=type(error).__name__):
                self.client._ws = self.transport
                self.transport.send_json.side_effect = error
                await self.next_refresh()
                await self.coordinator.async_refresh()
                self.assertFalse(self.coordinator.last_update_success)
                self.assertRegex(
                    str(self.coordinator.last_exception),
                    r"^bridge_update_failed: "
                    r"phase=auth_status method=auth.status reason=send_failed "
                    r"elapsed_ms=\d+ reset=true$",
                )
                self.assertNotIn(PRIVATE, str(self.coordinator.last_exception))
                self.assertEqual(self.client._pending, {})

    async def test_auth_challenges_and_auth_errors_do_not_reset_or_tolerate(self):
        await self.complete()
        for state in ("require_2fa", "require_captcha", "pending"):
            with self.subTest(state=state):
                self.auth_state = state
                if state == "pending":
                    with self.assertRaisesRegex(
                        UpdateFailed, "^bridge_auth_not_ready$"
                    ):
                        await self.next_refresh()
                        await self.coordinator._async_update_data()
                else:
                    with self.assertRaisesRegex(
                        ConfigEntryAuthFailed, "^bridge_auth_challenge_required$"
                    ):
                        await self.next_refresh()
                        await self.coordinator._async_update_data()
                self.assertFalse(self.coordinator.snapshot_fresh)
        self.assertEqual(len(self.coordinator.refresh_failures), 3)
        self.assertEqual(self.coordinator.refresh_failures[-1]["phase"], "auth_status")
        self.client.auth_status = AsyncMock(
            side_effect=EufySdkApiClientAuthenticationError(PRIVATE)
        )
        with self.assertRaisesRegex(
            ConfigEntryAuthFailed, "^bridge_authentication_failed$"
        ):
            await self.next_refresh()
            await self.coordinator._async_update_data()
        self.transport.close.assert_not_awaited()

    async def test_unexpected_auth_response_failure_preserves_traceback(self):
        await self.complete()
        error = KeyError(PRIVATE)
        self.client.auth_status = AsyncMock(side_effect=error)
        with self.assertLogs(LOGGER, level="ERROR") as logs:
            await self.next_refresh()
            await self.coordinator.async_refresh()
        self.assertFalse(self.coordinator.last_update_success)
        self.assertIs(self.coordinator.last_exception, error)
        self.assertEqual(self.coordinator.refresh_failures[-1]["category"], "other")
        self.assertIn("Unexpected error fetching", logs.output[0])
        self.assertIn(
            PRIVATE,
            "".join(traceback.format_exception(self.coordinator.last_exception)),
        )
        self.assertNotIn(PRIVATE, json.dumps(list(self.coordinator.refresh_failures)))
        self.transport.close.assert_not_awaited()

    async def test_optional_solix_failure_is_recorded_without_failing_eufy(self):
        await self.complete()
        generation = self.coordinator.solix_snapshot_generation
        self.slow_method = "solix.devices"
        await self.next_refresh()
        task = asyncio.create_task(self.coordinator.async_refresh())
        await self.advance()
        await self.advance(16)
        await task
        self.assertTrue(self.coordinator.last_update_success)
        self.assertTrue(self.coordinator.snapshot_fresh)
        self.assertEqual(self.coordinator.solix_snapshot_generation, generation)
        self.assertEqual(self.coordinator.refresh_failures[-1]["phase"], "solix")
        self.assertEqual(
            self.coordinator.refresh_failures[-1]["method"], "solix.devices"
        )
        self.transport.close.assert_not_awaited()

    async def test_cached_notifications_do_not_regress_solix_telemetry(self):
        await self.complete()
        entity = EufySolixEntity(
            self.coordinator, "synthetic-solix", "battery", ("batterySoc",)
        )
        entity.async_write_ha_state = Mock()
        self.coordinator.async_add_listener(entity._handle_solix_snapshot)
        entity._handle_solix_event(
            SimpleNamespace(
                data={
                    "event": "solixReading",
                    "deviceSn": "synthetic-solix",
                    "values": {"batterySoc": 55},
                }
            )
        )
        await self.slow_refresh()
        self.assertEqual(entity.solix_value("batterySoc"), 55)
        self.slow_method = None
        self.solix[0]["values"]["batterySoc"] = 61
        await self.complete()
        self.assertEqual(entity.solix_value("batterySoc"), 61)

    async def test_replaced_transport_is_not_tolerated_or_reset_by_old_timeout(self):
        await self.complete()
        self.slow_method = "devices.list"
        await self.next_refresh()
        task = asyncio.create_task(self.coordinator.async_refresh())
        await self.advance()
        replacement = Mock(closed=False)
        self.client._ws = replacement
        self.client._reset_command_health()
        await self.advance(16)
        await task
        self.assertFalse(self.coordinator.last_update_success)
        self.assertIs(self.client._ws, replacement)
        self.transport.close.assert_not_awaited()
        self.assertFalse(self.coordinator.refresh_failures[-1]["reset"])

    async def test_reset_waiting_for_lock_cannot_close_replacement_transport(self):
        await self.complete()
        await self.client._connect_lock.acquire()
        task = asyncio.create_task(
            self.client.reset_connection(
                expected_generation=self.client.connection_generation
            )
        )
        await self.advance()
        replacement = Mock(closed=False)
        self.client._ws = replacement
        self.client._reset_command_health()
        self.client._connect_lock.release()
        self.assertFalse(await task)
        self.assertIs(self.client._ws, replacement)

    async def test_failure_ring_retention_bounds_and_diagnostics_privacy(self):
        await self.complete()
        for _ in range(25):
            self.client.auth_status = AsyncMock(
                side_effect=EufySdkApiClientError(PRIVATE)
            )
            await self.next_refresh()
            await self.coordinator.async_refresh()
        failures = list(self.coordinator.refresh_failures)
        self.assertEqual(len(failures), 20)
        self.client.auth_status = AsyncMock(return_value={"state": "ok"})
        for _ in range(25):
            await self.complete()
            task = asyncio.create_task(
                self.client.rpc("device.set", sn=PRIVATE, value=PRIVATE)
            )
            await self.advance()
            await self.advance(6)
            self.client._pending[self.frames[-1]["id"]].set_result(
                {"ok": True, "payload": PRIVATE}
            )
            await task
        self.assertEqual(list(self.coordinator.refresh_failures), failures)
        self.assertEqual(len(self.client.slow_successful_rpcs), 20)
        self.assertEqual(
            set(self.client.slow_successful_rpcs[0]),
            {"method", "elapsed_ms", "timestamp_ms"},
        )
        diagnostics = await async_get_config_entry_diagnostics(self.hass, self.entry)
        self.assertEqual(diagnostics["refresh_failures"], failures)
        self.assertEqual(
            diagnostics["slow_successful_rpcs"], list(self.client.slow_successful_rpcs)
        )
        self.assertNotIn(PRIVATE, json.dumps(diagnostics))
        self.assertEqual(
            set(failures[0]),
            {
                "timestamp_ms",
                "phase",
                "method",
                "category",
                "elapsed_ms",
                "reset",
                "consecutive_count",
            },
        )
        self.assertEqual(
            diagnostics["refresh"]["last_complete_snapshot_ms"],
            self.coordinator.last_complete_snapshot_ms,
        )

    async def test_request_local_failure_records_do_not_mix_concurrent_methods(self):
        first = asyncio.create_task(self.client.rpc("device.set", sn=PRIVATE))
        await self.advance()
        second = asyncio.create_task(
            self.client.rpc("device.set", sn=PRIVATE, timeout=60)
        )
        await self.advance()
        await self.advance(16)
        with self.assertRaises(EufySdkApiClientReplyTimeoutError) as caught:
            await first
        self.assertIsInstance(caught.exception, EufySdkApiClientCommunicationError)
        self.assertEqual(str(caught.exception), "bridge_request_timeout")
        self.assertEqual(caught.exception.failure.method, "device.set")
        self.assertEqual(caught.exception.failure.category, "reply_timeout")
        self.assertEqual(caught.exception.failure.elapsed_ms, 16000)
        self.assertFalse(second.done())
        second.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await second
        self.assertEqual(self.client._pending, {})

    async def test_connect_and_not_connected_records_never_include_endpoint(self):
        self.client._ws = None
        with self.assertRaises(EufySdkApiClientCommunicationError) as caught:
            await self.client.rpc("auth.status", payload=PRIVATE)
        self.assertEqual(caught.exception.failure.category, "not_connected")
        self.client._session.ws_connect = AsyncMock(side_effect=OSError(PRIVATE))
        await self.next_refresh()
        await self.coordinator.async_refresh()
        record = self.coordinator.refresh_failures[-1]
        self.assertEqual(record["phase"], "connect")
        self.assertEqual(record["method"], "connect")
        self.assertEqual(record["category"], "not_connected")
        self.assertNotIn(
            PRIVATE,
            "".join(traceback.format_exception(self.coordinator.last_exception)),
        )

    async def test_fast_successes_are_not_recorded_and_bridge_errors_are_sanitized(
        self,
    ):
        await self.client.rpc("auth.status")
        self.assertEqual(list(self.client.slow_successful_rpcs), [])

        def reject(frame):
            self.client._pending[frame["id"]].set_result(
                {"ok": False, "error": PRIVATE}
            )

        self.transport.send_json.side_effect = reject
        with self.assertRaises(EufySdkApiClientError) as caught:
            await self.client.rpc("devices.list", args=PRIVATE)
        self.assertEqual(caught.exception.failure.category, "bridge_error")
        self.assertNotIn(PRIVATE, str(caught.exception))

    async def test_alarm_stop_and_auto_clear_survive_cached_refresh(self):
        await self.complete()
        entry = Mock()
        timers = []

        def later(_hass, delay, action):
            timers.append((delay, action))
            return Mock()

        with patch.object(integration, "async_call_later", side_effect=later):
            alarm = integration._alarm_lifecycle(self.hass, entry, self.coordinator)
            alarm(
                {
                    "event": "alarm",
                    "deviceSn": "synthetic-device",
                    "type": 4,
                    "phase": "triggered",
                }
            )
            self.slow_method = "devices.list"
            await self.next_refresh()
            task = asyncio.create_task(self.coordinator.async_refresh())
            await self.advance()
            timers[0][1](None)
            await self.advance(16)
            await task
            self.assertFalse(
                self.coordinator.data["synthetic-device"]["state"]["alarmTriggered"]
            )
            self.assertEqual(len(timers), 1)
            self.slow_method = None
            await self.complete()
            alarm(
                {
                    "event": "alarm",
                    "deviceSn": "synthetic-device",
                    "type": 4,
                    "phase": "triggered",
                }
            )
            alarm(
                {
                    "event": "alarm",
                    "deviceSn": "synthetic-device",
                    "type": 16,
                    "phase": "triggered",
                }
            )
            await self.slow_refresh()
            self.assertFalse(
                self.coordinator.data["synthetic-device"]["state"]["alarmTriggered"]
            )
            self.assertEqual(len(timers), 2)

    async def test_cached_storage_uses_original_expiry(self):
        device = storage_device(observed=NOW - FRESHNESS_MS + 5000)
        device["sn"] = "synthetic-device"
        self.devices = [device]
        await self.complete()
        self.assertEqual(
            storage_reading(self.coordinator.data["synthetic-device"], NOW)[0],
            "current",
        )
        await self.slow_refresh()
        cached = self.coordinator.data["synthetic-device"]
        self.assertEqual(
            cached["storageTelemetry"]["observedAtMs"],
            device["storageTelemetry"]["observedAtMs"],
        )
        self.assertEqual(storage_reading(cached, NOW + 16000)[0], "unavailable")

    async def test_cancellation_cleans_journal_and_invalidates_timeout_tolerance(self):
        await self.complete()
        self.slow_method = "devices.list"
        await self.next_refresh()
        task = asyncio.create_task(self.coordinator._async_update_data())
        await self.advance()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.client._pending, {})
        self.assertIsNone(self.coordinator._pending_state_events)
        self.assertEqual(self.coordinator.completed_snapshot_generation, 1)
        self.assertFalse(self.coordinator.snapshot_fresh)
        self.assertEqual(self.coordinator._consecutive_reply_timeouts, 0)
        await self.slow_refresh()
        self.assertFalse(self.coordinator.last_update_success)
        self.assertIsInstance(self.coordinator.last_exception, UpdateFailed)
        self.transport.close.assert_not_awaited()

    async def test_closed_and_reset_requests_have_distinct_categories(self):
        for action, category in (
            (self.client.close, "connection_closed"),
            (self.client.reset_connection, "connection_reset"),
        ):
            with self.subTest(category=category):
                self.client._ws = self.transport
                task = asyncio.create_task(self.client.rpc("device.set", sn=PRIVATE))
                await self.advance()
                await action()
                with self.assertRaises(EufySdkApiClientCommunicationError) as caught:
                    await task
                self.assertEqual(caught.exception.failure.category, category)
                self.assertEqual(caught.exception.failure.method, "device.set")
                self.assertNotIn(PRIVATE, str(caught.exception.failure))

    async def test_receiver_loss_is_not_tolerated(self):
        await self.complete()
        self.slow_method = "devices.list"
        await self.next_refresh()
        task = asyncio.create_task(self.coordinator.async_refresh())
        await self.advance()

        async def lost_socket():
            for _ in ():
                yield

        self.transport.__aiter__ = lambda _: lost_socket()
        await self.client._receive_loop()
        await task
        self.assertFalse(self.coordinator.last_update_success)
        self.assertEqual(
            self.coordinator.refresh_failures[-1]["category"], "connection_closed"
        )
        self.assertEqual(self.coordinator._consecutive_reply_timeouts, 0)

    async def test_same_socket_ready_invalidates_tolerance(self):
        await self.complete()
        self.slow_method = "devices.list"
        await self.next_refresh()
        task = asyncio.create_task(self.coordinator.async_refresh())
        await self.advance()
        self.client._dispatch_event({"event": "ready"})
        await self.advance(16)
        await task
        self.assertFalse(self.coordinator.last_update_success)
        self.assertIs(self.client._ws, self.transport)
        self.transport.close.assert_not_awaited()

    async def test_slow_success_threshold_is_strictly_over_five_seconds(self):
        # Half a millisecond past the boundary: on a large clock base, 5.001 s can truncate to 5000 ms.
        for elapsed in (5, 5.0015):
            with self.subTest(elapsed=elapsed):
                task = asyncio.create_task(self.client.rpc("device.set", sn=PRIVATE))
                await self.advance()
                await self.advance(elapsed)
                self.client._pending[self.frames[-1]["id"]].set_result({"ok": True})
                await task
        self.assertEqual(len(self.client.slow_successful_rpcs), 1)

    async def test_unexpected_rpc_error_propagates_unchanged(self):
        error = ValueError(PRIVATE)
        self.transport.send_json.side_effect = error
        with self.assertRaises(ValueError) as caught:
            await self.client.rpc("devices.list", payload=PRIVATE)
        self.assertIs(caught.exception, error)
        self.assertEqual(str(caught.exception), PRIVATE)
        self.assertEqual(self.client._pending, {})

    async def test_timeout_streak_does_not_transfer_to_replacement_transport(self):
        await self.complete()
        await self.slow_refresh()
        replacement = Mock(closed=False, close=AsyncMock())
        replacement.send_json = AsyncMock(side_effect=self.send)
        self.client._ws = replacement
        self.client._reset_command_health()
        await self.slow_refresh()
        self.assertFalse(self.coordinator.last_update_success)
        self.assertIsInstance(self.coordinator.last_exception, UpdateFailed)
        self.assertEqual(self.coordinator.refresh_failures[-1]["consecutive_count"], 1)
        self.assertFalse(self.coordinator.refresh_failures[-1]["reset"])
        self.assertIs(self.client._ws, replacement)
        replacement.close.assert_not_awaited()

    async def test_failed_connect_cannot_reset_queued_successful_reconnect(self):
        self.client._ws = None
        connecting = asyncio.Event()
        release = asyncio.Event()
        replacement = Mock(closed=False, close=AsyncMock())
        calls = 0

        async def connect(*_args: object, **_kwargs: object):
            nonlocal calls
            calls += 1
            if calls == 1:
                connecting.set()
                await release.wait()
                raise OSError(PRIVATE)
            return replacement

        async def receive():
            await asyncio.Event().wait()

        self.client._session.ws_connect = AsyncMock(side_effect=connect)
        self.client._receive_loop = receive
        await self.next_refresh()
        task = asyncio.create_task(self.coordinator.async_refresh())
        await connecting.wait()
        reconnect = asyncio.create_task(self.client.connect())
        await self.advance()
        release.set()
        await reconnect
        await task
        self.assertIs(self.client._ws, replacement)
        replacement.close.assert_not_awaited()
        self.assertFalse(self.coordinator.refresh_failures[-1]["reset"])
        self.assertFalse(self.coordinator.last_update_success)
        await self.client.close()

    async def reconnect(self):
        replacement = Mock(closed=False, close=AsyncMock())
        replacement.send_json = AsyncMock(side_effect=self.send)
        self.client._session.ws_connect = AsyncMock(return_value=replacement)

        async def receive():
            await asyncio.Event().wait()

        self.client._receive_loop = receive
        await self.client._reconnect()
        self.addAsyncCleanup(self.client.close)
        return replacement

    async def test_failed_refresh_then_reconnect_timeout_cannot_restore_availability(
        self,
    ):
        await self.complete()
        entity = EufySdkDeviceEntity(self.coordinator, "synthetic-device")
        completed_transport = self.coordinator._completed_snapshot_transport_generation
        self.transport.send_json.side_effect = OSError(PRIVATE)
        await self.next_refresh()
        await self.coordinator.async_refresh()
        self.assertFalse(entity.available)
        self.transport.close.assert_awaited_once()
        replacement = await self.reconnect()
        self.assertNotEqual(self.client.connection_generation, completed_transport)
        await self.slow_refresh()
        self.assertFalse(entity.available)
        self.assertIsInstance(self.coordinator.last_exception, UpdateFailed)
        self.assertFalse(self.coordinator.refresh_failures[-1]["reset"])
        self.assertEqual(self.coordinator.refresh_failures[-1]["consecutive_count"], 1)
        self.assertEqual(
            self.coordinator._completed_snapshot_transport_generation,
            completed_transport,
        )
        replacement.close.assert_not_awaited()
        self.retry.assert_called_once()

    async def test_supervisor_reconnect_between_polls_invalidates_cached_snapshot(self):
        await self.complete()
        entity = EufySdkDeviceEntity(self.coordinator, "synthetic-device")
        completed = self.coordinator.completed_snapshot_generation
        completed_transport = self.coordinator._completed_snapshot_transport_generation

        async def lost_socket():
            for _ in ():
                yield

        self.transport.__aiter__ = lambda _: lost_socket()
        self.client.add_disconnect_listener(self.coordinator.reset_camera_health)
        await self.client._receive_loop()
        replacement = await self.reconnect()
        self.assertEqual(self.coordinator.snapshot_generation, completed)
        self.assertNotEqual(self.client.connection_generation, completed_transport)
        await self.slow_refresh()
        self.assertFalse(entity.available)
        self.assertIsInstance(self.coordinator.last_exception, UpdateFailed)
        self.assertFalse(self.coordinator.refresh_failures[-1]["reset"])
        self.assertEqual(self.coordinator.refresh_failures[-1]["consecutive_count"], 1)
        replacement.close.assert_not_awaited()
        self.retry.assert_called_once()

    async def test_failed_refresh_on_same_transport_invalidates_cached_snapshot(self):
        await self.complete()
        generation = self.client.connection_generation
        self.auth_state = "pending"
        await self.next_refresh()
        await self.coordinator.async_refresh()
        self.assertFalse(self.coordinator.last_update_success)
        self.auth_state = "ok"
        await self.slow_refresh()
        self.assertFalse(self.coordinator.last_update_success)
        self.assertIsInstance(self.coordinator.last_exception, UpdateFailed)
        self.assertEqual(self.client.connection_generation, generation)
        self.assertFalse(self.coordinator.refresh_failures[-1]["reset"])
        self.transport.close.assert_not_awaited()

    async def test_unexpected_list_rpc_error_propagates_with_sanitized_failure_ring(
        self,
    ):
        await self.complete()
        await self.slow_refresh()
        error = RuntimeError(PRIVATE)

        def fail_list(frame):
            if frame["cmd"] == "devices.list":
                raise error
            self.send(frame)

        self.transport.send_json.side_effect = fail_list
        with self.assertRaises(RuntimeError) as caught:
            await self.next_refresh()
            await self.coordinator._async_update_data()
        self.assertIs(caught.exception, error)
        self.assertEqual(self.coordinator._consecutive_reply_timeouts, 0)
        record = self.coordinator.refresh_failures[-1]
        self.assertEqual(record["category"], "other")
        self.assertEqual(record["phase"], "list_devices")
        self.assertEqual(record["method"], "devices.list")
        self.assertEqual(record["consecutive_count"], 0)
        self.assertFalse(record["reset"])
        self.assertNotIn(PRIVATE, json.dumps(list(self.coordinator.refresh_failures)))
        self.assertIsNone(self.coordinator._pending_state_events)
        self.assertEqual(self.client._pending, {})
        self.transport.close.assert_not_awaited()

    async def test_expected_bridge_error_keeps_fast_retry_and_connection(self):
        await self.complete()
        entity = EufySdkDeviceEntity(self.coordinator, "synthetic-device")

        def reject_list(frame):
            if frame["cmd"] != "devices.list":
                self.send(frame)
                return
            self.frames.append(frame)
            self.client._pending[frame["id"]].set_result(
                {"ok": False, "error": PRIVATE}
            )

        self.transport.send_json.side_effect = reject_list
        await self.next_refresh()
        await self.coordinator.async_refresh()
        self.assertFalse(self.coordinator.last_update_success)
        self.assertFalse(entity.available)
        self.assertIsInstance(self.coordinator.last_exception, UpdateFailed)
        self.assertRegex(
            str(self.coordinator.last_exception),
            r"^bridge_update_failed: phase=list_devices method=devices\.list "
            r"reason=bridge_error elapsed_ms=\d+ reset=false$",
        )
        self.assertNotIn(PRIVATE, str(self.coordinator.last_exception))
        self.retry.assert_called_once()
        self.assertTrue(self.client.connected)
        self.transport.close.assert_not_awaited()
        self.assertEqual(self.coordinator.completed_snapshot_generation, 1)
        self.assertEqual(
            self.coordinator.refresh_failures[-1]["category"], "bridge_error"
        )

    async def test_completed_snapshot_records_read_transport_before_solix_reconnect(
        self,
    ):
        generation = self.client.connection_generation

        async def solix_reconnect():
            self.client._reset_command_health()
            return self.solix

        self.client.list_solix_devices = AsyncMock(side_effect=solix_reconnect)
        await self.complete()
        self.assertEqual(
            self.coordinator._completed_snapshot_transport_generation, generation
        )
        self.assertNotEqual(self.client.connection_generation, generation)
        await self.slow_refresh()
        self.assertFalse(self.coordinator.last_update_success)
        self.assertIsInstance(self.coordinator.last_exception, UpdateFailed)
        self.transport.close.assert_not_awaited()
