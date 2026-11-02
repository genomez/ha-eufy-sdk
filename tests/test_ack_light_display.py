"""Acknowledged light presentation stays separate from receipts and OFF safety."""

# ruff: noqa: ANN001, ANN003, ANN201, ANN202, D101, D102, FBT002, FBT003, INP001, PT009, PT027, SLF001
import unittest
from unittest.mock import AsyncMock, patch

import test_late_light_settle as timing
import test_light_correction as safety

from custom_components.eufy_sdk.api import CommandReceipt


class AckDisplayTests(unittest.IsolatedAsyncioTestCase):
    _entity = timing.LateLightSettleTests._entity
    _report = timing.LateLightSettleTests._report
    clock = timing.LateLightSettleTests.clock
    advance = timing.LateLightSettleTests.advance
    receipt = timing.LateLightSettleTests.receipt
    snapshot = timing.LateLightSettleTests.snapshot

    def acknowledged(self, initial=True):
        entity, coordinator, client, seen = self.clock(initial)
        client.set_property.return_value = CommandReceipt(
            "acknowledged", True, "unconfirmed"
        )
        return entity, coordinator, client, seen

    async def test_recorded_stale_report_at_eleven_then_matching_at_thirty_two(self):
        for target in (False, True):
            with self.subTest(target=target):
                entity, coordinator, _, seen = self.acknowledged(not target)
                await entity.write(target)
                await self.advance(11)
                self.receipt(entity, not target)
                self.assertIs(
                    coordinator.data["fixture-device"]["state"]["light"], not target
                )
                await self.advance(32)
                self.assertEqual(seen, [target])
                self.receipt(entity, target)
                self.assertIsNone(entity._light_guard_owner)
                self._report(entity, not target)
                self.assertEqual(seen, [target, not target])

    async def test_persistent_contrary_report_is_visible_at_exact_deadline(self):
        entity, _, _, seen = self.acknowledged()
        await entity.write(False)
        await self.advance(11)
        self._report(entity, True)
        await self.advance(44.99)
        self.assertEqual(seen, [False])
        await self.advance(45)
        self.assertEqual(seen, [False, True])
        self.assertIsNone(entity._light_guard_owner)

    async def test_late_contrary_and_unknown_are_visible_after_deadline(self):
        entity, _, _, seen = self.acknowledged()
        await entity.write(False)
        await self.advance(46)
        self._report(entity, None)
        self.assertEqual(seen, [False, True, None])

    async def test_new_ack_supersedes_old_display_deadline(self):
        entity, _, _, seen = self.acknowledged()
        await entity.write(False)
        old = list(self.events)
        await self.advance(10)
        await entity.write(True)
        for _, callback, _ in old:
            callback(None)
        self.assertEqual(entity._light_guard_owner, 2)
        self.assertEqual(seen, [False, True])

    async def test_pending_acceptance_and_failed_command_never_start_ack_display(self):
        entity, _, client, _ = self.acknowledged()
        client.set_property.return_value = CommandReceipt(
            "pending_accepted", True, "unconfirmed"
        )
        await entity.write(False)
        self.assertIsNone(entity._light_guard_owner)
        client.set_property = AsyncMock(side_effect=RuntimeError("synthetic failure"))
        with self.assertRaises(RuntimeError):
            await entity.write(False)
        self.assertIsNone(entity._light_guard_owner)


class AckSafetyTests(safety.SafetyFixture, unittest.IsolatedAsyncioTestCase):
    def setup_light(self, **kwargs):
        result = super().setup_light(**kwargs)

        def later(_hass, seconds, action):
            event = [self.now + seconds, action, False]
            self.events.append(event)
            return lambda: event.__setitem__(2, True)

        timer = patch("custom_components.eufy_sdk.entity.async_call_later", later)
        timer.start()
        self.addCleanup(timer.stop)
        return result

    async def test_raw_on_during_hold_then_off_retry_after_sixty_second_delay(self):
        entity, coordinator, _, seen = await self.activate()
        controller = entity._light_correction
        self.observation(True)
        await entity.write(False)
        self.assertEqual(controller.retry_due, 60)
        seen.clear()
        seen.append(entity.is_on)
        await self.advance(11)
        self._report(entity, True)
        self.observation(True)
        self.assertTrue(coordinator.data["fixture-device"]["state"]["light"])
        self.assertTrue(controller.observed)
        self.assertFalse(entity.is_on)
        await self.advance(44)
        self.assertEqual(seen, [False])
        self.assertTrue(controller.observed)
        self.assertEqual(len(self.commands), 1)
        await self.advance(45)
        self.assertEqual(seen, [False, True])
        self.assertIsNone(entity._light_guard_owner)
        await self.advance(59.99)
        self.assertEqual(len(self.commands), 1)
        # The 60-second OFF delay is checked on a 15-second policy tick.
        await self.advance(60)
        self.assertEqual(len(self.commands), 1)
        await self.advance(71)
        self.assertEqual(
            [(stamp, value) for stamp, value, _ in self.commands],
            [(0, False), (71, False)],
        )
        self.assertTrue(entity.is_on)
        self.assertIsNone(entity._light_guard_owner)

    async def test_raw_on_and_existing_stuck_incident_survive_new_off_hold(self):
        entity, _, _, _ = await self.activate()
        self.observation(True)
        await entity.write(False)
        await self.advance(301)
        self.observation(True)
        controller = entity._light_correction
        self.assertIsNotNone(controller.off_stuck)
        incident = controller.off_stuck["incident_id"]
        await entity.write(False)
        await self.advance(312)
        self._report(entity, True)
        self.observation(True)
        self.assertEqual(entity.state, "off")
        self.assertTrue(controller.observed)
        self.assertEqual(controller.off_stuck["incident_id"], incident)
        self.assertEqual(entity.extra_state_attributes["off_stuck_incident"], incident)
        await self.advance(346)
        self.assertEqual(entity.state, "on")
