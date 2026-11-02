"""Acknowledged light presentation stays separate from receipts and OFF safety."""

# ruff: noqa: ANN001, ANN201, D101, D102, FBT002, FBT003, INP001, PT009, PT027, SLF001
import unittest
from unittest.mock import AsyncMock

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
    async def test_off_verification_and_retries_run_while_display_holds(self):
        entity, coordinator, _, seen = await self.activate()
        self.observation(True)
        await entity.write(False)
        seen.clear()
        seen.append(entity.is_on)
        await self.advance(11)
        self._report(entity, True)
        self.observation(True)
        self.assertTrue(coordinator.data["fixture-device"]["state"]["light"])
        self.assertFalse(entity.is_on)
        await self.advance(44)
        self.assertEqual(seen, [False])
        await self.advance(71)
        self.assertEqual([value for _, value, _ in self.commands], [False, False])
