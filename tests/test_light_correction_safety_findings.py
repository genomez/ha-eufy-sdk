"""Regress phone visibility and uninterrupted floodlight safety episodes offline."""

# ruff: noqa: ANN001, ANN002, ANN003, ANN201, ANN202, D102, FBT003, INP001, PT009, SLF001

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import test_floodlight_safety as fixtures

from custom_components.eufy_sdk import notification_delivery
from custom_components.eufy_sdk.api import (
    CommandReceipt,
    EufySdkApiClientCommunicationError,
)
from custom_components.eufy_sdk.light_correction import LightCorrection


class SafetyFindingsTests(unittest.IsolatedAsyncioTestCase):
    """Use real policy and delivery with synthetic time, reports and transport."""

    _entity = fixtures.FloodlightSafetyTests._entity
    _report = fixtures.FloodlightSafetyTests._report
    asyncSetUp = fixtures.FloodlightSafetyTests.asyncSetUp
    asyncTearDown = fixtures.FloodlightSafetyTests.asyncTearDown
    make = fixtures.FloodlightSafetyTests.make
    drain = fixtures.FloodlightSafetyTests.drain
    at = fixtures.FloodlightSafetyTests.at
    report = fixtures.FloodlightSafetyTests.report

    def phone(self, entity):
        entity.coordinator.config_entry.options["light_safety_notify_service"] = (
            "notify.fixture_phone"
        )

    def notices(self, entity, kind):
        return [
            call.args[1]
            for call in entity.hass.bus.async_fire.call_args_list
            if call.args[1]["kind"] == kind
        ]

    async def test_watchdog_capability_abort_alerts_and_keeps_retrying(self):
        entity, controller, client = await self.make()
        self.phone(entity)
        client.get_properties.return_value = [{"name": "light"}]
        self.report(controller, True)
        self.now = 901
        self.report(controller, True)
        await self.drain()
        await self.at(916)
        self.assertIsNone(controller.pending)
        self.assertEqual(len(self.notices(entity, "off_blocked")), 1)
        self.assertEqual(entity.hass.services.async_call.await_count, 1)
        self.assertGreaterEqual(client.get_properties.await_count, 2)
        client.get_properties.return_value = [
            {"name": "light", "boundedStateWrites": True}
        ]
        await self.at(931)
        self.assertIs(controller.pending, False)
        self.assertEqual(len(self.sent), 1)
        self.report(controller, False)
        await self.drain()
        self.assertEqual(len(self.notices(entity, "now_off")), 1)

    async def test_watchdog_unavailable_error_alerts_without_exhausting_retries(self):
        entity, controller, client = await self.make()
        self.phone(entity)
        client.set_property.side_effect = EufySdkApiClientCommunicationError(
            "light_correction_unavailable"
        )
        self.report(controller, True)
        self.now = 901
        self.report(controller, True)
        await self.drain()
        await self.at(916)
        await self.at(931)
        self.assertIsNone(controller.pending)
        self.assertGreaterEqual(client.set_property.await_count, 3)
        self.assertEqual(len(self.notices(entity, "off_blocked")), 1)
        self.assertEqual(entity.hass.services.async_call.await_count, 1)

    async def test_watchdog_hub_busy_alerts_and_preserves_ha_on(self):
        entity, controller, _ = await self.make()
        _, busy, _ = await self.make(index=1, coordinator=entity.coordinator)
        self.phone(entity)
        self.report(controller, True)
        busy.explicit_busy = 1
        self.now = 901
        self.report(controller, True)
        await self.drain()
        self.assertIsNone(controller.pending)
        self.assertEqual(len(self.notices(entity, "off_blocked")), 1)
        await entity.write(True)
        busy.explicit_busy = 0
        await self.at(2000)
        self.assertTrue(controller.on_authorized)
        self.assertTrue(all(row[2] is True for row in self.sent))

    async def test_pending_off_push_during_bridge_parent_and_cooldown_waits(self):
        for index, reason in enumerate(("bridge", "parent", "cooldown")):
            with self.subTest(reason=reason):
                entity, controller, client = await self.make(index=index)
                self.phone(entity)
                await entity.write(False)
                self.report(controller, True)
                start = self.now
                if reason == "bridge":
                    client.connected = False
                elif reason == "parent":
                    entity.device["cameraHealth"] = {"parentTransport": "disconnected"}
                else:
                    controller.health_until = start + 1000
                await self.at(start + 300)
                self.assertEqual(len(self.notices(entity, "off_unconfirmed")), 1)
                self.assertEqual(entity.hass.services.async_call.await_count, 1)
                self.assertEqual(controller.off_stuck["reason"], "no_confirmation")
                controller.stop("closed")

    async def test_repeated_explicit_off_keeps_original_alert_deadline(self):
        entity, controller, client = await self.make()
        self.phone(entity)
        await entity.write(False)
        original = controller.pending_since
        self.report(controller, True)
        client.connected = False
        for moment in range(60, 301, 60):
            self.now = moment
            await entity.write(False)
            await self.drain()
        self.assertEqual(controller.pending_since, original)
        self.assertEqual(len(self.notices(entity, "off_unconfirmed")), 1)
        self.assertEqual(entity.hass.services.async_call.await_count, 1)

    async def test_binding_cycle_resets_dedupe_in_memory_and_storage(self):
        binding = "input_boolean.fixture_intent"
        self.states[binding] = "off"
        entity, controller, _ = await self.make(binding=binding)
        self.phone(entity)
        await entity.write(False)
        self.report(controller, True)
        await self.at(300)
        self.assertEqual(len(self.notices(entity, "off_unconfirmed")), 1)
        self.states[binding] = "on"
        controller.check_policy()
        self.assertIsNone(controller.pending)
        self.states[binding] = "off"
        controller.check_policy()
        self.assertNotIn("off_unconfirmed", controller.notice_kinds)
        controller.stop("closed")
        controller = LightCorrection(entity)
        entity._light_correction = controller
        self.controllers.append(controller)
        await controller.async_added()
        self.assertNotIn("off_unconfirmed", controller.notice_kinds)
        self.report(controller, True)
        self.now = 1201
        self.report(controller, True)
        await self.drain()
        await self.at(1502)
        self.assertEqual(len(self.notices(entity, "off_unconfirmed")), 2)

    async def test_disabled_topology_cannot_hold_hub_recovery_forever(self):
        entity, controller, _ = await self.make()
        other, _, _ = await self.make(index=1, coordinator=entity.coordinator)
        controller.track_recovery(
            CommandReceipt("pending_accepted", True, "unconfirmed", intent=7)
        )
        controller.uncertain_outcome()
        entity.device["model"] = "unsupported"
        self.assertFalse(controller.enabled)
        await other.write(False)
        await self.at(226)
        self.assertFalse(controller.recovery_pending)
        self.assertFalse(controller.queue.busy())
        self.assertTrue(any(row[1] == other._sn and row[3] for row in self.sent))

    async def test_storage_activation_failure_warns_and_retries_with_backoff(self):
        entity, controller, _ = await self.make()
        self.phone(entity)
        controller.loaded = controller.storage_loaded = False
        controller.store.async_load = AsyncMock(
            side_effect=OSError("synthetic storage failure")
        )
        await controller._activate()
        await self.drain()
        self.assertFalse(controller.loaded)
        self.assertEqual(controller.activation_retry_due, 60)
        self.assertEqual(len(self.notices(entity, "activation_gap")), 1)
        self.assertEqual(entity.hass.services.async_call.await_count, 1)
        self.assertIn("could not start", self.notification.call_args.args[1])
        calls = controller.store.async_load.await_count
        await self.at(59)
        self.assertEqual(controller.store.async_load.await_count, calls)
        await self.at(60)
        self.assertEqual(controller.activation_retry_due, 180)
        await self.at(180)
        self.assertEqual(controller.activation_retry_due, 480)
        controller.store.async_load.side_effect = None
        controller.store.async_load.return_value = None
        await self.at(480)
        self.assertTrue(controller.loaded)
        self.assertEqual(controller.activation_attempts, 0)
        self.assertEqual(len(self.notices(entity, "activation_restored")), 1)

    async def test_policy_activation_failure_warns_and_retries(self):
        _, controller, _ = await self.make()
        with patch.object(
            controller, "check_policy", side_effect=RuntimeError("synthetic")
        ):
            await controller._activate()
        self.assertFalse(controller.loaded)
        self.assertEqual(len(self.notices(controller.entity, "activation_gap")), 1)
        await self.at(60)
        self.assertTrue(controller.loaded)

    async def test_activation_ticks_survive_repeated_policy_failures(self):
        _, controller, _ = await self.make()
        with patch.object(
            controller, "check_policy", side_effect=RuntimeError("synthetic")
        ):
            await controller._activate()
            self.now = 60
            tick = next(row[1] for row in reversed(self.timers) if not row[2])
            tick(None)
            await self.drain()
            self.assertFalse(controller.loaded)
            self.assertEqual(controller.activation_retry_due, 180)
        self.now = 180
        tick = next(row[1] for row in reversed(self.timers) if not row[2])
        tick(None)
        await self.drain()
        self.assertTrue(controller.loaded)

    async def test_reconnects_preserve_on_deadline_and_gap_clock(self):
        _, controller, client = await self.make()
        self.report(controller, True)
        original = controller.on_since
        silence = controller.silence_since
        for moment in (400, 800):
            self.now = moment
            client.connected = False
            controller.reset_observations()
            client.connected = True
            controller.reset_observations()
            self.assertEqual(controller.on_since, original)
            self.assertEqual(controller.silence_since, silence)
            self.assertFalse(controller._fresh())
            self.report(controller, True)
        self.now = 901
        self.report(controller, True)
        await self.drain()
        self.assertEqual(len(self.sent), 1)
        self.assertIs(controller.pending, False)

    async def test_outcome_gap_alert_precedes_existing_settlement_bound(self):
        entity, controller, _ = await self.make()
        controller.uncertain_outcome()
        await self.at(164)
        self.assertEqual(self.notices(entity, "outcome_gap"), [])
        await self.at(165)
        self.assertEqual(len(self.notices(entity, "outcome_gap")), 1)
        self.assertTrue(controller.recovery_pending)
        await self.at(180)
        self.assertFalse(controller.recovery_pending)
        self.assertEqual(len(self.notices(entity, "outcome_restored")), 1)

    async def test_backward_wall_step_accepts_fresh_report_and_confirms_off(self):
        entity, controller, _ = await self.make()
        self.report(controller, True)
        self.now = 100
        await entity.write(False)
        self.wall -= 3600
        self.now = 101
        self.report(controller, False)
        self.assertIs(controller.observed, False)
        self.assertIsNone(controller.pending)
        self.assertTrue(controller._fresh())
        self.wall += 7200
        self.assertTrue(controller._fresh())
        self.now += 121
        self.assertFalse(controller._fresh())

    async def test_backward_wall_step_can_close_an_existing_off_incident(self):
        entity, controller, _ = await self.make()
        await entity.write(False)
        self.report(controller, True)
        await self.at(300)
        self.assertIsNotNone(controller.off_stuck)
        self.wall -= 3600
        self.now = 301
        self.report(controller, False)
        self.assertIsNone(controller.off_stuck)
        self.assertIsNone(controller.pending)

    async def test_backward_wall_step_does_not_make_duplicate_report_new(self):
        _, controller, _ = await self.make()
        self.report(controller, True)
        stamp = controller.observed_at
        self.now = 100
        self.wall -= 60
        controller.observe(
            {
                "source": "rtc",
                "properties": {"light": False},
                "observedAtMs": stamp * 1000,
            }
        )
        self.assertIs(controller.observed, True)
        self.assertEqual(controller.observed_at, stamp)

    async def test_confirmed_off_supersedes_blocked_phone_retry(self):
        entity, controller, _ = await self.make()
        self.phone(entity)
        entity.hass.services.async_call.side_effect = RuntimeError("synthetic")
        controller._notice("off_blocked", True)
        await self.drain()
        self.assertEqual(controller.notice_pending["kind"], "off_blocked")
        entity.hass.services.async_call.side_effect = None
        controller._notice("now_off", False)
        await self.drain()
        self.assertIsNone(controller.notice_pending)
        self.assertFalse(controller.notice_queue)

    async def test_problem_phone_envelope_and_normal_delivery_remain_compatible(self):
        entity, _, _ = await self.make()
        for problem in (True, False):
            await notification_delivery.deliver_phone_notice(
                entity.hass,
                "notify.fixture_phone",
                "Title",
                "Message",
                "tag",
                problem=problem,
            )
            call = entity.hass.services.async_call.await_args
            expected = {"tag": "tag", "channel": "Eufy"}
            if problem:
                expected.update(importance="high", sticky=True, ttl=0, priority="high")
            self.assertEqual(call.args[2]["data"], expected)
            self.assertTrue(call.kwargs["blocking"])

    async def test_timed_out_phone_service_preserves_retry_then_recovers(self):
        entity, controller, _ = await self.make()
        self.phone(entity)
        cancelled = asyncio.Event()

        async def hanging(*_args, **_kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        entity.hass.services.async_call = AsyncMock(side_effect=hanging)
        with patch.object(notification_delivery, "PHONE_NOTICE_TIMEOUT", 0):
            controller._notice("off_unconfirmed", True)
            await self.drain()
        self.assertTrue(cancelled.is_set())
        self.assertIsNone(controller.notice_task)
        self.assertEqual(controller.notice_pending["phone_attempts"], 1)
        self.assertEqual(controller.notice_retry_due, 60)
        entity.hass.services.async_call.side_effect = None
        self.now = 60
        controller._deliver_pending()
        await self.drain()
        self.assertIsNone(controller.notice_pending)
        self.assertEqual(entity.hass.services.async_call.await_count, 2)
