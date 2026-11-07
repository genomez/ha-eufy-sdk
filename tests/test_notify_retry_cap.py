"""Verify persistent phone retries independently of light command timing."""

# ruff: noqa: ANN001, ANN002, ANN003, ANN201, ANN202, D102, FBT003, INP001, PT009, SLF001

import asyncio
import unittest
from unittest.mock import patch

import test_floodlight_safety as fixtures

from custom_components.eufy_sdk.const import CONF_LIGHT_CORRECTION
from custom_components.eufy_sdk.light_correction import LightCorrection


class NotifyRetryTests(unittest.IsolatedAsyncioTestCase):
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

    async def first_five(self, controller):
        for when in (60, 180, 480, 1080):
            await self.retry_at(controller, when)

    async def test_exact_schedule_repeats_final_delay_without_giving_up(self):
        entity, controller = await self.failing()
        original = self.notification.call_args_list[0]
        events = entity.hass.bus.async_fire.call_count
        for count, due in enumerate((60, 180, 480, 1080, 1680, 2280), start=1):
            self.assertEqual(controller.notice_retry_due, due)
            await self.retry_at(controller, due - 0.01)
            self.assertEqual(entity.hass.services.async_call.await_count, count)
            await self.retry_at(controller, due)
            self.assertEqual(entity.hass.services.async_call.await_count, count + 1)
        self.assertEqual(controller.notice_pending["phone_attempts"], 7)
        self.assertEqual(controller.notice_retry_due, 2880)
        failed = [row for row in controller.journal if row["kind"] == "notify_failed"]
        gave_up = [row for row in controller.journal if row["kind"] == "notify_gave_up"]
        self.assertEqual(len(failed), 7)
        self.assertEqual(len(gave_up), 0)
        self.assertEqual(entity.hass.bus.async_fire.call_count, events)
        self.assertEqual(self.notification.call_args.kwargs, original.kwargs)
        self.assertEqual(
            self.notification.call_args.args[1],
            original.args[1] + " Phone delivery failed; check the notification "
            "service in the integration options.",
        )
        self.dismiss.assert_not_called()
        tags = {
            call.args[2]["data"]["tag"]
            for call in entity.hass.services.async_call.await_args_list
        }
        self.assertEqual(tags, {original.kwargs["notification_id"]})

    async def test_success_after_five_failures_clears_count_warning_and_retries(self):
        entity, controller = await self.failing()
        notice = controller.notice_pending
        await self.first_five(controller)
        self.assertIn("Phone delivery failed", self.notification.call_args.args[1])
        entity.hass.services.async_call.side_effect = None
        await self.retry_at(controller, 1680)
        self.assertNotIn("phone_attempts", notice)
        self.assertIsNone(controller.notice_pending)
        self.assertNotIn("Phone delivery failed", self.notification.call_args.args[1])
        await self.retry_at(controller, 5000)
        self.assertEqual(entity.hass.services.async_call.await_count, 6)

    async def test_replacement_starts_fresh_without_inheriting_delay(self):
        entity, controller = await self.failing()
        await self.retry_at(controller, 60)
        controller._notice("now_off", False)
        await self.drain()
        self.assertEqual(controller.notice_pending["kind"], "now_off")
        self.assertEqual(controller.notice_pending["phone_attempts"], 1)
        self.assertEqual(controller.notice_retry_due, 120)
        self.assertEqual(entity.hass.services.async_call.await_count, 3)

    async def test_queue_yields_waiting_failure_and_retries_each_notice_in_order(self):
        entity, controller = await self.failing()
        controller._notice("on_unconfirmed", True)
        await self.drain()
        self.assertEqual(entity.hass.services.async_call.await_count, 2)
        await self.first_five(controller)
        self.assertEqual(entity.hass.services.async_call.await_count, 10)
        self.assertEqual(controller.notice_retry_due, 1680)
        messages = [
            call.args[2]["message"]
            for call in entity.hass.services.async_call.await_args_list
        ]
        self.assertEqual(messages, messages[:2] * 5)
        self.assertNotEqual(messages[0], messages[1])

        # Recovery of the second notice must not wait for the permanently failing first.
        async def deliver(_domain, _action, payload, **_kwargs):
            if payload["message"] == messages[0]:
                error = "synthetic"
                raise RuntimeError(error)

        entity.hass.services.async_call.side_effect = deliver
        await self.retry_at(controller, 1680)
        self.assertEqual(controller.notice_pending["kind"], "off_unconfirmed")
        self.assertFalse(controller.notice_queue)
        self.assertNotIn("Phone delivery failed", self.notification.call_args.args[1])
        await self.retry_at(controller, 2280)
        # An older failed notice cannot overwrite the latest successful bell.
        self.assertNotIn("Phone delivery failed", self.notification.call_args.args[1])

    async def test_ready_queue_retains_fifo_order_during_an_inflight_failure(self):
        entity, controller, _ = await self.make()
        entity.coordinator.config_entry.options["light_safety_notify_service"] = (
            "notify.fixture_phone"
        )
        entered, release = asyncio.Event(), asyncio.Event()

        async def deliver(*_args, **_kwargs):
            entered.set()
            await release.wait()
            error = "synthetic"
            raise RuntimeError(error)

        entity.hass.services.async_call.side_effect = deliver
        controller._notice("off_unconfirmed", True)
        await entered.wait()
        controller._notice("on_unconfirmed", True)
        with patch.object(
            controller, "_publish_notice", side_effect=RuntimeError("synthetic")
        ):
            controller._notice("camera_gap", True)
        with patch.object(
            controller, "_publish_notice", wraps=controller._publish_notice
        ) as publish:
            release.set()
            await self.drain()
        self.assertEqual(
            [call.args[0]["kind"] for call in publish.call_args_list],
            ["on_unconfirmed", "camera_gap"],
        )
        messages = [
            call.args[2]["message"]
            for call in entity.hass.services.async_call.await_args_list
        ]
        self.assertEqual(len(messages), 2)
        self.assertTrue(messages[1].startswith("Didn't turn on"))
        self.assertEqual(controller.notice_pending["kind"], "off_unconfirmed")

    async def test_disable_stops_pending_retries(self):
        entity, controller = await self.failing()
        await self.first_five(controller)
        entity.coordinator.config_entry.options[CONF_LIGHT_CORRECTION] = False
        controller.options_updated()
        await self.retry_at(controller, 1680)
        self.assertEqual(entity.hass.services.async_call.await_count, 5)
        self.assertIsNone(controller.notice_task)

    async def test_close_stops_pending_retries(self):
        entity, controller = await self.failing()
        await self.first_five(controller)
        controller.stop("closed")
        await self.retry_at(controller, 1680)
        self.assertEqual(entity.hass.services.async_call.await_count, 5)
        self.assertIsNone(controller.notice_task)

    async def test_separate_gap_bell_does_not_prevent_clearing_phone_failure(self):
        entity, controller = await self.failing()
        await self.first_five(controller)
        failed_bell = self.notification.call_args
        controller._notice("camera_gap", True)
        gap_bell = self.notification.call_args
        entity.hass.services.async_call.side_effect = None
        await self.retry_at(controller, 1680)
        recovered_bell = self.notification.call_args
        self.assertEqual(recovered_bell.kwargs, failed_bell.kwargs)
        self.assertNotEqual(
            recovered_bell.kwargs["notification_id"],
            gap_bell.kwargs["notification_id"],
        )
        self.assertNotIn("Phone delivery failed", recovered_bell.args[1])
        self.dismiss.assert_not_called()

    async def test_supersession_after_five_failures_stops_old_retries(self):
        entity, controller = await self.failing()
        await self.first_five(controller)
        entity.hass.services.async_call.side_effect = None
        controller._notice("now_off", False)
        await self.drain()
        await self.retry_at(controller, 1680)
        self.assertEqual(entity.hass.services.async_call.await_count, 6)
        self.assertIsNone(controller.notice_pending)

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
        self.assertEqual(restored.notice_pending["phone_attempts"], 5)
        self.assertEqual(entity.hass.services.async_call.await_count, 5)
        stored = next(iter(fixtures.MemoryStore.rows.values()))
        self.assertEqual(stored["notice_pending"]["phone_attempts"], 5)
        self.assertNotIn("retry_due", stored["notice_pending"])
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
