"""Exercise floodlight policy using actual entity writes and synthetic reports."""

# ruff: noqa: ANN001, ANN002, ANN003, ANN201, ANN202, D102, FBT003
# ruff: noqa: INP001, PT009, SLF001, PLR2004

import asyncio
import unittest
from copy import deepcopy
from itertools import pairwise
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import AsyncMock, Mock, patch

import test_floodlight_state_hold as fixtures

from custom_components.eufy_sdk.api import CommandReceipt
from custom_components.eufy_sdk.const import CONF_LIGHT_CORRECTION
from custom_components.eufy_sdk.light_correction import (
    CAP_SECONDS,
    LightCorrection,
)


class MemoryStore:
    """Persist only the controller's real allowlisted storage projection."""

    rows: ClassVar[dict] = {}

    def __init__(self, _hass, _version, key) -> None:
        """Bind a key without reading the real HA store."""
        self.key = key

    async def async_load(self):
        return deepcopy(self.rows.get(self.key))

    def async_delay_save(self, data, _delay):
        self.rows[self.key] = deepcopy(data())


class FloodlightSafetyTests(unittest.IsolatedAsyncioTestCase):
    """Use fake monotonic time, genuine guards and timestamped RTC input."""

    _entity = fixtures.FloodlightStateHoldTests._entity
    _report = fixtures.FloodlightStateHoldTests._report

    async def asyncSetUp(self):
        self.now = 0.0
        self.wall = 1_800_000_000.0
        self.tasks = []
        self.sent = []
        self.timers = []
        self.controllers = []
        MemoryStore.rows = {}
        self.states = {}

        def later(_hass, delay, action):
            row = [self.now + delay, action, False]
            self.timers.append(row)
            return lambda: row.__setitem__(2, True)

        real_sleep = asyncio.sleep

        async def sleep(delay):
            self.now += delay
            await real_sleep(0)

        for target, value in (
            ("monotonic", lambda: self.now),
            ("time", lambda: self.wall + self.now),
            ("async_call_later", later),
            ("Store", MemoryStore),
        ):
            item = patch(f"custom_components.eufy_sdk.light_correction.{target}", value)
            item.start()
            self.addCleanup(item.stop)
        self.sleep_patch = patch(
            "custom_components.eufy_sdk.light_correction.asyncio.sleep", sleep
        )
        self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)
        self.notification = self.enterContext(
            patch(
                "custom_components.eufy_sdk.light_correction.persistent_notification.async_create"
            )
        )
        self.dismiss = self.enterContext(
            patch(
                "custom_components.eufy_sdk.light_correction.persistent_notification.async_dismiss"
            )
        )

    async def asyncTearDown(self):
        for controller in self.controllers:
            controller.entity._removed = True
            controller.stop("closed")
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def make(self, *, enabled=True, binding=None, index=0, coordinator=None):
        entity, own_coordinator, client, _ = self._entity()
        sn = f"fixture-light-{index}"
        if coordinator is None:
            coordinator = own_coordinator
            coordinator.data = {"fixture-hub": {"model": "T9000"}}
            coordinator.config_entry.entry_id = "fixture-entry"
            coordinator.config_entry.options = {CONF_LIGHT_CORRECTION: enabled}
        entity.coordinator = coordinator
        entity._sn = sn
        coordinator.data[sn] = {
            "state": {"light": False},
            "model": "T8425",
            "stationSn": "fixture-hub",
        }
        if binding:
            coordinator.config_entry.options.setdefault(
                "light_safety_intent_entities", {}
            )[sn] = binding
        entity.entity_id = f"switch.fixture_light_{index}"
        entity.hass.states.get.side_effect = lambda key: (
            SimpleNamespace(state=self.states[key]) if key in self.states else None
        )

        def task(coro):
            result = asyncio.create_task(coro)
            self.tasks.append(result)
            return result

        entity.hass.async_create_task.side_effect = task
        entity.hass.services.async_call = AsyncMock()
        client = coordinator.config_entry.runtime_data.client
        client.connected = True
        client.bounded_state_writes_supported = True
        client.late_contrary_effects_supported = True
        client.get_properties = AsyncMock(
            return_value=[{"name": "light", "boundedStateWrites": True}]
        )
        client.confirm_light = AsyncMock(
            return_value={"confirmed": False, "recoveryBusy": False, "intent": 1}
        )
        client.pause_light_correction = Mock()

        async def send(target, _prop, desired, **kwargs):
            guard = kwargs.get("_send_guard")
            if guard is not None and not guard():
                return CommandReceipt(
                    "cancelled_before_send", False, "unconfirmed", intent=1
                )
            self.sent.append(
                (self.now, target, desired, kwargs.get("automatic", False))
            )
            return CommandReceipt("acknowledged", True, "unconfirmed", intent=1)

        client.set_property = AsyncMock(side_effect=send)
        controller = LightCorrection(entity)
        entity._light_correction = controller
        self.controllers.append(controller)
        await controller.async_added()
        return entity, controller, client

    async def drain(self):
        for _ in range(24):
            await asyncio.sleep(0)
        for task in self.tasks:
            if task.done() and not task.cancelled():
                task.result()

    async def at(self, value):
        self.now = value
        for controller in self.controllers:
            controller.check_policy()
        await self.drain()

    def report(self, controller, value, **telemetry):
        # Each report is distinct even if several arrive in the same test tick.
        self.now += 0.001
        controller.observe(
            {
                "source": "rtc",
                "observedAtMs": (self.wall + self.now) * 1000,
                "properties": {"light": value, **telemetry},
            }
        )

    async def test_toggle_off_preserves_explicit_display_and_sends_no_extra_work(self):
        entity, controller, client = await self.make(enabled=False)
        await entity.write(True)
        self.assertTrue(entity.is_on)
        await self.at(4000)
        self.report(controller, True, motion=False)
        controller.motion()
        self.assertEqual(len(self.sent), 1)
        self.assertIsNone(controller.timer)
        self.assertIsNone(controller.store)
        self.assertEqual(controller.attributes, {})
        client.confirm_light.assert_not_awaited()
        self.notification.assert_not_called()
        entity.hass.bus.async_fire.assert_not_called()

    async def test_non_floodlight_excluded(self):
        entity, _controller, _ = await self.make()
        entity.device["model"] = "T8410"
        await entity.write(False)
        await self.at(4000)
        self.assertEqual(len(self.sent), 1)

    async def test_binding_states_and_precedence(self):
        entity, controller, _ = await self.make(binding="sensor.fixture_intent")
        for state, expected in (
            ("on", True),
            ("off", False),
            ("paused", None),
            ("unavailable", None),
            ("unknown", None),
        ):
            with self.subTest(state=state):
                self.states["sensor.fixture_intent"] = state
                await entity.write(state != "on")
                self.assertIs(controller.intent(), expected)
                self.assertIsNone(controller.pending)
        self.states.clear()
        self.assertIsNone(controller.intent())

    async def test_binding_on_without_ha_request_never_sends_on(self):
        self.states["sensor.fixture_intent"] = "on"
        _, controller, _ = await self.make(binding="sensor.fixture_intent")
        self.report(controller, False)
        await self.at(3600)
        self.assertEqual(self.sent, [])

    async def test_restored_on_inhibits_off_but_never_authorizes_on(self):
        entity, controller, _ = await self.make()
        await entity.write(True)
        self.assertTrue(controller.on_authorized)
        controller.stop("closed")
        restored = LightCorrection(entity)
        entity._light_correction = restored
        self.controllers.append(restored)
        await restored.async_added()
        self.assertTrue(restored.last_command)
        self.assertFalse(restored.on_authorized)
        self.assertIsNone(restored.pending)
        self.report(restored, False)
        await self.at(4000)
        self.assertEqual(len(self.sent), 1)

    async def test_quiet_rule_requires_full_on_and_quiet_windows(self):
        _, controller, _ = await self.make()
        self.report(controller, True, motion=False, person=False)
        await self.at(899)
        self.report(controller, True, motion=False, person=False)
        await self.drain()
        self.assertEqual(self.sent, [])
        await self.at(901)
        self.assertEqual(len(self.sent), 1)
        self.assertFalse(self.sent[0][2])

    async def test_repeated_motion_waits_until_hard_cap(self):
        _, controller, _ = await self.make()
        self.report(controller, True)
        for instant in (600, 900, 1200, 1700, 1799):
            await self.at(instant)
            controller.motion()
            self.report(controller, True, motion=True)
            await self.drain()
            self.assertEqual(self.sent, [])
        await self.at(1801)
        self.assertEqual(len(self.sent), 1)

    async def test_detection_push_quiet_rule_includes_already_on_startup(self):
        _, controller, _ = await self.make()
        self.report(controller, True)
        await self.at(901)
        self.report(controller, True)
        await self.drain()
        self.assertEqual(len(self.sent), 1)

    async def test_intent_off_requires_two_minutes(self):
        self.states["sensor.fixture_intent"] = "on"
        _, controller, _ = await self.make(binding="sensor.fixture_intent")
        self.report(controller, True)
        await self.at(1900)
        self.states["sensor.fixture_intent"] = "off"
        controller.check_policy()
        self.report(controller, True)
        await self.at(2019)
        self.report(controller, True)
        await self.drain()
        self.assertEqual(self.sent, [])
        await self.at(2021)
        self.assertEqual(len(self.sent), 1)

    async def test_optimistic_off_is_not_confirmation(self):
        entity, controller, _ = await self.make()
        self.report(controller, True)
        await entity.write(False)
        self.assertFalse(entity.is_on)
        self.assertIs(controller.pending, False)
        await self.at(60.1)
        self.assertEqual(len(self.sent), 2)

    async def test_predispatch_guard_rechecks_intent_after_manifest_await(self):
        self.states["sensor.fixture_intent"] = "off"
        _entity, controller, client = await self.make(binding="sensor.fixture_intent")
        self.report(controller, True)

        async def manifest(_target):
            self.states["sensor.fixture_intent"] = "on"
            return [{"name": "light", "boundedStateWrites": True}]

        client.get_properties.side_effect = manifest
        self.now = 1801
        self.report(controller, True)
        await self.drain()
        self.assertEqual(self.sent, [])

    async def test_off_backoff_and_stop_on_new_on(self):
        entity, controller, _ = await self.make()
        await entity.write(False)
        for instant, count in (
            (59, 1),
            (60, 2),
            (179, 2),
            (180, 3),
            (479, 3),
            (480, 4),
            (780, 5),
        ):
            await self.at(instant)
            self.assertEqual(len(self.sent), count)
        await entity.write(True)
        self.report(controller, True)
        await self.at(1500)
        self.assertEqual(len(self.sent), 6)

    async def test_offline_wait_and_resume_trigger(self):
        entity, controller, _ = await self.make()
        await entity.write(False)
        entity.device["cameraHealth"] = {"connectivity": "offline"}
        await self.at(65)
        self.assertEqual(len(self.sent), 1)
        entity.device["cameraHealth"] = {"connectivity": "online"}
        controller.check_policy()
        await self.drain()
        self.assertEqual(len(self.sent), 2)

    async def test_degraded_health_wait_is_not_an_attempt(self):
        entity, controller, _ = await self.make()
        await entity.write(False)
        controller.health(
            {"status": "command_degraded", "cooldownRemainingMs": 600000, "revision": 1}
        )
        await self.at(300)
        self.assertEqual(controller.attempts, 1)
        self.assertEqual(len(self.sent), 1)
        await self.at(601)
        self.assertEqual(len(self.sent), 2)

    async def test_on_retry_schedule_stop_on_off(self):
        entity, controller, _ = await self.make()
        await entity.write(True)
        for instant, count in (
            (29, 1),
            (30, 2),
            (89, 2),
            (90, 3),
            (210, 4),
            (510, 5),
            (810, 6),
        ):
            await self.at(instant)
            self.assertEqual(len(self.sent), count)
        await entity.write(False)
        self.report(controller, False)
        await self.at(1300)
        self.assertEqual(len(self.sent), 7)

    async def test_on_read_first_suppresses_duplicate(self):
        entity, _controller, client = await self.make()
        await entity.write(True)
        client.confirm_light.return_value = {
            "intent": 1,
            "confirmed": True,
            "observedValue": True,
            "recoveryBusy": False,
        }
        await self.at(30)
        self.assertEqual(len(self.sent), 1)
        client.confirm_light.assert_awaited_once()

    async def test_one_outstanding_and_pending_sdk_child_blocks_whole_hub(self):
        entity, controller, client = await self.make()
        other, _second, _ = await self.make(index=1, coordinator=entity.coordinator)
        await entity.write(True)
        controller.receipt(
            CommandReceipt("pending_accepted", True, "unconfirmed", intent=1)
        )
        await other.write(False)
        client.confirm_light.return_value = {
            "intent": 1,
            "confirmed": False,
            "recoveryBusy": True,
        }
        await self.at(60)
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(controller.recovery_pending)
        client.confirm_light.return_value = {
            "intent": 1,
            "confirmed": False,
            "recoveryBusy": False,
        }
        await self.at(75)
        self.assertGreater(len(self.sent), 2)

    async def test_one_controller_rpc_outstanding_per_hub(self):
        entity, controller, client = await self.make()
        _, second, _ = await self.make(index=1, coordinator=entity.coordinator)
        self.report(controller, True)
        self.report(second, True)
        active, maximum = 0, 0
        release = asyncio.Event()
        original = client.set_property.side_effect

        async def blocked(*args, **kwargs):
            nonlocal active, maximum
            active += 1
            maximum = max(active, maximum)
            await release.wait()
            result = await original(*args, **kwargs)
            active -= 1
            return result

        client.set_property.side_effect = blocked
        self.now = 1801
        self.report(controller, True)
        self.report(second, True)
        await self.drain()
        self.assertEqual(active, 1)
        release.set()
        await self.drain()
        self.assertEqual(maximum, 1)
        self.assertEqual(len(self.sent), 2)

    async def test_five_lights_share_three_second_spacing(self):
        entity, _controller, _ = await self.make()
        for index in range(1, 5):
            await self.make(index=index, coordinator=entity.coordinator)
        for candidate in self.controllers:
            self.report(candidate, True)
        self.now = CAP_SECONDS + 1
        for candidate in self.controllers:
            self.report(candidate, True)
        await self.drain()
        self.assertEqual(len(self.sent), 5)
        times = [row[0] for row in self.sent]
        self.assertTrue(all(after - before >= 3 for before, after in pairwise(times)))

    async def test_three_minute_native_motion_cycle_has_no_alert_or_command(self):
        _, controller, _ = await self.make()
        self.report(controller, True)
        controller.motion()
        await self.at(180)
        self.report(controller, False)
        await self.drain()
        self.assertEqual(self.sent, [])
        self.notification.assert_not_called()

    async def test_fresh_confirmation_resolves_problem_using_same_tag(self):
        entity, controller, _ = await self.make()
        entity.coordinator.config_entry.options["light_safety_notify_service"] = (
            "notify.fixture_phone"
        )
        await entity.write(False)
        self.report(controller, True)
        await self.at(300)
        self.assertEqual(controller.notice_kind, "off_unconfirmed")
        self.report(controller, False)
        await self.drain()
        self.assertEqual(controller.notice_kind, "now_off")
        calls = entity.hass.services.async_call.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            calls[0].args[2]["data"]["tag"], calls[1].args[2]["data"]["tag"]
        )
        self.assertEqual(calls[0].args[2]["data"]["importance"], "high")
        self.assertTrue(calls[0].args[2]["data"]["sticky"])
        self.assertNotIn("sticky", calls[1].args[2]["data"])
        self.assertIsNone(controller.pending)

    async def test_on_failure_alert_after_ten_minutes(self):
        entity, controller, _ = await self.make()
        await entity.write(True)
        self.report(controller, False)
        await self.at(599)
        self.assertNotEqual(controller.notice_kind, "on_unconfirmed")
        self.report(controller, False)
        await self.at(600)
        self.assertEqual(controller.notice_kind, "on_unconfirmed")

    async def test_notify_failure_bell_fallback_and_separate_delivery_retry(self):
        entity, controller, _ = await self.make()
        entity.coordinator.config_entry.options["light_safety_notify_service"] = (
            "notify.fixture_phone"
        )
        entity.hass.services.async_call.side_effect = RuntimeError("synthetic")
        controller._notice("off_unconfirmed", True)
        await self.drain()
        self.notification.assert_called_once()
        self.assertIsNotNone(controller.notice_pending)
        self.assertEqual(controller.journal[-1]["kind"], "notify_failed")
        entity.hass.services.async_call.side_effect = None
        await self.at(60)
        self.assertIsNone(controller.notice_pending)
        self.assertEqual(self.sent, [])
        self.notification.assert_called_once()

    async def test_alert_dedupe_survives_restart(self):
        entity, controller, _ = await self.make()
        controller._notice("camera_gap", True)
        await self.drain()
        controller.stop("closed")
        restored = LightCorrection(entity)
        self.controllers.append(restored)
        await restored.async_added()
        restored._notice("camera_gap", True)
        await self.drain()
        self.notification.assert_called_once()

    async def test_paused_binding_is_not_an_intent_gap(self):
        self.states["sensor.fixture_intent"] = "paused"
        _, controller, _ = await self.make(binding="sensor.fixture_intent")
        await self.at(601)
        self.assertEqual(controller.state, "paused")
        self.notification.assert_not_called()

    async def test_unavailable_binding_reports_gap_after_five_minutes(self):
        self.states["sensor.fixture_intent"] = "unavailable"
        _, controller, _ = await self.make(binding="sensor.fixture_intent")
        await self.at(299)
        self.notification.assert_not_called()
        await self.at(301)
        self.assertEqual(controller.notice_kind, "intent_gap")

    async def test_missing_camera_reports_cannot_authorize_watchdog(self):
        _, controller, _ = await self.make()
        self.report(controller, True)
        await self.at(2000)
        self.assertEqual(self.sent, [])
        self.assertEqual(controller.state, "protection gap")

    async def test_journal_bounded_and_contains_no_transport_identity(self):
        entity, controller, _ = await self.make()
        for _ in range(100):
            controller.motion()
        self.assertEqual(len(controller.journal), 80)
        self.assertNotIn(entity._sn, str(list(controller.journal)))
        self.assertTrue(all(set(row) == {"at", "kind"} for row in controller.journal))

    async def test_old_contrary_path_never_sends_after_fifteen_seconds(self):
        entity, controller, _ = await self.make()
        await entity.write(False)
        controller.event(
            {
                "intent": 1,
                "state": "contrary",
                "observedValue": True,
                "recoveryBusy": False,
            }
        )
        await self.at(15)
        self.assertEqual(len(self.sent), 1)

    async def test_second_watchdog_episode_gets_a_new_confirmed_off_notice(self):
        _, controller, _ = await self.make()
        for base in (0, 2000):
            self.now = base
            self.report(controller, True)
            self.now = base + 1801
            self.report(controller, True)
            await self.drain()
            self.report(controller, False)
            await self.drain()
        notices = [
            call
            for call in controller.entity.hass.bus.async_fire.call_args_list
            if call.args[1]["kind"] == "safety_off"
        ]
        self.assertEqual(len(notices), 2)

    async def test_paused_time_does_not_count_toward_intent_gap(self):
        self.states["sensor.fixture_intent"] = "paused"
        _, controller, _ = await self.make(binding="sensor.fixture_intent")
        await self.at(1000)
        self.states["sensor.fixture_intent"] = "unavailable"
        controller.check_policy()
        await self.at(1299)
        self.assertNotIn("intent_gap", controller.notice_kinds)
        await self.at(1301)
        self.assertIn("intent_gap", controller.notice_kinds)

    async def test_multiple_recovery_tokens_settle_independently(self):
        _, controller, _ = await self.make()
        controller.track_recovery(
            CommandReceipt("pending_accepted", True, "unconfirmed", intent=1)
        )
        controller.track_recovery(
            CommandReceipt("pending_accepted", True, "unconfirmed", intent=2)
        )
        self.now = 50
        controller.event({"intent": 1, "recoveryBusy": False})
        self.assertEqual(set(controller.recoveries), {2})
        controller.event({"intent": 2, "recoveryBusy": False})
        self.assertFalse(controller.recovery_pending)

    async def test_predispatch_failure_does_not_park_the_hub(self):
        entity, controller, client = await self.make()
        await entity.write(False)
        client.set_property.side_effect = RuntimeError("synthetic preparation failure")
        await self.at(60)
        self.assertFalse(controller.recovery_pending)
        self.assertFalse(controller.queue.busy())

    async def test_binding_off_revokes_automatic_rpc_and_its_retained_child(self):
        self.states["sensor.fixture_intent"] = "on"
        entity, controller, client = await self.make(binding="sensor.fixture_intent")
        await entity.write(True)
        await self.at(30)
        self.assertTrue(controller.automatic_owned)
        self.states["sensor.fixture_intent"] = "off"
        controller.check_policy()
        client.pause_light_correction.assert_called_with(
            entity._sn, keep_observation=True
        )
        self.assertFalse(controller.on_authorized)
        self.assertIsNone(controller.pending)

    async def test_unqueryable_recovery_expires_without_gap_alert(self):
        _, controller, client = await self.make()
        controller.track_recovery(
            CommandReceipt("pending_accepted", True, "unconfirmed", intent=1)
        )
        client.confirm_light.side_effect = RuntimeError("synthetic unavailable watch")
        await self.at(50)
        self.report(controller, False)
        await self.at(224)
        self.report(controller, False)
        await self.drain()
        self.assertNotIn("outcome_gap", controller.notice_kinds)
        self.assertTrue(controller.recovery_pending)
        await self.at(225)
        self.assertFalse(controller.recovery_pending)
        self.assertIsNone(controller.outcome_unknown_since)
        self.assertIn(
            "recovery_wait_expired", [row["kind"] for row in controller.journal]
        )
