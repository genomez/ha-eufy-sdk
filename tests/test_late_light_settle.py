"""Offline timing regressions for bounded, contrary-value light settling."""

# ruff: noqa: ANN001, ANN002, ANN201, ANN202, D102, FBT002, FBT003, INP001, PLR2004, PT009, PT027, SLF001

import asyncio
import unittest
from unittest.mock import patch

import test_floodlight_state_hold as fixtures


class LateLightSettleTests(unittest.IsolatedAsyncioTestCase):
    """Exercise real light entities/cache with a manual offline clock."""

    _entity = fixtures.FloodlightStateHoldTests._entity
    _report = fixtures.FloodlightStateHoldTests._report

    def clock(self, initial=True, prop="light"):
        entity, coordinator, client, seen = self._entity(initial=initial, prop=prop)
        self.now, self.events, self.tasks = 0, [], []

        def later(_hass, seconds, callback):
            event = [self.now + seconds, callback, False]
            self.events.append(event)

            def cancel():
                event[2] = True

            return cancel

        entity.hass.async_create_task.side_effect = lambda coro: self.tasks.append(
            asyncio.create_task(coro)
        )
        p = patch("custom_components.eufy_sdk.entity.async_call_later", later)
        p.start()
        self.addCleanup(p.stop)
        return entity, coordinator, client, seen

    async def advance(self, target):
        while True:
            due = [e for e in self.events if not e[2] and e[0] <= target]
            if not due:
                break
            event = min(due, key=lambda e: e[0])
            self.now, event[2] = event[0], True
            event[1](None)
            if self.tasks:
                tasks, self.tasks = self.tasks, []
                await asyncio.gather(*tasks)
        self.now = target

    def snapshot(self, entity, value):
        coordinator = entity.coordinator
        coordinator.data["fixture-device"]["state"][entity._prop] = value
        coordinator.snapshot_generation += 1
        coordinator.completed_snapshot_generation = coordinator.snapshot_generation
        entity._handle_coordinator_update()

    async def test_agreement_at_twenty_then_six_second_late_on_is_suppressed(self):
        for target in (False, True):
            with self.subTest(target=target):
                entity, coordinator, _client, seen = self.clock(initial=not target)

                async def refresh(entity=entity, target=target):
                    self.snapshot(entity, target)

                coordinator.async_request_refresh.side_effect = refresh
                await entity.write(target)
                await self.advance(20)
                self.assertIsNone(entity._optimistic_owner)
                await self.advance(22)
                self._report(entity, not target)
                await self.advance(27.9)
                self.assertEqual(seen, [target])
                self._report(entity, target)
                await self.advance(30)
                self.assertEqual(seen, [target])

    async def test_stale_snapshot_at_twenty_then_corrective_snapshot_at_twenty_eight(
        self,
    ):
        entity, coordinator, _client, seen = self.clock()

        async def refresh():
            self.snapshot(entity, True)

        coordinator.async_request_refresh.side_effect = refresh
        await entity.write(False)
        await self.advance(20)
        await self.advance(27.9)
        self.assertEqual(seen, [False])
        self.snapshot(entity, False)
        await self.advance(30)
        self.assertEqual(seen, [False])

    async def test_matching_evidence_releases_optimism_before_reconcile(self):
        entity, _coordinator, _client, seen = self.clock()
        await entity.write(False)
        await self.advance(2)
        self._report(entity, False)
        self.assertIsNone(entity._optimistic_owner)
        self.assertIsNone(entity._assumed_value)
        self.assertEqual(seen, [False])

    async def test_real_motion_change_visible_after_eight_seconds(self):
        entity, _coordinator, _client, seen = self.clock()
        await entity.write(False)
        self._report(entity, False)
        await self.advance(3)
        self._report(entity, True)
        await self.advance(10.9)
        self.assertEqual(seen, [False])
        await self.advance(11)
        self.assertEqual(seen, [False, True])
        self._report(entity, False)
        self.assertEqual(seen, [False, True, False])

    async def test_contrary_report_without_initial_agreement_is_bounded_to_eight(self):
        entity, _coordinator, _client, seen = self.clock()
        await entity.write(False)
        await self.advance(1)
        self._report(entity, True)
        await self.advance(9)
        self.assertEqual(seen, [False, True])

    async def test_unknown_value_is_not_evidence_of_a_persistent_opposite_boolean(self):
        entity, _coordinator, _client, seen = self.clock()
        await entity.write(False)
        self._report(entity, False)
        await self.advance(1)
        self._report(entity, True)
        await self.advance(4)
        self.snapshot(entity, None)
        await self.advance(10)
        self.assertEqual(seen, [False])
        self._report(entity, False)
        await self.advance(30)
        self.assertEqual(seen, [False])

    async def test_canceled_same_owner_contrary_callback_cannot_clear_new_candidate(
        self,
    ):
        entity, _coordinator, _client, seen = self.clock()
        await entity.write(False)
        self._report(entity, False)
        await self.advance(1)
        self._report(entity, True)
        old = next(event for event in self.events if event[0] == 9)
        await self.advance(3)
        self._report(entity, False)
        await self.advance(4)
        self._report(entity, True)
        old[1](None)
        self.assertEqual(seen, [False])
        await self.advance(12)
        self.assertEqual(seen, [False, True])

    async def test_repeated_same_contrary_snapshots_do_not_restart_debounce(self):
        entity, _coordinator, _client, seen = self.clock()
        await entity.write(False)
        self._report(entity, False)
        await self.advance(2)
        self._report(entity, True)
        await self.advance(7)
        self.snapshot(entity, True)
        await self.advance(10)
        self.assertEqual(seen, [False, True])

    async def test_agreement_cancels_contrary_candidate(self):
        entity, _coordinator, _client, seen = self.clock()
        await entity.write(False)
        self._report(entity, False)
        await self.advance(2)
        self._report(entity, True)
        await self.advance(5)
        self._report(entity, False)
        await self.advance(11)
        self._report(entity, True)
        await self.advance(18.9)
        self.assertEqual(seen, [False])
        await self.advance(19)
        self.assertEqual(seen, [False, True])

    async def test_deadline_bounds_ineffective_command_even_if_refresh_failed(self):
        entity, coordinator, _client, seen = self.clock()
        coordinator.last_update_success = False
        await entity.write(False)
        await self.advance(29.9)
        self.assertEqual(seen, [False])
        await self.advance(30)
        self.assertEqual(seen, [False, True])
        self.assertIsNone(entity._optimistic_owner)

    async def test_change_near_deadline_is_not_hidden_past_thirty(self):
        entity, _coordinator, _client, seen = self.clock()
        await entity.write(False)
        self._report(entity, False)
        await self.advance(28)
        self._report(entity, True)
        await self.advance(30)
        self.assertEqual(seen, [False, True])

    async def test_new_automation_command_is_immediate_and_invalidates_old_timers(self):
        entity, _coordinator, _client, seen = self.clock()
        await entity.write(False)
        self._report(entity, False)
        await self.advance(2)
        self._report(entity, True)
        old_events = list(self.events)
        await self.advance(3)
        await entity.write(True)
        for event in old_events:
            event[1](None)
        self.assertEqual(seen, [False, True])
        self.assertEqual(entity._optimistic_owner, 2)
        self.assertEqual(entity._light_guard_owner, 2)

    async def test_matching_report_during_new_command_replaces_older_guard(self):
        entity, _coordinator, client, seen = self.clock()
        await entity.write(False)
        self._report(entity, False)
        gate = asyncio.Event()

        async def pending(*_args):
            self._report(entity, True)
            await gate.wait()

        client.set_property.side_effect = pending
        task = asyncio.create_task(entity.write(True))
        await asyncio.sleep(0)
        gate.set()
        await task
        self.assertEqual(seen, [False, True])
        self.assertIsNone(entity._optimistic_owner)
        self.assertEqual(entity._light_guard_owner, 2)
        self._report(entity, False)
        self.assertEqual(seen, [False, True])

    async def test_failed_new_command_preserves_old_settle(self):
        entity, _coordinator, client, seen = self.clock()
        await entity.write(False)
        client.set_property.side_effect = RuntimeError("synthetic rejection")
        with self.assertRaisesRegex(RuntimeError, "synthetic rejection"):
            await entity.write(True)
        await self.advance(30)
        self.assertEqual(seen, [False, True])

    async def test_removal_invalidates_deadline_and_contrary_callback(self):
        entity, _coordinator, _client, seen = self.clock()
        await entity.write(False)
        self._report(entity, False)
        self._report(entity, True)
        old_events = list(self.events)
        await entity.async_will_remove_from_hass()
        for event in old_events:
            event[1](None)
        self.assertEqual(seen, [False])
        self.assertTrue(all(event[2] for event in old_events))

    async def test_no_recent_command_external_change_is_immediate(self):
        entity, _coordinator, _client, seen = self.clock(initial=False)
        self._report(entity, True)
        self.assertEqual(seen, [True])

    async def test_unrelated_cache_updates_do_not_count_as_new_light_evidence(self):
        entity, coordinator, _client, seen = self.clock()
        await entity.write(False)
        for _ in range(3):
            coordinator.data["fixture-device"]["state"]["unrelated"] = True
            entity._handle_coordinator_update()
        await self.advance(10)
        self.assertEqual(seen, [False])
        self.assertFalse(any(event[0] == 8 for event in self.events))

    async def test_non_light_keeps_existing_snapshot_release(self):
        entity, coordinator, _client, seen = self.clock(prop="enabled")

        async def refresh():
            self.snapshot(entity, True)

        coordinator.async_request_refresh.side_effect = refresh
        await entity.write(False)
        await self.advance(20)
        self.assertEqual(seen, [False, True])

    async def test_fresh_receipt_contradiction_uses_same_debounce(self):
        entity, _coordinator, _client, seen = self.clock()
        await entity.write(False)
        self._report(entity, False)
        await self.advance(22)
        fixtures.FloodlightStateHoldTests._receipt(self, entity, True)
        await self.advance(27.9)
        self.assertEqual(seen, [False])
        self._report(entity, False)
        await self.advance(30)
        self.assertEqual(seen, [False])
