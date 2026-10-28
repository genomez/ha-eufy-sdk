"""Keep event images current without fetching on unrelated coordinator traffic."""

# ruff: noqa: ANN001, ANN201, D101, D102, INP001, PT009, SLF001

import asyncio
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from custom_components.eufy_sdk.entity import EufySdkDeviceEntity
from custom_components.eufy_sdk.image import EufySdkEventImage
from custom_components.eufy_sdk.pushmap import EVENT_IMAGE_REFRESH


class ImageRefreshTriggerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tasks = []
        self.hass = Mock()

        def create_task(coro) -> asyncio.Task:
            task = asyncio.create_task(coro)
            self.tasks.append(task)
            return task

        self.hass.async_create_task.side_effect = create_task
        self.coordinator = SimpleNamespace(
            data={"synthetic-camera": {"stream": "/stream"}, "other-camera": {}},
            last_update_success=True,
            completed_snapshot_generation=1,
        )
        self.entity = EufySdkEventImage(
            self.hass, self.coordinator, "synthetic-camera", "bridge.invalid", 3000
        )
        self.entity.hass = self.hass
        self.entity.async_write_ha_state = Mock()
        self.event_time = datetime(2026, 10, 28, 12, tzinfo=UTC)
        self.entity._fetch = AsyncMock(
            return_value=(b"retained image", self.event_time)
        )

    async def drain(self):
        await asyncio.gather(*self.tasks)
        self.tasks.clear()

    async def seed(self):
        await self.entity._refresh()
        self.entity._fetch.reset_mock()
        self.entity.async_write_ha_state.reset_mock()
        return self.entity.image_last_updated

    async def test_health_and_unrelated_updates_neither_fetch_nor_signal(self):
        timestamp = await self.seed()
        for revision in range(50):
            self.coordinator.data["synthetic-camera"]["cameraHealth"] = {
                "revision": revision
            }
            self.coordinator.data["synthetic-camera"]["commandHealth"] = {
                "revision": revision
            }
            self.coordinator.data["synthetic-camera"]["state"] = {"batteryLevel": 80}
            self.coordinator.data["other-camera"]["state"] = {"light": bool(revision)}
            self.entity._handle_coordinator_update()
        await self.drain()
        self.entity._fetch.assert_not_awaited()
        self.entity.async_write_ha_state.assert_not_called()
        self.assertEqual(self.entity.image_last_updated, timestamp)

    async def test_new_event_and_bridge_nudge_refresh_own_image(self):
        for event in ("personDetected", EVENT_IMAGE_REFRESH):
            with self.subTest(event=event):
                await self.seed()
                new_time = datetime(2026, 10, 28, 13, tzinfo=UTC)
                self.entity._fetch.return_value = (event.encode(), new_time)
                self.entity._handle_event(
                    Mock(data={"deviceSn": "synthetic-camera", "event": event})
                )
                await self.drain()
                self.entity._fetch.assert_awaited_once()
                self.entity.async_write_ha_state.assert_called_once()
                self.assertEqual(await self.entity.async_image(), event.encode())
                self.assertEqual(self.entity._event_time, new_time)

    async def test_other_device_and_telemetry_events_do_not_fetch(self):
        await self.seed()
        for data in (
            {"deviceSn": "other-camera", "event": EVENT_IMAGE_REFRESH},
            {"deviceSn": "synthetic-camera", "event": "ptzNotify"},
            {"deviceSn": "synthetic-camera", "event": "cameraHealth"},
        ):
            self.entity._handle_event(Mock(data=data))
        await self.drain()
        self.entity._fetch.assert_not_awaited()
        self.entity.async_write_ha_state.assert_not_called()

    async def test_duplicate_image_nudge_does_not_signal_a_new_image(self):
        timestamp = await self.seed()
        self.entity._handle_event(
            Mock(data={"deviceSn": "synthetic-camera", "event": EVENT_IMAGE_REFRESH})
        )
        await self.drain()
        self.entity._fetch.assert_awaited_once()
        self.entity.async_write_ha_state.assert_not_called()
        self.assertEqual(self.entity.image_last_updated, timestamp)

    async def test_same_bytes_with_new_event_metadata_signal_a_new_image(self):
        await self.seed()
        new_time = datetime(2026, 10, 28, 13, tzinfo=UTC)
        self.entity._fetch.return_value = (b"retained image", new_time)
        self.entity._handle_event(
            Mock(data={"deviceSn": "synthetic-camera", "event": EVENT_IMAGE_REFRESH})
        )
        await self.drain()
        self.entity.async_write_ha_state.assert_called_once()
        self.assertEqual(self.entity._event_time, new_time)

    async def test_completed_snapshot_checks_once_without_republishing_same_image(self):
        timestamp = await self.seed()
        self.coordinator.completed_snapshot_generation += 1
        for _ in range(20):
            self.entity._handle_coordinator_update()
        await self.drain()
        self.entity._fetch.assert_awaited_once()
        self.entity.async_write_ha_state.assert_not_called()
        self.assertEqual(self.entity.image_last_updated, timestamp)

    async def test_snapshot_catches_image_whose_event_was_missed(self):
        await self.seed()
        self.entity._fetch.return_value = (b"missed event image", self.event_time)
        self.coordinator.completed_snapshot_generation += 1
        self.entity._handle_coordinator_update()
        await self.drain()
        self.entity.async_write_ha_state.assert_called_once()
        self.assertEqual(await self.entity.async_image(), b"missed event image")

    async def test_restart_fetches_retained_image_without_an_event(self):
        with patch.object(EufySdkDeviceEntity, "async_added_to_hass", new=AsyncMock()):
            await self.entity.async_added_to_hass()
        self.entity._fetch.assert_awaited_once()
        self.entity.async_write_ha_state.assert_called_once()
        self.assertEqual(await self.entity.async_image(), b"retained image")
        self.assertEqual(self.entity._event_time, self.event_time)

    async def test_first_complete_data_retries_failed_initial_fetch(self):
        self.entity._fetch.return_value = None
        with patch.object(EufySdkDeviceEntity, "async_added_to_hass", new=AsyncMock()):
            await self.entity.async_added_to_hass()
        self.entity.async_write_ha_state.assert_not_called()
        self.entity._fetch.return_value = (b"first image", self.event_time)
        self.coordinator.completed_snapshot_generation += 1
        self.entity._handle_coordinator_update()
        await self.drain()
        self.assertEqual(await self.entity.async_image(), b"first image")
        self.entity.async_write_ha_state.assert_called_once()

    async def test_recovery_fetches_missed_image_and_health_stays_quiet(self):
        timestamp = await self.seed()
        self.coordinator.last_update_success = False
        self.entity._handle_coordinator_update()
        self.entity.async_write_ha_state.assert_called_once()
        self.assertEqual(self.entity.image_last_updated, timestamp)
        self.entity._fetch.assert_not_awaited()
        self.entity.async_write_ha_state.reset_mock()
        self.entity._fetch.return_value = (b"recovered image", self.event_time)
        self.coordinator.last_update_success = True
        self.entity._handle_coordinator_update()
        await self.drain()
        self.assertEqual(await self.entity.async_image(), b"recovered image")
        self.entity._fetch.assert_awaited_once()
        self.entity._fetch.reset_mock()
        self.entity.async_write_ha_state.reset_mock()
        self.entity._handle_coordinator_update()
        await self.drain()
        self.entity._fetch.assert_not_awaited()
        self.entity.async_write_ha_state.assert_not_called()

    async def test_device_reappearing_fetches_image_without_waiting_for_next_poll(self):
        timestamp = await self.seed()
        device = self.coordinator.data.pop("synthetic-camera")
        self.entity._handle_coordinator_update()
        self.assertFalse(self.entity.available)
        self.assertEqual(self.entity.image_last_updated, timestamp)
        self.entity._fetch.assert_not_awaited()
        self.coordinator.data["synthetic-camera"] = device
        self.entity._fetch.return_value = (b"returned image", self.event_time)
        self.entity._handle_coordinator_update()
        await self.drain()
        self.assertTrue(self.entity.available)
        self.entity._fetch.assert_awaited_once()
        self.assertEqual(await self.entity.async_image(), b"returned image")
