"""Exercise uncommanded light episodes with fake time and local observations."""

# ruff: noqa: ANN001, ANN002, ANN003, ANN201, ANN202, D102, FBT003
# ruff: noqa: INP001, PT009, PT027, SLF001
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import test_light_correction as fixtures

from custom_components.eufy_sdk.const import (
    CONF_LIGHT_CORRECTION,
    CONF_LIGHT_CORRECTION_PAUSE,
)
from custom_components.eufy_sdk.light_correction import (
    FAILURE_EVENT,
    WINDOW_SECONDS,
    LightCorrection,
)


class LightGraceTests(unittest.IsolatedAsyncioTestCase):
    """Share the existing correction harness without duplicating its test cases."""

    _entity = fixtures.LightCorrectionTests._entity
    _report = fixtures.LightCorrectionTests._report
    setup_light = fixtures.LightCorrectionTests.setup_light
    advance = fixtures.LightCorrectionTests.advance
    dispatch = fixtures.LightCorrectionTests.dispatch

    def emit(self, state="contrary", *, intent=None, observed=None, age_ms=None):
        controller = self.entity._light_correction
        data = {
            "event": "lateContraryEffect",
            "deviceSn": "fixture-device",
            "property": "light",
            "intent": controller.intent if intent is None else intent,
            "desiredValue": controller.desired,
            "state": state,
            "recoveryBusy": False,
            "observedValue": (
                observed
                if observed is not None
                else controller.desired
                if state == "matched"
                else not controller.desired
            ),
            "ageMs": (
                max(0, self.now - controller.born) * 1000 if age_ms is None else age_ms
            ),
        }
        self.assertIsNotNone(fixtures.late_contrary_event(data))
        self.entity._handle_property_changed(SimpleNamespace(data=data))

    def setup_grace(self, *, enabled=True, desired=False):
        clock = patch(
            "custom_components.eufy_sdk.light_correction.time",
            lambda: 10000 + self.now + 0.5,
        )
        clock.start()
        self.addCleanup(clock.stop)
        self.sequence = 0
        return self.setup_light(enabled=enabled, desired=desired)

    def observation(self, value, **overrides):
        self.sequence += 1
        data = {
            "event": "propertiesObserved",
            "source": "rtc",
            "deviceSn": "fixture-device",
            "properties": {"light": value},
            "observedAtMs": (10000 + self.now) * 1000 + self.sequence,
            **overrides,
        }
        self.entity._handle_property_changed(SimpleNamespace(data=data))

    def motion(self, *, device="fixture-device", kind="motion"):
        self.entity._handle_property_changed(
            SimpleNamespace(
                data={
                    "event": kind,
                    "deviceSn": device,
                }
            )
        )

    async def arm(self):
        entity, coordinator, client, seen = self.setup_grace()
        await entity.write(False)
        await self.advance(601)
        self.observation(False)
        self.observation(True)
        self.assertEqual(entity._light_correction.state, "uncommanded_grace")
        return entity, coordinator, client, seen

    async def test_startup_on_and_cached_changes_do_not_arm(self):
        entity, _, _, _ = self.setup_grace()
        await entity.write(False)
        await self.advance(601)
        self.observation(True)
        self._report(entity, False)
        self._report(entity, True)
        entity._handle_coordinator_update()
        await self.advance(1000)
        self.assertEqual(len(self.commands), 1)

    async def test_unknown_runtime_intent_does_not_arm(self):
        entity, _, client, _ = self.setup_grace()
        entity._light_correction = LightCorrection(entity)
        self.observation(False)
        self.observation(True)
        await self.advance(1000)
        client.set_property.assert_not_awaited()
        self.assertIsNone(entity._light_correction.ha_intent)

    async def test_live_transition_waits_five_minutes_and_exposes_deadline(self):
        entity, _, client, _ = await self.arm()
        self.assertIsNotNone(
            entity.extra_state_attributes["light_correction_grace_until"]
        )
        await self.advance(900.999)
        self.assertEqual(len(self.commands), 1)
        await self.advance(901)
        self.assertEqual(self.commands[-1][:2], (901, False))
        self.assertTrue(self.commands[-1][2]["bounded"])
        self.assertFalse(self.commands[-1][2]["correction"])
        client.confirm_light.assert_not_awaited()
        await self.advance(946)
        self.assertTrue(self.commands[-1][2]["correction"])
        client.confirm_light.assert_awaited_once()

    async def test_motion_restarts_grace_including_repeated_visual_push(self):
        entity, _, _, _ = await self.arm()
        await self.advance(800)
        self.motion(kind="personDetected")
        self.assertEqual(entity._light_correction.grace_due, 1100)
        await self.advance(1000)
        self.motion(kind="personDetected")
        self.assertEqual(entity._light_correction.grace_due, 1300)
        await self.advance(1299.999)
        self.assertEqual(len(self.commands), 1)
        await self.advance(1300)
        self.assertEqual(self.commands[-1][0], 1300)

    async def test_motion_cannot_extend_beyond_fifteen_minutes(self):
        entity, _, _, _ = await self.arm()
        for moment in (800, 1000, 1200, 1400):
            await self.advance(moment)
            self.motion()
        self.assertEqual(entity._light_correction.grace_due, 1501)
        await self.advance(1500.999)
        self.assertEqual(len(self.commands), 1)
        await self.advance(1501)
        self.assertEqual(self.commands[-1][0], 1501)

    async def test_wrong_camera_and_audio_do_not_extend(self):
        entity, _, _, _ = await self.arm()
        await self.advance(800)
        self.motion(device="other")
        self.motion(kind="soundDetected")
        self.assertEqual(entity._light_correction.grace_due, 901)

    async def test_fresh_self_off_cancels_quietly(self):
        entity, _, _, _ = await self.arm()
        self.observation(False)
        await self.advance(1600)
        self.assertIsNone(entity._light_correction.timer)
        self.assertEqual(len(self.commands), 1)
        self.notification.assert_not_called()

    async def test_new_on_or_off_command_cancels_grace(self):
        for desired in (True, False):
            with self.subTest(desired=desired):
                entity, _, _, _ = await self.arm()
                await self.advance(700)
                await entity.write(desired)
                self.assertIsNone(entity._light_correction.grace_due)
                await self.advance(1000)
                self.assertEqual(len(self.commands), 2)
                self.assertEqual(entity._light_correction.state, "watching")

    async def test_pause_and_disable_cancel_grace_without_resuming_episode(self):
        for gate in ("pause", "option", "capability"):
            with self.subTest(gate=gate):
                entity, coordinator, client, _ = await self.arm()
                if gate == "pause":
                    coordinator.config_entry.options[CONF_LIGHT_CORRECTION_PAUSE] = (
                        "input_boolean.fixture_pause"
                    )
                    self.paused = True
                    entity._light_correction._pause_changed(
                        SimpleNamespace(
                            data={
                                "entity_id": "input_boolean.fixture_pause",
                            }
                        )
                    )
                elif gate == "option":
                    coordinator.config_entry.options[CONF_LIGHT_CORRECTION] = False
                    entity._handle_coordinator_update()
                else:
                    client.late_contrary_effects_supported = False
                    entity._handle_coordinator_update()
                self.assertIsNone(entity._light_correction.timer)
                await self.advance(1600)
                self.assertEqual(len(self.commands), 1)
                self.assertTrue(entity._light_correction.episode_consumed)

    async def test_disabled_and_paused_never_arm(self):
        entity, coordinator, _, _ = self.setup_grace(enabled=False)
        await entity.write(False)
        await self.advance(601)
        self.observation(False)
        self.observation(True)
        self.assertIsNone(entity._light_correction.grace_due)
        coordinator.config_entry.options[CONF_LIGHT_CORRECTION] = True
        coordinator.config_entry.options[CONF_LIGHT_CORRECTION_PAUSE] = (
            "input_boolean.fixture_pause"
        )
        self.paused = True
        self.observation(False)
        self.observation(True)
        self.assertIsNone(entity._light_correction.grace_due)

    async def test_stale_future_duplicate_malformed_and_non_rtc_do_not_arm(self):
        entity, _, _, _ = self.setup_grace()
        await entity.write(False)
        await self.advance(601)
        self.observation(False)
        for data in (
            {"source": "cloud"},
            {"observedAtMs": 1},
            {"observedAtMs": float("inf")},
            {"observedAtMs": float("nan")},
            {"observedAtMs": (10000 + self.now + 2) * 1000},
            {"observedAtMs": entity._light_correction.observation_stamp * 1000},
            {"properties": []},
            {"properties": {"light": 1}},
        ):
            self.observation(True, **data)
        await self.advance(1500)
        self.assertEqual(len(self.commands), 1)

    async def test_reconnect_requires_a_new_off_baseline(self):
        for reconnect in ("bridgeDisconnected", "ready"):
            with self.subTest(reconnect=reconnect):
                entity, _, _, _ = await self.arm()
                entity._handle_property_changed(
                    SimpleNamespace(data={"event": reconnect})
                )
                self.observation(True)
                await self.advance(1600)
                self.assertEqual(len(self.commands), 1)
                self.observation(False)
                self.observation(True)
                self.assertEqual(entity._light_correction.state, "uncommanded_grace")

    async def test_existing_window_uses_original_delay_without_new_grace(self):
        entity, _, _, _ = self.setup_grace()
        await entity.write(False)
        await self.advance(32)
        self.observation(False)
        self.observation(True)
        self.emit()
        self.assertIsNone(entity._light_correction.grace_due)
        await self.advance(47)
        self.assertEqual(self.commands[-1][0], 47)
        self.assertTrue(self.commands[-1][2]["correction"])

    async def test_verified_success_stops_cycle(self):
        entity, _, client, _ = await self.arm()
        await self.advance(901)
        original = client.confirm_light.side_effect

        async def matched(sn, intent):
            return {**await original(sn, intent), "observedValue": False}

        client.confirm_light.side_effect = matched
        await self.advance(1300)
        self.assertEqual(len(self.commands), 2)
        self.assertEqual(entity._light_correction.state, "matched")
        self.notification.assert_not_called()

    async def test_failure_reports_once_and_never_rearms_same_episode(self):
        entity, _, client, _ = await self.arm()
        await self.advance(1700)
        self.assertEqual(len(self.commands), 4)
        self.assertEqual(entity._light_correction.attempts, 3)
        self.assertEqual(entity._light_correction.state, "failed")
        self.assertEqual(client.confirm_light.await_count, 3)
        self.notification.assert_called_once()
        self.assertEqual(entity.hass.bus.async_fire.call_args.args[0], FAILURE_EVENT)
        self.observation(True)
        self.motion()
        await self.advance(2500)
        self.assertEqual(len(self.commands), 4)
        self.observation(False)
        self.observation(True)
        self.assertEqual(entity._light_correction.state, "uncommanded_grace")

    async def test_unload_cancels_grace_and_stale_callback(self):
        entity, _, _, _ = await self.arm()
        callback = self.events[-1][1]
        await entity.async_will_remove_from_hass()
        self.dispatch(callback, None)
        await self.advance(2000)
        self.assertIsNone(entity._light_correction.timer)
        self.assertIsNone(entity._light_correction.task)
        self.assertIsNone(entity._light_correction.pause_unsub)
        self.assertEqual(len(self.commands), 1)

    async def test_new_command_cancels_bootstrap_during_clock_wait(self):
        entity, _, client, _ = await self.arm()
        original = client.set_property.side_effect
        entered, resume = asyncio.Event(), asyncio.Event()
        guards = []

        async def blocked(sn, name, value, **kwargs):
            if "_send_guard" in kwargs:
                guards.append(kwargs["_send_guard"])
                entered.set()
                await resume.wait()
            return await original(sn, name, value, **kwargs)

        client.set_property.side_effect = blocked
        await self.advance(901)
        self.assertTrue(entered.is_set())
        await entity.write(True)
        resume.set()
        await asyncio.sleep(0)
        self.assertFalse(guards[0]())
        self.assertEqual(len(self.commands), 2)

    async def test_spacing_is_preserved_when_grace_admits_a_cycle(self):
        entity, _, _, _ = await self.arm()
        entity._light_correction.last_correction = 890
        await self.advance(934.999)
        self.assertEqual(len(self.commands), 1)
        await self.advance(935)
        self.assertEqual(self.commands[-1][0], 935)

    async def test_bootstrap_error_reports_once_without_restarting(self):
        entity, _, client, _ = await self.arm()
        client.set_property.side_effect = fixtures.EufySdkApiClientCommunicationError(
            "bridge_request_timeout"
        )
        await self.advance(2000)
        self.assertEqual(client.set_property.await_count, 2)
        self.notification.assert_called_once()
        self.assertEqual(entity._light_correction.failure_reason, "protection_lost")

    async def test_cancelled_automatic_cycle_cannot_revive_from_late_event(self):
        entity, coordinator, _, _ = await self.arm()
        await self.advance(901)
        coordinator.config_entry.options[CONF_LIGHT_CORRECTION] = False
        entity._handle_coordinator_update()
        coordinator.config_entry.options[CONF_LIGHT_CORRECTION] = True
        self.emit()
        await self.advance(1600)
        self.assertEqual(len(self.commands), 2)
        self.assertEqual(entity._light_correction.state, "uncommanded_cancelled")

    async def test_unknown_observation_revokes_off_baseline_and_grace(self):
        entity, _, _, _ = self.setup_grace()
        await entity.write(False)
        await self.advance(601)
        self.observation(False)
        self.observation(None)
        self.observation(True)
        self.assertIsNone(entity._light_correction.grace_due)
        self.observation(False)
        self.observation(True)
        self.assertIsNotNone(entity._light_correction.grace_due)
        self.observation(None)
        await self.advance(1600)
        self.assertEqual(len(self.commands), 1)
        self.notification.assert_not_called()

    async def test_old_lineage_events_cannot_take_over_new_grace_after_failure(self):
        entity, _, client, _ = await self.arm()
        await self.advance(1300)
        self.assertTrue(entity._light_correction.failed)
        self.observation(False)
        self.observation(True)
        previous_reads = client.confirm_light.await_count
        self.emit()
        self.emit("matched")
        self.assertEqual(entity._light_correction.state, "uncommanded_grace")
        self.assertEqual(entity._light_correction.grace_due, 1600)
        await self.advance(1599)
        self.assertEqual(client.confirm_light.await_count, previous_reads)
        self.assertEqual(len(self.commands), 4)
        await self.advance(1600)
        self.assertEqual(len(self.commands), 5)
        self.assertFalse(entity._light_correction.failed)

    async def test_self_off_after_bootstrap_ends_cycle_and_next_on_gets_new_grace(self):
        entity, _, _, _ = await self.arm()
        await self.advance(901)
        self.observation(False)
        self.emit("matched")
        await self.advance(930)
        self.observation(True)
        self.emit()
        self.assertEqual(entity._light_correction.grace_due, 1230)
        await self.advance(1229)
        self.assertEqual(len(self.commands), 2)
        await self.advance(1230)
        self.assertEqual(len(self.commands), 3)


class WindowMotionGraceTests(unittest.IsolatedAsyncioTestCase):
    """Protect motion lighting without extending an explicit command lineage."""

    _entity = LightGraceTests._entity
    _report = LightGraceTests._report
    setup_light = LightGraceTests.setup_light
    setup_grace = LightGraceTests.setup_grace
    advance = LightGraceTests.advance
    dispatch = LightGraceTests.dispatch
    observation = LightGraceTests.observation
    motion = LightGraceTests.motion
    emit = LightGraceTests.emit

    async def window(self, *, motion_at=70, on_at=100, desired=False):
        entity, coordinator, client, seen = self.setup_grace(desired=desired)
        await entity.write(desired)
        if motion_at is not None:
            await self.advance(motion_at)
            self.motion()
        await self.advance(on_at)
        self.emit()
        return entity, coordinator, client, seen

    async def test_motion_thirty_seconds_before_on_skips_first_window_write(self):
        entity, _, client, _ = await self.window()
        receipt = entity._command_receipt
        controller = entity._light_correction
        self.assertEqual(controller.state, "uncommanded_grace")
        self.assertEqual(controller.grace_due, 400)
        self.assertTrue(controller.episode_consumed)
        self.assertIs(controller.ha_intent, False)
        self.assertEqual(controller.intent, 1)
        self.assertIsNotNone(
            entity.extra_state_attributes["light_correction_grace_until"]
        )
        await self.advance(115)
        self.assertEqual(len(self.commands), 1)
        client.confirm_light.assert_not_awaited()
        await self.advance(400)
        self.assertFalse(self.commands[-1][2]["correction"])
        self.assertIs(entity._command_receipt, receipt)

    async def test_motion_between_on_and_first_write_hands_off(self):
        entity, _, client, _ = await self.window(motion_at=None)
        stale_callback = self.events[-1][1]
        await self.advance(110)
        self.motion(kind="personDetected")
        self.dispatch(stale_callback, None)
        self.assertEqual(entity._light_correction.grace_started, 100)
        self.assertEqual(entity._light_correction.grace_due, 410)
        await self.advance(409.999)
        self.assertEqual(len(self.commands), 1)
        client.confirm_light.assert_not_awaited()
        await self.advance(410)
        self.assertEqual(self.commands[-1][0], 410)

    async def test_motion_ninety_seconds_before_on_keeps_normal_schedule(self):
        entity, _, _, _ = await self.window(motion_at=10)
        self.assertIsNone(entity._light_correction.grace_due)
        await self.advance(114.999)
        self.assertEqual(len(self.commands), 1)
        await self.advance(115)
        self.assertEqual(self.commands[-1][0], 115)
        self.assertTrue(self.commands[-1][2]["correction"])

    async def test_no_motion_preserves_all_three_offsets(self):
        entity, _, client, _ = await self.window(motion_at=None)
        await self.advance(114.999)
        self.assertEqual(len(self.commands), 1)
        await self.advance(400)
        self.assertEqual([entry[0] for entry in self.commands], [0, 115, 160, 400])
        self.assertIsNone(entity._light_correction.grace_due)
        self.assertEqual(client.confirm_light.await_count, 3)

    async def test_sixty_second_lookback_boundary(self):
        for motion_at, expected in ((40, True), (39.999, False)):
            with self.subTest(motion_at=motion_at):
                entity, _, _, _ = await self.window(motion_at=motion_at)
                self.assertEqual(
                    entity._light_correction.grace_due is not None, expected
                )

    async def test_motion_before_first_command_is_retained(self):
        entity, _, _, _ = self.setup_grace()
        self.motion(kind="vehicleDetected")
        self.assertEqual(entity._light_correction.last_motion, 0)
        await self.advance(10)
        await entity.write(False)
        await self.advance(30)
        self.emit()
        self.assertEqual(entity._light_correction.grace_due, 330)

    async def test_same_device_visual_membership_only(self):
        for kind in (
            "motion",
            "personDetected",
            "vehicleDetected",
            "petDetection",
            "dogDetected",
            "strangerDetected",
        ):
            with self.subTest(kind=kind):
                entity, _, _, _ = await self.window(motion_at=None)
                await self.advance(110)
                self.motion(kind=kind)
                self.assertEqual(entity._light_correction.grace_due, 410)
        entity, _, _, _ = await self.window(motion_at=None)
        await self.advance(110)
        self.motion(device="other")
        self.motion(kind="soundDetected")
        self.motion(kind="packageDelivered")
        self.assertIsNone(entity._light_correction.last_motion)
        await self.advance(115)
        self.assertEqual(len(self.commands), 2)

    async def test_restarts_and_cap_stay_rooted_at_contrary_on(self):
        entity, _, _, _ = await self.window()
        for moment in (300, 500, 700, 900):
            await self.advance(moment)
            self.motion()
            if moment < WINDOW_SECONDS:
                self.emit()
        controller = entity._light_correction
        self.assertEqual(controller.grace_started, 100)
        self.assertEqual(controller.grace_due, 1000)
        await self.advance(999.999)
        self.assertEqual(len(self.commands), 1)
        await self.advance(1000)
        self.assertEqual(self.commands[-1][0], 1000)
        self.assertFalse(self.commands[-1][2]["correction"])

    async def test_old_window_expiry_cannot_end_grace_or_revive_lineage(self):
        entity, _, client, _ = self.setup_grace()
        await entity.write(False)
        controller = entity._light_correction
        old_expiry = self.events[0][1]
        old_intent = controller.intent
        await self.advance(560)
        self.motion()
        await self.advance(590)
        self.emit()
        await self.advance(600)
        self.dispatch(old_expiry, None)
        self.emit("expired", intent=old_intent)
        self.emit("matched", intent=old_intent)
        self.emit("closed", intent=old_intent)
        self.assertEqual(controller.state, "uncommanded_grace")
        self.assertEqual(controller.grace_due, 890)
        await self.advance(889.999)
        self.assertEqual(len(self.commands), 1)
        await self.advance(890)
        self.assertEqual(len(self.commands), 2)
        self.assertFalse(self.commands[-1][2]["correction"])
        self.assertNotEqual(controller.intent, old_intent)
        self.assertEqual(controller.born, 890)
        client.confirm_light.assert_not_awaited()
        await self.advance(935)
        self.assertTrue(self.commands[-1][2]["correction"])
        self.notification.assert_not_called()

    async def test_fresh_off_during_grace_cancels_quietly(self):
        entity, _, _, _ = await self.window()
        await self.advance(110)
        self.observation(False)
        self.emit()
        await self.advance(1000)
        self.assertIsNone(entity._light_correction.grace_due)
        self.assertFalse(entity._light_correction.episode_consumed)
        self.assertEqual(len(self.commands), 1)
        self.notification.assert_not_called()

    async def test_new_ha_command_takes_ownership_during_grace(self):
        for desired in (True, False):
            with self.subTest(desired=desired):
                entity, _, _, _ = await self.window()
                old_intent = 1
                await self.advance(110)
                await entity.write(desired)
                self.emit(intent=old_intent)
                await self.advance(410)
                self.assertEqual(len(self.commands), 2)
                self.assertIsNone(entity._light_correction.grace_due)
                self.assertIs(entity._light_correction.ha_intent, desired)

    async def test_pause_disable_and_capability_loss_cancel_grace(self):
        for gate in ("pause", "option", "capability"):
            with self.subTest(gate=gate):
                entity, coordinator, client, _ = await self.window()
                if gate == "pause":
                    coordinator.config_entry.options[CONF_LIGHT_CORRECTION_PAUSE] = (
                        "input_boolean.fixture_pause"
                    )
                    self.paused = True
                    entity._light_correction._pause_changed(
                        SimpleNamespace(
                            data={
                                "entity_id": "input_boolean.fixture_pause",
                            }
                        )
                    )
                elif gate == "option":
                    coordinator.config_entry.options[CONF_LIGHT_CORRECTION] = False
                    entity._handle_coordinator_update()
                else:
                    client.late_contrary_effects_supported = False
                    entity._handle_coordinator_update()
                await self.advance(1000)
                self.assertIsNone(entity._light_correction.grace_due)
                self.assertEqual(len(self.commands), 1)
                self.notification.assert_not_called()

    async def test_reconnect_and_removal_cancel_grace_without_failure(self):
        for event in ("bridgeDisconnected", "ready", "removal"):
            with self.subTest(event=event):
                entity, _, _, _ = await self.window()
                stale_callback = self.events[-1][1]
                if event == "removal":
                    await entity.async_will_remove_from_hass()
                else:
                    entity._handle_property_changed(
                        SimpleNamespace(
                            data={
                                "event": event,
                            }
                        )
                    )
                self.dispatch(stale_callback, None)
                await self.advance(1000)
                self.assertIsNone(entity._light_correction.grace_due)
                self.assertIsNone(entity._light_correction.last_motion)
                self.assertEqual(len(self.commands), 1)
                self.notification.assert_not_called()

    async def test_desired_on_with_motion_remains_observation_only(self):
        entity, _, _, _ = await self.window(desired=True)
        await self.advance(110)
        self.motion()
        await self.advance(115)
        self.assertIsNone(entity._light_correction.grace_due)
        self.assertEqual(self.commands[-1][:2], (0, True))
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(
            entity.extra_state_attributes["light_correction_suppression_reason"],
            "on_observation_only",
        )
        self.notification.assert_not_called()

    async def test_motion_after_completed_write_keeps_cycle_and_limits(self):
        entity, _, _, _ = await self.window(motion_at=None)
        await self.advance(115)
        await self.advance(116)
        self.motion()
        self.emit()
        self.assertIsNone(entity._light_correction.grace_due)
        await self.advance(500)
        self.assertEqual([entry[0] for entry in self.commands], [0, 115, 160, 400])
        self.assertEqual(entity._light_correction.attempts, 3)
        self.notification.assert_called_once()

    async def test_motion_during_confirmation_cancels_unsent_task(self):
        entity, _, client, _ = await self.window(motion_at=None)
        entered = asyncio.Event()
        release = asyncio.Event()
        previous = client.confirm_light.side_effect

        async def blocked(*args):
            entered.set()
            await release.wait()
            return await previous(*args)

        client.confirm_light.side_effect = blocked
        await self.advance(115)
        self.assertTrue(entered.is_set())
        self.motion()
        release.set()
        await asyncio.sleep(0)
        self.assertEqual(entity._light_correction.grace_due, 415)
        self.assertEqual(entity._light_correction.attempts, 0)
        self.assertEqual(len(self.commands), 1)

    async def test_motion_during_clock_wait_cancels_before_dispatch(self):
        entity, _, client, _ = await self.window(motion_at=None)
        entered, release = asyncio.Event(), asyncio.Event()
        guards = []
        previous = client.set_property.side_effect

        async def blocked(*args, **kwargs):
            guards.append(kwargs["_send_guard"])
            entered.set()
            await release.wait()
            return await previous(*args, **kwargs)

        client.set_property.side_effect = blocked
        await self.advance(115)
        self.assertTrue(entered.is_set())
        self.motion()
        release.set()
        await asyncio.sleep(0)
        self.assertFalse(guards[0]())
        self.assertEqual(entity._light_correction.grace_due, 415)
        self.assertEqual(entity._light_correction.attempts, 0)
        self.assertEqual(len(self.commands), 1)

    async def test_pending_dispatch_requires_an_unsent_receipt_before_grace(self):
        for sent in (False, True):
            with self.subTest(sent=sent):
                entity, _, client, _ = await self.window(motion_at=None)
                entered, release = asyncio.Event(), asyncio.Event()

                async def queued(
                    *_, sent=sent, entered=entered, release=release, **kwargs
                ):
                    self.assertTrue(kwargs["_send_guard"]())
                    entered.set()
                    await release.wait()
                    return fixtures.CommandReceipt(
                        "cancelled_after_send" if sent else "cancelled_before_send",
                        sent,
                        "unknown",
                        intent=1,
                    )

                client.set_property.side_effect = queued
                await self.advance(115)
                self.assertTrue(entered.is_set())
                await self.advance(116)
                self.motion()
                self.assertIsNone(entity._light_correction.grace_due)
                client.pause_light_correction.assert_called_once()
                release.set()
                for _ in range(5):
                    await asyncio.sleep(0)
                controller = entity._light_correction
                self.assertEqual(controller.grace_due, None if sent else 416)
                self.assertEqual(controller.attempts, int(sent))
                self.assertEqual(controller.write_sent, sent)

    async def test_lost_reply_after_dispatch_never_assumes_an_unsent_write(self):
        entity, _, client, _ = await self.window(motion_at=None)

        async def lost(*_, **kwargs):
            self.assertTrue(kwargs["_send_guard"]())
            message = "bridge_request_timeout"
            raise fixtures.EufySdkApiClientCommunicationError(message)

        client.set_property.side_effect = lost
        await self.advance(115)
        self.motion()
        self.assertTrue(entity._light_correction.write_sent)
        self.assertIsNone(entity._light_correction.grace_due)

    async def test_real_rpc_cancellation_hands_off_only_with_unsent_receipt(self):
        for sent in (False, True):
            with self.subTest(sent=sent):
                entity, _, client, _ = await self.window(motion_at=None)
                protocol = fixtures.LightCorrectionProtocolTests.client(self)
                protocol._set_late_contrary_capability(fixtures.CAPABILITY)
                previous = protocol._ws.send_json.side_effect
                submitted = asyncio.Event()
                request_id = None

                async def reply(
                    payload,
                    sent=sent,
                    previous=previous,
                    protocol=protocol,
                    submitted=submitted,
                ):
                    nonlocal request_id
                    if payload["cmd"] == "boundedStateClock":
                        await previous(payload)
                    elif payload["cmd"] == "device.set":
                        self.frames.append(payload)
                        request_id = payload["id"]
                        submitted.set()
                    elif payload["cmd"] == "boundedStateCancel":
                        self.frames.append(payload)
                        self.assertIs(payload["keepObservation"], True)
                        protocol._pending[request_id].set_result(
                            {
                                "ok": True,
                                "receipt": {
                                    "status": (
                                        "cancelled_after_send"
                                        if sent
                                        else "cancelled_before_send"
                                    ),
                                    "sendAttempted": sent,
                                    "effect": "unknown",
                                    "intent": 1,
                                },
                            }
                        )

                async def send(*args, protocol=protocol, entity=entity, **kwargs):
                    # Convert only the fake monotonic clock to the loop clock;
                    # the wire wall deadline and real RPC guards stay intact.
                    kwargs["correction_expires_at"] = (
                        asyncio.get_running_loop().time()
                        + entity._light_correction.born
                        + WINDOW_SECONDS
                        - self.now
                    )
                    return await protocol.set_property(*args, **kwargs)

                protocol._ws.send_json.side_effect = reply
                client.set_property.side_effect = send
                client.pause_light_correction.side_effect = (
                    protocol.pause_light_correction
                )
                await self.advance(115)
                self.assertTrue(submitted.is_set())
                await self.advance(116)
                self.motion()
                for _ in range(10):
                    await asyncio.sleep(0)
                self.assertEqual(
                    entity._light_correction.grace_due, None if sent else 416
                )
                self.assertEqual(entity._light_correction.attempts, int(sent))
                self.assertEqual(protocol._correction_requests, {})
                self.assertEqual(protocol._pending, {})
                self.assertEqual(
                    [frame["cmd"] for frame in self.frames],
                    ["boundedStateClock", "device.set", "boundedStateCancel"],
                )

    async def test_confirmation_only_contrary_observation_can_hand_off(self):
        entity, _, _, _ = self.setup_grace()
        await entity.write(False)
        await self.advance(60)
        self.motion()
        await self.advance(80)
        self.emit("suspended")
        await self.advance(100)
        self.assertEqual(entity._light_correction.grace_due, 400)
        self.assertEqual(len(self.commands), 1)

    async def test_unsent_receipt_replays_replacement_match_before_handoff(self):
        entity, _, client, _ = await self.window(motion_at=None)
        release = asyncio.Event()

        async def queued(*_, **kwargs):
            self.assertTrue(kwargs["_send_guard"]())
            await release.wait()
            return fixtures.CommandReceipt(
                "cancelled_before_send", False, "unknown", intent=2
            )

        client.set_property.side_effect = queued
        await self.advance(115)
        await self.advance(116)
        self.motion()
        self.emit("matched", intent=2)
        release.set()
        for _ in range(5):
            await asyncio.sleep(0)
        self.assertEqual(entity._light_correction.state, "matched")
        self.assertEqual(entity._light_correction.attempts, 0)
        self.assertIsNone(entity._light_correction.grace_due)
        self.notification.assert_not_called()

    async def test_window_expiry_preserves_only_pending_receipt_resolution(self):
        for outcome in ("unsent", "sent", "lost", "suspended", "exhausted"):
            with self.subTest(outcome=outcome):
                entity, _, client, _ = await self.window(motion_at=None, on_at=580)
                release = asyncio.Event()
                previous = client.set_property.side_effect
                guards = []

                async def queued(
                    *_, release=release, guards=guards, outcome=outcome, **kwargs
                ):
                    guards.append(kwargs["_send_guard"])
                    self.assertTrue(guards[-1]())
                    await release.wait()
                    if outcome == "lost":
                        message = "bridge_request_timeout"
                        raise fixtures.EufySdkApiClientCommunicationError(message)
                    return fixtures.CommandReceipt(
                        "cancelled_after_send"
                        if outcome == "sent"
                        else "cancelled_before_send",
                        outcome == "sent",
                        "unknown",
                        intent=2,
                    )

                client.set_property.side_effect = queued
                await self.advance(595)
                await self.advance(596)
                self.motion()
                controller = entity._light_correction
                task = controller.task
                await self.advance(601)
                self.assertTrue(controller.motion_expiry_pending)
                self.assertFalse(controller._live())
                self.assertFalse(guards[0]())
                self.assertFalse(task.done())
                self.notification.assert_not_called()
                if outcome == "suspended":
                    self.emit("suspended", intent=2, age_ms=6000)
                elif outcome == "exhausted":
                    self.emit("verification_exhausted", intent=2, age_ms=6000)
                await self.advance(610)
                release.set()
                for _ in range(5):
                    await asyncio.sleep(0)
                self.assertTrue(task.done())
                self.assertFalse(controller.motion_expiry_pending)
                if outcome == "unsent":
                    self.assertEqual(controller.grace_due, 896)
                    self.assertEqual(controller.grace_started, 580)
                    self.assertEqual(controller.attempts, 0)
                    self.notification.assert_not_called()
                    client.set_property.side_effect = previous
                    await self.advance(896)
                    self.assertEqual(self.commands[-1][0], 896)
                    self.assertFalse(self.commands[-1][2]["correction"])
                else:
                    self.assertIsNone(controller.grace_due)
                    self.assertEqual(
                        controller.failure_reason,
                        "read_budget_exhausted"
                        if outcome == "exhausted"
                        else "window_expired",
                    )
                    self.notification.assert_called_once()

    async def test_suspended_evidence_requires_a_new_confirmed_on(self):
        for state in ("suspended", "detection_exhausted"):
            with self.subTest(state=state):
                entity, _, _, _ = await self.window(motion_at=None)
                await self.advance(105)
                self.emit(state)
                await self.advance(110)
                self.motion()
                self.assertIsNone(entity._light_correction.grace_due)
                await self.advance(115)
                self.assertEqual(len(self.commands), 1)
                await self.advance(120)
                self.emit()
                self.assertEqual(entity._light_correction.grace_due, 420)
                self.assertEqual(entity._light_correction.grace_started, 120)

    async def test_old_intent_suspension_during_dispatch_invalidates_candidate(self):
        for state in ("suspended", "detection_exhausted"):
            with self.subTest(state=state):
                entity, _, client, _ = await self.window(motion_at=None)
                release = asyncio.Event()

                async def queued(*_, release=release, **kwargs):
                    self.assertTrue(kwargs["_send_guard"]())
                    await release.wait()
                    return fixtures.CommandReceipt(
                        "cancelled_before_send", False, "unknown", intent=2
                    )

                client.set_property.side_effect = queued
                await self.advance(115)
                await self.advance(116)
                self.motion()
                self.emit(state, intent=1)
                release.set()
                for _ in range(5):
                    await asyncio.sleep(0)
                self.assertIsNone(entity._light_correction.window_on_since)
                self.assertIsNone(entity._light_correction.grace_due)
                self.notification.assert_not_called()

    async def test_early_event_preserves_on_and_motion_receipt_times(self):
        for final in ("contrary", "matched", "fresh_off"):
            with self.subTest(final=final):
                entity, _, client, _ = self.setup_grace()
                release = asyncio.Event()
                previous = client.set_property.side_effect

                async def delayed(*args, previous=previous, release=release, **kwargs):
                    receipt = await previous(*args, **kwargs)
                    await release.wait()
                    return receipt

                client.set_property.side_effect = delayed
                write = asyncio.create_task(entity.write(False))
                for _ in range(5):
                    await asyncio.sleep(0)
                await self.advance(100)
                self.emit(intent=1)
                await self.advance(110)
                self.motion()
                if final == "matched":
                    self.emit("matched", intent=1)
                elif final == "fresh_off":
                    self.observation(False)
                await self.advance(120)
                release.set()
                await write
                controller = entity._light_correction
                self.assertEqual(
                    controller.grace_due, 410 if final == "contrary" else None
                )
                if final == "contrary":
                    self.assertEqual(controller.grace_started, 100)


class GraceProtocolTests(unittest.IsolatedAsyncioTestCase):
    """Use actual bounded RPC plumbing with an in-memory transport."""

    client = fixtures.LightCorrectionProtocolTests.client

    async def test_bootstrap_guard_blocks_dispatch_after_clock_exchange(self):
        client = self.client()
        client._set_late_contrary_capability(fixtures.CAPABILITY)
        previous = client._ws.send_json.side_effect
        allowed = True

        async def clock_reply(payload):
            nonlocal allowed
            await previous(payload)
            allowed = False

        client._ws.send_json.side_effect = clock_reply
        with self.assertRaisesRegex(
            fixtures.EufySdkApiClientCommunicationError,
            "^light_correction_paused_before_send$",
        ):
            await client.set_property(
                "fixture",
                "light",
                False,
                bounded=True,
                _send_guard=lambda: allowed,
            )
        self.assertEqual([frame["cmd"] for frame in self.frames], ["boundedStateClock"])
        self.assertEqual(client._correction_requests, {})

    async def test_bootstrap_cancel_uses_existing_bounded_cancel_protocol(self):
        client = self.client()
        client._set_late_contrary_capability(fixtures.CAPABILITY)
        previous = client._ws.send_json.side_effect
        submitted = asyncio.Event()
        request_id = None

        async def reply(payload):
            nonlocal request_id
            if payload["cmd"] == "boundedStateClock":
                await previous(payload)
            elif payload["cmd"] == "device.set":
                self.frames.append(payload)
                self.assertNotIn("correction", payload["execution"])
                request_id = payload["id"]
                submitted.set()
            elif payload["cmd"] == "boundedStateCancel":
                self.frames.append(payload)
                self.assertNotIn("keepObservation", payload)
                client._pending[request_id].set_result(
                    {
                        "ok": True,
                        "receipt": {
                            "status": "cancelled_before_send",
                            "sendAttempted": False,
                            "effect": "unconfirmed",
                            "intent": 2,
                        },
                    }
                )

        client._ws.send_json.side_effect = reply
        task = asyncio.create_task(
            client.set_property(
                "fixture",
                "light",
                False,
                bounded=True,
                _send_guard=lambda: True,
            )
        )
        await submitted.wait()
        client.pause_light_correction("fixture")
        receipt = await task
        self.assertEqual(receipt.status, "cancelled_before_send")
        self.assertEqual(client._correction_requests, {})
        self.assertEqual(
            len([f for f in self.frames if f["cmd"] == "boundedStateCancel"]), 1
        )
