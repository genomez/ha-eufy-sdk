"""Verify bounded phone retries independently of light command timing."""

# ruff: noqa: ANN001, ANN201, D102, FBT003, INP001, PT009, SLF001

import unittest

import test_floodlight_safety as fixtures

from custom_components.eufy_sdk.light_correction import LightCorrection


class NotifyRetryCapTests(unittest.IsolatedAsyncioTestCase):
    """Exercise real notice delivery with fake time and in-memory storage."""

    _entity = fixtures.FloodlightSafetyTests._entity
    _report = fixtures.FloodlightSafetyTests._report
    asyncSetUp = fixtures.FloodlightSafetyTests.asyncSetUp
    asyncTearDown = fixtures.FloodlightSafetyTests.asyncTearDown
    make = fixtures.FloodlightSafetyTests.make
    drain = fixtures.FloodlightSafetyTests.drain

    async def failing(self):
        entity, controller, _ = await self.make()
        entity.coordinator.config_entry.options["light_safety_notify_service"] = (
            "notify.fixture_phone"
        )
        entity.hass.services.async_call.side_effect = RuntimeError("synthetic")
        controller._notice("off_unconfirmed", True)
        await self.drain()
        return entity, controller

    async def retry_at(self, controller, when):
        self.now = when
        controller._deliver_pending()
        await self.drain()

    async def exhaust(self, controller):
        for when in (60, 180, 480, 1080):
            await self.retry_at(controller, when)

    async def test_exact_schedule_cap_bell_and_single_give_up(self):
        entity, controller = await self.failing()
        original = self.notification.call_args
        events = entity.hass.bus.async_fire.call_count
        for count, due in enumerate((60, 180, 480, 1080), start=1):
            self.assertEqual(controller.notice_retry_due, due)
            await self.retry_at(controller, due - 0.01)
            self.assertEqual(entity.hass.services.async_call.await_count, count)
            await self.retry_at(controller, due)
            self.assertEqual(entity.hass.services.async_call.await_count, count + 1)
        self.assertIsNone(controller.notice_pending)
        for when in (2000, 10000):
            await self.retry_at(controller, when)
        self.assertEqual(entity.hass.services.async_call.await_count, 5)
        failed = [row for row in controller.journal if row["kind"] == "notify_failed"]
        gave_up = [row for row in controller.journal if row["kind"] == "notify_gave_up"]
        self.assertEqual(len(failed), 5)
        self.assertEqual(len(gave_up), 1)
        self.assertEqual(gave_up[0]["attempts"], 5)
        self.assertEqual(entity.hass.bus.async_fire.call_count, events)
        self.assertEqual(self.notification.call_args.kwargs, original.kwargs)
        self.assertEqual(
            self.notification.call_args.args[1],
            original.args[1] + " Phone delivery failed; check the notification "
            "service in the integration options.",
        )
        self.dismiss.assert_not_called()

    async def test_success_midway_clears_count_and_stops_retrying(self):
        entity, controller = await self.failing()
        notice = controller.notice_pending
        await self.retry_at(controller, 60)
        entity.hass.services.async_call.side_effect = None
        await self.retry_at(controller, 180)
        self.assertNotIn("phone_attempts", notice)
        self.assertIsNone(controller.notice_pending)
        await self.retry_at(controller, 5000)
        self.assertEqual(entity.hass.services.async_call.await_count, 3)

    async def test_replacement_starts_fresh_without_inheriting_delay(self):
        entity, controller = await self.failing()
        await self.retry_at(controller, 60)
        controller._notice("now_off", False)
        await self.drain()
        self.assertEqual(controller.notice_pending["kind"], "now_off")
        self.assertEqual(controller.notice_pending["phone_attempts"], 1)
        self.assertEqual(controller.notice_retry_due, 120)
        self.assertEqual(entity.hass.services.async_call.await_count, 3)

    async def test_queue_waits_for_give_up_then_delivers_in_order(self):
        entity, controller = await self.failing()
        controller._notice("on_unconfirmed", True)
        await self.drain()
        self.assertEqual(entity.hass.services.async_call.await_count, 1)
        await self.exhaust(controller)
        self.assertEqual(entity.hass.services.async_call.await_count, 6)
        self.assertEqual(controller.notice_pending["kind"], "on_unconfirmed")
        self.assertEqual(controller.notice_pending["phone_attempts"], 1)
        self.assertEqual(controller.notice_retry_due, 1140)

    async def test_retry_count_round_trip_and_review19_compatibility(self):
        entity, controller = await self.failing()
        await self.retry_at(controller, 60)
        controller.stop("closed")
        restored = LightCorrection(entity)
        self.controllers.append(restored)
        entity._light_correction = restored
        await restored.async_added()
        await self.drain()
        self.assertEqual(restored.notice_pending["phone_attempts"], 3)
        for when in (360, 960):
            await self.retry_at(restored, when)
        self.assertIsNone(restored.notice_pending)
        self.assertEqual(entity.hass.services.async_call.await_count, 5)
        stored = next(iter(fixtures.MemoryStore.rows.values()))
        self.assertIsNone(stored["notice_pending"])
        # A review.19 notice has no phone-attempt counter.
        stored["notice_pending"] = {
            "kind": "on_unconfirmed",
            "problem": True,
            "attempts": 2,
            "emitted": True,
        }
        restored.stop("closed")
        legacy = LightCorrection(entity)
        self.controllers.append(legacy)
        await legacy.async_added()
        await self.drain()
        self.assertEqual(legacy.notice_pending["phone_attempts"], 1)
