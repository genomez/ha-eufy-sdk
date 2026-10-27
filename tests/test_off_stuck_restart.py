"""Keep restart noise separate from real unresolved OFF evidence."""

# ruff: noqa: ANN001, ANN201, D102, FBT003, INP001, PT009, SLF001

import unittest
from copy import deepcopy
from types import SimpleNamespace

import test_floodlight_safety as fixtures
import test_off_stuck_escalation as incidents
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState

from custom_components.eufy_sdk.light_correction import LightCorrection


class RestartQuietTests(unittest.IsolatedAsyncioTestCase):
    """Exercise lifecycle boundaries without transport or storage I/O."""

    _entity = fixtures.FloodlightSafetyTests._entity
    _report = fixtures.FloodlightSafetyTests._report
    asyncSetUp = fixtures.FloodlightSafetyTests.asyncSetUp
    asyncTearDown = fixtures.FloodlightSafetyTests.asyncTearDown
    make = fixtures.FloodlightSafetyTests.make
    drain = fixtures.FloodlightSafetyTests.drain
    at = fixtures.FloodlightSafetyTests.at
    report = fixtures.FloodlightSafetyTests.report
    incidents = incidents.OffStuckEscalationTests.incidents

    async def restore(self, entity, controller, *, state=CoreState.running):
        controller.stop("closed")
        entity.hass.state = state
        restored = LightCorrection(entity)
        entity._light_correction = restored
        self.controllers.append(restored)
        await restored.async_added()
        return restored

    async def test_off_disconnect_and_ready_keep_evidence_without_incidents(self):
        entity, controller, client = await self.make()
        self.now = 2000
        self.report(controller, False)
        stamp = controller.observed_at
        self.now += 24
        client.connected = False
        entity._handle_property_changed(
            SimpleNamespace(data={"event": "bridgeDisconnected"})
        )
        self.assertIs(controller.observed, False)
        self.assertEqual(controller.observed_at, stamp)
        await self.at(self.now + 300)
        self.assertEqual(self.incidents(entity), [])
        client.connected = True
        entity._handle_property_changed(SimpleNamespace(data={"event": "ready"}))
        await self.at(self.now + 300)
        self.assertEqual(self.incidents(entity), [])

    async def test_off_observation_survives_reload_and_restart(self):
        entity, controller, _ = await self.make()
        self.report(controller, False)
        stamp = controller.observed_at
        self.now = 5000
        for _ in range(2):
            controller = await self.restore(entity, controller)
            self.assertIs(controller.observed, False)
            self.assertEqual(controller.observed_at, stamp)
            await self.at(self.now + 301)
            self.assertEqual(self.incidents(entity), [])

    async def test_stored_on_is_evidence_but_cannot_authorize_watchdog(self):
        entity, controller, _ = await self.make()
        self.report(controller, True)
        self.now = 60
        controller = await self.restore(entity, controller)
        self.assertIs(controller.observed, True)
        self.assertFalse(controller._fresh())
        self.assertIsNone(controller.on_since)
        await self.at(359)
        self.assertEqual(self.incidents(entity), [])
        await self.at(360)
        self.assertEqual(self.incidents(entity)[0]["reason"], "no_reports")
        self.assertEqual(controller.off_stuck["escalate_at"], self.wall + 60 + 1800)
        self.assertEqual(self.sent, [])

    async def test_reconnect_unknown_and_on_get_full_silence_interval(self):
        for index, value in enumerate((None, True)):
            with self.subTest(value=value):
                entity, controller, client = await self.make(index=index)
                self.now += 2000
                if value is True:
                    self.report(controller, value)
                client.connected = False
                controller.reset_observations()
                start = self.now
                self.now = start + 24
                client.connected = True
                entity._handle_property_changed(
                    SimpleNamespace(data={"event": "ready"})
                )
                start = self.now
                self.now = start + 299
                controller.check_policy()
                self.assertEqual(self.incidents(entity), [])
                self.now = start + 300
                controller.check_policy()
                self.assertEqual(self.incidents(entity)[0]["reason"], "no_reports")
                self.assertEqual(
                    controller.off_stuck["escalate_at"], int(self.wall + start) + 1800
                )
                controller.stop("closed")

    async def test_startup_downtime_is_not_silence(self):
        entity, controller, client = await self.make()
        controller = await self.restore(entity, controller, state=CoreState.starting)
        client.connected = True
        await self.at(5000)
        self.assertIsNone(controller.silence_since)
        self.assertEqual(self.incidents(entity), [])
        entity.hass.state = CoreState.running
        controller._ha_started(None)
        await self.at(5299)
        self.assertEqual(self.incidents(entity), [])
        await self.at(5300)
        self.assertEqual(self.incidents(entity)[0]["reason"], "no_reports")
        self.assertEqual(controller.off_stuck["escalate_at"], self.wall + 5000 + 1800)

    async def test_unknown_episode_uses_last_qualifying_report_for_silence(self):
        entity, controller, _ = await self.make()
        self.now = 100
        self.report(controller, True)
        controller.on_reported_at = None
        onset = controller.report_received
        await self.at(onset + 300)
        self.assertEqual(self.incidents(entity)[0]["reason"], "no_reports")
        self.assertEqual(
            controller.off_stuck["escalate_at"], int(self.wall + onset) + 1800
        )

    async def test_unknown_blocked_episode_uses_waiting_onset(self):
        entity, controller, client = await self.make()
        client.connected = False
        self.now = 100
        controller = await self.restore(entity, controller)
        await self.at(399)
        self.assertEqual(self.incidents(entity), [])
        await self.at(400)
        self.assertEqual(controller.off_stuck["reason"], "blocked")
        self.assertEqual(controller.off_stuck["escalate_at"], self.wall + 100 + 1800)

    async def test_unknown_episode_keeps_two_minute_floor_for_late_checks(self):
        entity, _, _ = await self.make()
        await self.at(2000)
        incident = self.incidents(entity)[0]
        self.assertEqual(incident["escalate_at"], incident["opened_at"] + 120)

    async def test_restored_deadline_is_not_rebased_to_restart_or_reconnect(self):
        entity, controller, client = await self.make()
        await self.at(300)
        controller.off_stuck["escalate_at"] = int(self.wall) + 900
        controller._save()
        saved = deepcopy(controller.off_stuck)
        self.now = 5000
        controller = await self.restore(entity, controller, state=CoreState.starting)
        entity.hass.state = CoreState.running
        controller._ha_started(None)
        client.connected = False
        controller.reset_observations()
        self.now = 5100
        client.connected = True
        controller.reset_observations()
        await self.at(5400)
        self.assertEqual(controller.off_stuck, saved)
        self.assertEqual(self.incidents(entity)[1]["escalate_at"], saved["escalate_at"])

    async def test_no_reports_clock_requires_a_connected_runtime(self):
        entity, controller, client = await self.make()
        controller = await self.restore(entity, controller, state=CoreState.starting)
        client.connected = False
        entity.hass.state = CoreState.running
        controller._ha_started(None)
        self.now = 5000
        controller._check_off_stuck(self.now, False, False, 0)
        self.assertIsNone(controller.silence_since)
        self.assertEqual(controller.off_stuck["reason"], "blocked")
        client.connected = True
        controller.check_policy()
        self.assertEqual(controller.silence_since, 5000)

    async def test_camera_offline_and_parent_outage_still_open_after_five_minutes(self):
        for index, health in enumerate(
            ({"connectivity": "offline"}, {"parentTransport": "disconnected"})
        ):
            entity, controller, _ = await self.make(index=index)
            self.report(controller, True)
            entity.device["cameraHealth"] = health
            controller.check_policy()
            start = self.now
            self.now = start + 299
            controller.check_policy()
            self.assertEqual(self.incidents(entity), [])
            self.now = start + 300
            controller.check_policy()
            self.assertIsNotNone(controller.off_stuck)
            controller.stop("closed")

    async def test_confirmed_off_outage_remains_outside_off_stuck(self):
        entity, controller, _ = await self.make()
        self.report(controller, False)
        entity.device["cameraHealth"] = {"connectivity": "offline"}
        await self.at(400)
        self.assertEqual(self.incidents(entity), [])
        self.assertIn("camera_gap", controller.notice_kinds)

    async def test_pending_off_with_last_off_still_gets_no_confirmation(self):
        entity, controller, client = await self.make()
        self.report(controller, False)
        await entity.write(False)
        client.connected = False
        controller.reset_observations()
        start = self.now
        await self.at(start + 299)
        self.assertEqual(self.incidents(entity), [])
        await self.at(start + 300)
        self.assertEqual(self.incidents(entity)[0]["reason"], "no_confirmation")

    async def test_stopping_and_final_write_cannot_open_or_save(self):
        entity, controller, _ = await self.make()
        baseline = deepcopy(fixtures.MemoryStore.rows)
        for state in (CoreState.stopping, CoreState.final_write):
            entity.hass.state = state
            self.now = 5000
            controller.reset_observations()
            controller._check_off_stuck(self.now, False, False, 0)
            controller._save()
            self.report(controller, True)
            self.assertEqual(self.incidents(entity), [])
            self.assertEqual(fixtures.MemoryStore.rows, baseline)
        self.assertEqual(self.sent, [])

    async def test_unloaded_controller_cannot_open_or_save(self):
        entity, controller, _ = await self.make()
        baseline = deepcopy(fixtures.MemoryStore.rows)
        controller.stop("closed")
        await self.at(5000)
        controller._save()
        self.assertEqual(self.incidents(entity), [])
        self.assertEqual(fixtures.MemoryStore.rows, baseline)

    async def test_restore_replay_waits_for_started_and_fires_once(self):
        entity, controller, _ = await self.make()
        await self.at(300)
        identity = controller.off_stuck["incident_id"]
        controller = await self.restore(entity, controller, state=CoreState.starting)
        listener = entity.hass.bus.async_listen_once.call_args
        self.assertEqual(listener.args[0], EVENT_HOMEASSISTANT_STARTED)
        await self.at(5000)
        self.assertEqual(len(self.incidents(entity)), 1)
        entity.hass.state = CoreState.running
        listener.args[1](None)
        controller.check_policy()
        self.assertEqual(len(self.incidents(entity)), 2)
        self.assertEqual(self.incidents(entity)[1]["incident_id"], identity)

    async def test_restored_confirmation_closes_after_started(self):
        entity, controller, _ = await self.make()
        await self.at(300)
        identity = controller.off_stuck["incident_id"]
        controller = await self.restore(entity, controller, state=CoreState.starting)
        self.now = 400
        self.report(controller, False)
        self.assertEqual(self.incidents(entity, "off_stuck_closed"), [])
        self.assertIsNotNone(controller.off_stuck)
        entity.hass.state = CoreState.running
        controller._ha_started(None)
        controller.check_policy()
        self.assertEqual(len(self.incidents(entity)), 1)
        (closed,) = self.incidents(entity, "off_stuck_closed")
        self.assertEqual(closed["incident_id"], identity)
        self.assertEqual(closed["outcome"], "confirmed_off")

    async def test_restore_intent_close_waits_for_started(self):
        entity, controller, _ = await self.make(binding="sensor.intent")
        self.states["sensor.intent"] = "off"
        await self.at(300)
        controller = await self.restore(entity, controller, state=CoreState.starting)
        self.states["sensor.intent"] = "on"
        controller.check_policy()
        self.assertEqual(self.incidents(entity, "off_stuck_closed"), [])
        entity.hass.state = CoreState.running
        controller._ha_started(None)
        (closed,) = self.incidents(entity, "off_stuck_closed")
        self.assertEqual(closed["outcome"], "intent_on")
