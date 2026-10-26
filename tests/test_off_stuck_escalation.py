"""Report unresolved OFF incidents with fake time and no external I/O."""

# ruff: noqa: ANN001, ANN201, ANN202, D102, FBT003, INP001, PT009, SLF001

import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import test_floodlight_safety as fixtures

from custom_components.eufy_sdk.const import (
    CONF_LIGHT_CORRECTION,
    CONF_LIGHT_CORRECTION_PAUSE,
)
from custom_components.eufy_sdk.light_correction import SAFETY_EVENT, LightCorrection

ATTRIBUTES = {
    "off_stuck_incident": "incident_id",
    "off_stuck_reason": "reason",
    "off_stuck_escalate_at": "escalate_at",
    "off_stuck_opened_at": "opened_at",
}


class OffStuckEscalationTests(unittest.IsolatedAsyncioTestCase):
    """Reuse real entity callbacks, the existing fake clock and memory store."""

    _entity = fixtures.FloodlightSafetyTests._entity
    _report = fixtures.FloodlightSafetyTests._report
    asyncSetUp = fixtures.FloodlightSafetyTests.asyncSetUp
    asyncTearDown = fixtures.FloodlightSafetyTests.asyncTearDown
    make = fixtures.FloodlightSafetyTests.make
    drain = fixtures.FloodlightSafetyTests.drain
    at = fixtures.FloodlightSafetyTests.at
    report = fixtures.FloodlightSafetyTests.report

    def incidents(self, entity, kind="off_stuck"):
        return [
            call.args[1]
            for call in entity.hass.bus.async_fire.call_args_list
            if call.args[0] == SAFETY_EVENT and call.args[1]["kind"] == kind
        ]

    async def gap(self, *, binding=None):
        entity, controller, client = await self.make(binding=binding)
        await self.at(300)
        self.assertEqual(len(self.incidents(entity)), 1)
        return entity, controller, client

    async def restart(self, entity, controller):
        controller.stop("closed")
        restored = LightCorrection(entity)
        entity._light_correction = restored
        self.controllers.remove(controller)
        self.controllers.append(restored)
        await restored.async_added()
        await self.drain()
        return restored

    async def test_no_confirmation_boundary_and_two_minute_escalation(self):
        entity, controller, _ = await self.make()
        await entity.write(False)
        self.now = 299
        self.report(controller, True)
        await self.drain()
        self.assertEqual(self.incidents(entity), [])
        await self.at(300)
        incident = self.incidents(entity)[0]
        self.assertEqual(incident["reason"], "no_confirmation")
        self.assertEqual(incident["opened_at"], self.wall + 300)
        self.assertEqual(incident["off_since"], self.wall)
        self.assertEqual(incident["escalate_at"], self.wall + 420)
        self.assertEqual(incident["observed"], "on")
        self.assertIn("off_unconfirmed", controller.notice_kinds)

    async def test_pending_off_alert_is_reported_even_when_transport_waits(self):
        entity, controller, client = await self.make()
        await entity.write(False)
        client.connected = False
        controller.check_policy()
        await self.at(299)
        self.assertEqual(self.incidents(entity), [])
        await self.at(300)
        incident = self.incidents(entity)[0]
        self.assertEqual(incident["reason"], "no_confirmation")
        self.assertEqual(incident["waiting"], "bridge_disconnected")
        self.assertNotIn("off_unconfirmed", controller.notice_kinds)
        self.assertEqual(len(self.sent), 1)

    async def test_no_reports_boundary_and_unknown_episode_escalation(self):
        entity, controller, _ = await self.make()
        await self.at(299)
        self.assertEqual(self.incidents(entity), [])
        await self.at(300)
        incident = self.incidents(entity)[0]
        self.assertEqual(incident["reason"], "no_reports")
        self.assertEqual(incident["escalate_at"], incident["opened_at"] + 1800)
        self.assertEqual(incident["observed"], "unknown")
        self.assertIsNone(incident["camera_reported_at"])
        self.assertIsNone(incident["waiting"])
        self.assertIn("camera_gap", controller.notice_kinds)
        self.assertEqual(self.sent, [])

    async def test_report_gap_uses_last_qualifying_light_report(self):
        entity, controller, _ = await self.make()
        self.now = 50
        self.report(controller, True)
        reported = controller.report_received
        await self.at(reported + 299)
        self.assertEqual(self.incidents(entity), [])
        self.now = reported + 300
        controller.observe(
            {
                "source": "rtc",
                "observedAtMs": (self.wall + self.now) * 1000,
                "properties": {"motion": False},
            }
        )
        self.assertEqual(self.incidents(entity)[0]["reason"], "no_reports")
        self.assertEqual(controller.report_received, reported)

    async def test_known_on_episode_sets_motion_safe_escalation(self):
        entity, controller, _ = await self.make()
        self.now = 50
        self.report(controller, True)
        episode = int(controller.on_reported_at)
        await self.at(controller.report_received + 300)
        incident = self.incidents(entity)[0]
        self.assertEqual(incident["escalate_at"], episode + 1800)
        self.assertEqual(incident["camera_reported_at"], episode)

    async def test_old_on_episode_still_gets_two_minutes_after_open(self):
        entity, controller, _ = await self.make()
        self.report(controller, True)
        await self.at(2000)
        incident = self.incidents(entity)[0]
        self.assertEqual(incident["escalate_at"], incident["opened_at"] + 120)

    async def test_blocked_boundary_for_every_wait_category(self):
        for index, category in enumerate(
            ("cooldown", "bridge_disconnected", "camera_offline", "parent_disconnected")
        ):
            with self.subTest(category=category):
                entity, controller, client = await self.make(index=index)
                start = self.now
                if category == "cooldown":
                    controller.health_until = start + 1000
                elif category == "bridge_disconnected":
                    client.connected = False
                else:
                    entity.device["cameraHealth"] = (
                        {"connectivity": "offline"}
                        if category == "camera_offline"
                        else {"parentTransport": "disconnected"}
                    )
                controller.check_policy()
                self.now = start + 299
                self.report(controller, True)
                await self.drain()
                self.assertEqual(self.incidents(entity), [])
                await self.at(start + 300)
                incident = self.incidents(entity)[0]
                self.assertEqual(incident["reason"], "blocked")
                self.assertEqual(incident["waiting"], category)
                self.assertEqual(
                    incident["escalate_at"], int(controller.on_reported_at) + 1800
                )
                controller.stop("closed")
                self.controllers.remove(controller)
        self.assertEqual(self.sent, [])

    async def test_blocked_unknown_observation_uses_open_time(self):
        entity, controller, client = await self.make()
        client.connected = False
        controller.check_policy()
        # Reconnection invalidates observations without advancing camera receipt age.
        self.now = 100
        controller.reset_observations()
        self.now = 299
        controller.report_received = self.now
        await self.at(300)
        incident = self.incidents(entity)[0]
        self.assertEqual(incident["reason"], "blocked")
        self.assertEqual(incident["observed"], "unknown")
        self.assertEqual(incident["escalate_at"], incident["opened_at"] + 1800)

    async def test_blocked_wait_is_continuous_and_resets_after_recovery(self):
        entity, controller, client = await self.make()
        client.connected = False
        controller.check_policy()
        self.now = 299
        self.report(controller, True)
        client.connected = True
        controller.check_policy()
        self.assertIsNone(controller.waiting_since)
        self.now = 300
        entity.device["cameraHealth"] = {"connectivity": "offline"}
        controller.check_policy()
        self.now = 599
        self.report(controller, True)
        self.assertEqual(self.incidents(entity), [])
        await self.at(600)
        self.assertEqual(self.incidents(entity)[0]["reason"], "blocked")

    async def test_wait_category_changes_do_not_reset_continuous_block(self):
        entity, controller, client = await self.make()
        client.connected = False
        controller.check_policy()
        self.now = 200
        client.connected = True
        entity.device["cameraHealth"] = {"parentTransport": "disconnected"}
        self.report(controller, True)
        await self.at(300)
        self.assertEqual(self.incidents(entity)[0]["reason"], "blocked")

    async def test_existing_transport_block_counts_when_intent_becomes_off(self):
        self.states["sensor.fixture_intent"] = "unknown"
        entity, controller, client = await self.make(binding="sensor.fixture_intent")
        client.connected = False
        controller.check_policy()
        self.now = 299
        self.report(controller, True)
        await self.at(300)
        self.assertEqual(self.incidents(entity), [])
        self.states["sensor.fixture_intent"] = "off"
        controller.check_policy()
        self.assertEqual(self.incidents(entity)[0]["reason"], "blocked")
        self.assertEqual(self.sent, [])

    async def test_fresh_motion_lit_on_reports_never_open_waiting_for_quiet(self):
        entity, controller, _ = await self.make()
        for instant in range(0, 1800, 60):
            self.now = instant
            self.report(controller, True, motion=True)
            await self.drain()
            self.assertEqual(controller.state, "waiting for quiet")
            self.assertEqual(self.incidents(entity), [])
        self.assertEqual(self.sent, [])

    async def test_paused_and_unknown_intent_never_open(self):
        for index, state in enumerate(("paused", "unknown", "unavailable", None, "on")):
            with self.subTest(state=state):
                if state is not None:
                    self.states["sensor.fixture_intent"] = state
                else:
                    self.states.clear()
                entity, controller, _ = await self.make(
                    index=index, binding="sensor.fixture_intent"
                )
                await self.at(self.now + 2000)
                self.assertEqual(self.incidents(entity), [])
                controller.stop("closed")
                self.controllers.remove(controller)
        self.assertEqual(self.sent, [])

    async def test_pause_helper_prevents_open_even_with_old_pending_off(self):
        entity, controller, client = await self.make()
        await entity.write(False)
        entity.coordinator.config_entry.options[CONF_LIGHT_CORRECTION_PAUSE] = (
            "input_boolean.pause"
        )
        self.states["input_boolean.pause"] = "on"
        client.connected = False
        controller.check_policy()
        await self.at(2000)
        self.assertEqual(self.incidents(entity), [])
        self.assertEqual(len(self.sent), 1)

    async def test_confirmed_off_never_opens_for_report_gap_or_block(self):
        entity, controller, client = await self.make()
        self.report(controller, False)
        client.connected = False
        controller.check_policy()
        await self.at(300)
        await self.at(4000)
        self.assertEqual(self.incidents(entity), [])
        self.assertEqual(self.sent, [])

    async def test_disabled_safety_never_opens(self):
        entity, controller, client = await self.make(enabled=False)
        client.connected = False
        await self.at(4000)
        self.assertEqual(self.incidents(entity), [])
        self.assertEqual(controller.attributes, {})
        self.assertIsNone(controller.store)
        self.assertEqual(self.sent, [])

    async def test_no_reports_merges_to_no_confirmation_with_same_id(self):
        entity, _, _ = await self.gap()
        first = self.incidents(entity)[0]
        self.now = 400
        await entity.write(False)
        await self.at(699)
        self.assertEqual(len(self.incidents(entity)), 1)
        await self.at(700)
        updated = self.incidents(entity)[1]
        self.assertEqual(updated["incident_id"], first["incident_id"])
        self.assertEqual(updated["opened_at"], first["opened_at"])
        self.assertEqual(updated["reason"], "no_confirmation")
        self.assertEqual(updated["escalate_at"], first["opened_at"] + 120)
        self.assertLess(updated["escalate_at"], first["escalate_at"])
        await self.at(1000)
        self.assertEqual(len(self.incidents(entity)), 2)

    async def test_blocked_merges_without_extending_existing_deadline(self):
        entity, controller, client = await self.make()
        self.report(controller, True)
        client.connected = False
        controller.check_policy()
        start = self.now
        self.now = start + 299
        self.report(controller, True)
        await self.at(start + 300)
        first = self.incidents(entity)[0]
        await entity.write(False)
        await self.at(self.now + 300)
        updated = self.incidents(entity)[1]
        self.assertEqual(updated["reason"], "no_confirmation")
        self.assertEqual(updated["incident_id"], first["incident_id"])
        self.assertLessEqual(updated["escalate_at"], first["escalate_at"])
        self.assertEqual(len(self.sent), 1)

    async def test_no_confirmation_is_not_downgraded_when_pending_changes(self):
        entity, controller, client = await self.make()
        await entity.write(False)
        client.connected = False
        await self.at(300)
        first = deepcopy(controller.off_stuck)
        self.now = 400
        await entity.write(False)
        await self.at(600)
        self.assertEqual(controller.off_stuck, first)
        self.assertEqual(len(self.incidents(entity)), 1)

    async def test_each_lifecycle_close_outcome_is_emitted_once(self):
        for index, outcome in enumerate(
            ("intent_on", "paused", "intent_unknown", "disabled")
        ):
            with self.subTest(outcome=outcome):
                self.states["sensor.fixture_intent"] = "off"
                entity, controller, _ = await self.make(
                    index=index, binding="sensor.fixture_intent"
                )
                await self.at(self.now + 300)
                identity = controller.off_stuck["incident_id"]
                if outcome == "disabled":
                    entity.coordinator.config_entry.options[CONF_LIGHT_CORRECTION] = (
                        False
                    )
                    controller.options_updated()
                else:
                    if outcome == "paused":
                        entity.coordinator.config_entry.options[
                            CONF_LIGHT_CORRECTION_PAUSE
                        ] = "input_boolean.pause"
                        self.states["input_boolean.pause"] = "on"
                    else:
                        self.states["sensor.fixture_intent"] = (
                            "on" if outcome == "intent_on" else "unknown"
                        )
                    controller.check_policy()
                controller.check_policy()
                closed = self.incidents(entity, "off_stuck_closed")
                self.assertEqual(len(closed), 1)
                self.assertEqual(closed[0]["outcome"], outcome)
                self.assertEqual(closed[0]["incident_id"], identity)
                self.assertIs(type(closed[0]["closed_at"]), int)
                self.assertIsNone(controller.off_stuck)
                for attribute in ATTRIBUTES:
                    self.assertIsNone(entity.extra_state_attributes.get(attribute))
                controller.stop("closed")
                self.controllers.remove(controller)

    async def test_deliberately_paused_binding_closes_as_paused(self):
        self.states["sensor.fixture_intent"] = "off"
        entity, controller, _ = await self.gap(binding="sensor.fixture_intent")
        self.states["sensor.fixture_intent"] = "paused"
        controller.check_policy()
        self.assertEqual(
            self.incidents(entity, "off_stuck_closed")[0]["outcome"], "paused"
        )

    async def test_fresh_off_closes_without_a_pending_command(self):
        entity, controller, _ = await self.gap()
        identity = controller.off_stuck["incident_id"]
        self.report(controller, False)
        await self.drain()
        closed = self.incidents(entity, "off_stuck_closed")
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["incident_id"], identity)
        self.assertEqual(closed[0]["outcome"], "confirmed_off")
        self.assertEqual(
            set(closed[0]),
            {"entity", "name", "kind", "incident_id", "closed_at", "outcome"},
        )
        self.assertIsNone(controller.off_stuck)
        self.assertIsNone(next(iter(fixtures.MemoryStore.rows.values()))["off_stuck"])
        self.assertEqual(self.sent, [])

    async def test_off_confirmation_obeys_freshness_source_and_ordering(self):
        entity, controller, _ = await self.gap()
        for source, stamp in (
            ("rtc", self.wall + 179),  # Older than the 120-second window.
            ("cloud", self.wall + 300),
            ("rtc", self.wall + 301),  # Future timestamp.
            ("rtc", self.wall + 300),  # Equal to incident open time.
            ("rtc", True),
            ("rtc", float("nan")),
        ):
            controller.observe(
                {
                    "source": source,
                    "observedAtMs": stamp * 1000,
                    "properties": {"light": False},
                }
            )
            self.assertIsNotNone(controller.off_stuck)
        self.now = 350
        controller.observe(
            {
                "source": "rtc",
                "observedAtMs": (self.wall + 301) * 1000,
                "properties": {"light": False},
            }
        )
        self.assertIsNone(controller.off_stuck)
        self.assertEqual(len(self.incidents(entity, "off_stuck_closed")), 1)

    async def test_subsecond_open_time_survives_restart_and_rejects_earlier_off(self):
        entity, controller, _ = await self.make()
        await self.at(300.75)
        controller = await self.restart(entity, controller)
        for stamp in (300.5, 300.75):
            controller.observe(
                {
                    "source": "rtc",
                    "observedAtMs": (self.wall + stamp) * 1000,
                    "properties": {"light": False},
                }
            )
            self.assertIsNotNone(controller.off_stuck)
        self.report(controller, False)
        self.assertIsNone(controller.off_stuck)

    async def test_off_report_before_latest_send_does_not_close(self):
        entity, controller, client = await self.gap()
        client.connected = False
        self.now = 350
        await entity.write(False)
        self.now = 360
        controller.observe(
            {
                "source": "rtc",
                "observedAtMs": (self.wall + 349) * 1000,
                "properties": {"light": False},
            }
        )
        self.assertIsNotNone(controller.off_stuck)
        self.report(controller, False)
        self.assertIsNone(controller.off_stuck)

    async def test_duplicate_or_out_of_order_report_does_not_close(self):
        entity, controller, _ = await self.gap()
        self.report(controller, True)
        stamp = controller.observed_at * 1000
        for candidate in (stamp, stamp - 0.5):
            controller.observe(
                {
                    "source": "rtc",
                    "observedAtMs": candidate,
                    "properties": {"light": False},
                }
            )
            self.assertIsNotNone(controller.off_stuck)
        self.assertEqual(self.incidents(entity, "off_stuck_closed"), [])

    async def test_restored_notices_and_runtime_reconnect_do_not_close(self):
        entity, controller, _ = await self.gap()
        identity = controller.off_stuck["incident_id"]
        for kind in ("camera_restored", "intent_restored", "outcome_restored"):
            controller._notice(kind, False)
        self.report(controller, True)
        entity._handle_property_changed(SimpleNamespace(data={"event": "ready"}))
        controller.event({"intent": 1, "recoveryBusy": False})
        await self.drain()
        self.assertEqual(controller.off_stuck["incident_id"], identity)
        self.assertEqual(self.incidents(entity, "off_stuck_closed"), [])

    async def test_event_contract_and_attributes_are_written_before_each_event(self):
        entity, controller, _ = await self.make()
        written, snapshots = [], []
        entity.async_write_ha_state.side_effect = lambda: written.append(
            deepcopy(entity.extra_state_attributes)
        )

        def capture(event, data):
            if event == SAFETY_EVENT and data["kind"].startswith("off_stuck"):
                self.assertTrue(written)
                snapshots.append((data, written[-1]))
                for attribute, field in ATTRIBUTES.items():
                    self.assertEqual(
                        written[-1].get(attribute),
                        data[field] if data["kind"] == "off_stuck" else None,
                    )

        entity.hass.bus.async_fire.side_effect = capture
        await self.at(300)
        opened = self.incidents(entity)[0]
        self.assertEqual(
            set(opened),
            {
                "entity",
                "name",
                "kind",
                "incident_id",
                "reason",
                "opened_at",
                "off_since",
                "escalate_at",
                "observed",
                "camera_reported_at",
                "attempts",
                "waiting",
            },
        )
        self.assertRegex(opened["incident_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(opened["entity"], entity.entity_id)
        self.assertEqual(opened["name"], entity.name)
        self.assertEqual(opened["attempts"], 0)
        for field in ("opened_at", "off_since", "escalate_at", "camera_reported_at"):
            self.assertTrue(opened[field] is None or type(opened[field]) is int)
        await entity.write(False)
        await self.at(600)
        self.report(controller, False)
        self.assertEqual(len(snapshots), 3)
        for attribute in ATTRIBUTES:
            self.assertIsNone(entity.extra_state_attributes[attribute])

    async def test_incident_events_add_no_bell_phone_or_commands(self):
        entity, controller, client = await self.make()
        entity.coordinator.config_entry.options["light_safety_notify_service"] = (
            "notify.fixture_phone"
        )
        client.connected = False
        controller.check_policy()
        self.now = 299
        self.report(controller, True)
        await self.at(300)
        self.assertEqual(self.incidents(entity)[0]["reason"], "blocked")
        self.report(controller, False)
        await self.drain()
        self.notification.assert_not_called()
        self.dismiss.assert_not_called()
        entity.hass.services.async_call.assert_not_awaited()
        client.set_property.assert_not_awaited()
        client.confirm_light.assert_not_awaited()
        self.assertEqual(self.sent, [])

    async def test_repeated_ticks_do_not_repeat_or_expire_an_incident(self):
        entity, controller, _ = await self.gap()
        incident = deepcopy(controller.off_stuck)
        for when in (300, 301, 420, 2100, 4000):
            await self.at(when)
        self.assertEqual(len(self.incidents(entity)), 1)
        self.assertEqual(controller.off_stuck, incident)
        self.assertEqual(self.sent, [])

    async def test_restart_refires_same_incident_once_with_wall_clock_deadlines(self):
        entity, controller, _ = await self.gap()
        incident = deepcopy(controller.off_stuck)
        self.now = 500
        controller = await self.restart(entity, controller)
        self.assertEqual(controller.off_stuck, incident)
        self.assertEqual(len(self.incidents(entity)), 2)
        self.assertEqual(self.incidents(entity)[0], self.incidents(entity)[1])
        await controller._activate()
        await self.at(600)
        self.assertEqual(len(self.incidents(entity)), 2)
        self.report(controller, False)
        self.assertEqual(
            self.incidents(entity, "off_stuck_closed")[0]["outcome"], "confirmed_off"
        )

    async def test_restart_applies_intent_and_pause_close_rules_before_replay(self):
        for index, state, outcome in (
            (0, "on", "intent_on"),
            (1, "unknown", "intent_unknown"),
            (2, "paused", "paused"),
        ):
            with self.subTest(state=state):
                self.states["sensor.fixture_intent"] = "off"
                entity, controller, _ = await self.make(
                    index=index, binding="sensor.fixture_intent"
                )
                await self.at(self.now + 300)
                identity = controller.off_stuck["incident_id"]
                self.states["sensor.fixture_intent"] = state
                controller = await self.restart(entity, controller)
                self.assertIsNone(controller.off_stuck)
                self.assertEqual(len(self.incidents(entity)), 1)
                closed = self.incidents(entity, "off_stuck_closed")[0]
                self.assertEqual(closed["incident_id"], identity)
                self.assertEqual(closed["outcome"], outcome)
                controller.stop("closed")
                self.controllers.remove(controller)

    async def test_restart_pending_off_preserves_incident_and_existing_retry_age(self):
        entity, controller, client = await self.make()
        await entity.write(False)
        client.connected = False
        await self.at(300)
        incident = deepcopy(controller.off_stuck)
        self.now = 1000
        controller = await self.restart(entity, controller)
        self.assertEqual(controller.off_stuck, incident)
        self.assertEqual(controller.pending_since, 1000)
        self.assertEqual(controller.retry_due, 1060)
        self.assertEqual(
            self.incidents(entity)[1]["incident_id"], incident["incident_id"]
        )
        self.assertEqual(len(self.sent), 1)

    async def test_invalid_saved_incidents_are_dropped_without_affecting_safety(self):
        entity, controller, _ = await self.gap()
        key = controller.store.key
        original = deepcopy(fixtures.MemoryStore.rows[key])
        valid = original["off_stuck"]
        invalid = [None, [], "bad", {}, {**valid, "private": "discard"}]
        for field, values in {
            "incident_id": [None, [], "x" * 32, "a" * 31, "A" * 32],
            "reason": [None, [], "unrecognized"],
            "opened_at": [True, "300", -1, float("nan"), self.wall + 301],
            "off_since": [True, -1, "bad", valid["opened_at"] + 1],
            "escalate_at": [None, True, float("inf"), valid["opened_at"] + 119],
            "confirmation_after": [
                None,
                True,
                float("nan"),
                float("inf"),
                valid["opened_at"] - 0.01,
                10**1000,
            ],
        }.items():
            invalid.extend({**valid, field: value} for value in values)
        for saved in invalid:
            with self.subTest(saved=saved):
                fixtures.MemoryStore.rows[key] = {**original, "off_stuck": saved}
                controller = await self.restart(entity, controller)
                self.assertTrue(controller.loaded)
                self.assertIsNone(controller.off_stuck)
                self.assertIs(controller.intent(), False)
        self.assertEqual(len(self.incidents(entity)), 1)

    async def test_a_later_episode_gets_a_new_random_identity(self):
        entity, controller, _ = await self.gap()
        first = controller.off_stuck["incident_id"]
        self.report(controller, False)
        self.report(controller, True)
        await self.at(controller.report_received + 300)
        self.assertNotEqual(controller.off_stuck["incident_id"], first)
        self.assertEqual(len(self.incidents(entity)), 2)

    async def test_incidents_preserve_existing_command_schedule(self):
        entity, controller, _ = await self.make()
        reference, comparison, _ = await self.make(index=1)
        comparison._check_off_stuck = Mock()
        await entity.write(False)
        await reference.write(False)
        for instant in (60, 180, 300, 480, 780):
            await self.at(instant)
        actual = [
            (at, desired, auto)
            for at, sn, desired, auto in self.sent
            if sn == entity._sn
        ]
        expected = [
            (at, desired, auto)
            for at, sn, desired, auto in self.sent
            if sn == reference._sn
        ]
        self.assertEqual(actual, expected)
        self.assertEqual([at for at, _, _ in actual], [0, 60, 180, 480, 780])
        self.assertEqual(len(self.incidents(entity)), 1)
        self.assertIsNotNone(controller.off_stuck)
        self.assertEqual(self.incidents(reference), [])
