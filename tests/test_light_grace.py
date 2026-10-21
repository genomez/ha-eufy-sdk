"""Exercise uncommanded light episodes with fake time and local observations."""

# ruff: noqa: ANN001, ANN003, ANN201, ANN202, D102, FBT003, INP001, PT009, PT027, SLF001
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import test_light_correction as fixtures

from custom_components.eufy_sdk.const import (
    CONF_LIGHT_CORRECTION,
    CONF_LIGHT_CORRECTION_PAUSE,
)
from custom_components.eufy_sdk.light_correction import FAILURE_EVENT, LightCorrection


class LightGraceTests(unittest.IsolatedAsyncioTestCase):
    """Share the existing correction harness without duplicating its test cases."""

    _entity = fixtures.LightCorrectionTests._entity
    _report = fixtures.LightCorrectionTests._report
    setup_light = fixtures.LightCorrectionTests.setup_light
    advance = fixtures.LightCorrectionTests.advance
    dispatch = fixtures.LightCorrectionTests.dispatch

    def emit(self, state="contrary", *, intent=None, observed=None):
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
            "ageMs": max(0, self.now - controller.born) * 1000,
        }
        self.assertIsNotNone(fixtures.late_contrary_event(data))
        self.entity._handle_property_changed(SimpleNamespace(data=data))

    def setup_grace(self, *, enabled=True):
        clock = patch(
            "custom_components.eufy_sdk.light_correction.time",
            lambda: 10000 + self.now + 0.5,
        )
        clock.start()
        self.addCleanup(clock.stop)
        self.sequence = 0
        return self.setup_light(enabled=enabled)

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
