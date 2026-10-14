# ruff: noqa: ANN201, D100, D101, D102, INP001, PT009, SLF001

import asyncio
import unittest
from unittest.mock import AsyncMock, Mock

from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.eufy_sdk.coordinator import EufySdkDataUpdateCoordinator


class SnapshotGenerationTests(unittest.IsolatedAsyncioTestCase):
    def _coordinator(self):
        coordinator = EufySdkDataUpdateCoordinator.__new__(EufySdkDataUpdateCoordinator)
        coordinator.data = {"synthetic-device": {"state": {"enabled": False}}}
        coordinator._read_snapshot = AsyncMock(return_value=coordinator.data)
        coordinator._clear_fast_retry = Mock()
        coordinator._schedule_fast_retry = Mock()
        return coordinator

    async def test_inflight_read_is_not_a_completed_snapshot(self):
        coordinator = self._coordinator()
        started = asyncio.Event()
        finish = asyncio.Event()

        async def read():
            started.set()
            await finish.wait()
            return {"synthetic-device": {"state": {"enabled": True}}}

        coordinator._read_snapshot.side_effect = read
        task = asyncio.create_task(coordinator._async_update_data())
        await started.wait()
        self.assertEqual(coordinator.snapshot_generation, 1)
        self.assertEqual(coordinator.completed_snapshot_generation, 0)
        finish.set()
        snapshot = await task
        self.assertEqual(coordinator.completed_snapshot_generation, 1)
        self.assertTrue(snapshot["synthetic-device"]["state"]["enabled"])

    async def test_failed_read_does_not_advance_completed_generation(self):
        coordinator = self._coordinator()
        await coordinator._async_update_data()
        coordinator._read_snapshot.side_effect = UpdateFailed("synthetic failure")
        with self.assertRaises(UpdateFailed):
            await coordinator._async_update_data()
        self.assertEqual(coordinator.snapshot_generation, 2)
        self.assertEqual(coordinator.completed_snapshot_generation, 1)
        coordinator._schedule_fast_retry.assert_called_once()
        self.assertIsNone(coordinator._pending_state_events)

    async def test_auth_failure_preserves_completion_and_clears_retry(self):
        coordinator = self._coordinator()
        coordinator._read_snapshot.side_effect = ConfigEntryAuthFailed(
            "synthetic challenge"
        )
        with self.assertRaises(ConfigEntryAuthFailed):
            await coordinator._async_update_data()
        self.assertEqual(coordinator.completed_snapshot_generation, 0)
        coordinator._clear_fast_retry.assert_called_once()
        coordinator._schedule_fast_retry.assert_not_called()
        self.assertIsNone(coordinator._pending_state_events)

    async def test_cancelled_read_is_not_reported_as_success(self):
        coordinator = self._coordinator()
        coordinator._read_snapshot.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await coordinator._async_update_data()
        self.assertEqual(coordinator.snapshot_generation, 1)
        self.assertEqual(coordinator.completed_snapshot_generation, 0)
        self.assertIsNone(coordinator._pending_state_events)

    async def test_realtime_journal_and_alarm_state_survive_snapshot(self):
        coordinator = self._coordinator()
        coordinator.data["synthetic-device"]["state"]["alarmTriggered"] = True

        async def read():
            coordinator.record_state_event("synthetic-device", {"enabled": True})
            return {"synthetic-device": {"state": {"enabled": False}}}

        coordinator._read_snapshot.side_effect = read
        snapshot = await coordinator._async_update_data()
        self.assertTrue(snapshot["synthetic-device"]["state"]["enabled"])
        self.assertTrue(snapshot["synthetic-device"]["state"]["alarmTriggered"])
        self.assertEqual(coordinator.completed_snapshot_generation, 1)

    async def test_generations_are_independent_for_each_coordinator(self):
        first = self._coordinator()
        second = self._coordinator()
        await first._async_update_data()
        await first._async_update_data()
        self.assertEqual(first.completed_snapshot_generation, 2)
        self.assertEqual(second.snapshot_generation, 0)
        self.assertEqual(second.completed_snapshot_generation, 0)
        await second._async_update_data()
        self.assertEqual(second.completed_snapshot_generation, 1)
        self.assertEqual(first.completed_snapshot_generation, 2)


if __name__ == "__main__":
    unittest.main()
