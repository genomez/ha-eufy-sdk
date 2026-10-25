"""Bound SDK waits, qualify detection silence and route safety notices offline."""

# ruff: noqa: ANN001, ANN002, ANN003, ANN201, ANN202, D102, FBT003
# ruff: noqa: INP001, PT009, PT027, SLF001, PLR2004

import asyncio
import unittest
from types import SimpleNamespace

import test_floodlight_safety as fixtures

from custom_components.eufy_sdk.api import CommandReceipt
from custom_components.eufy_sdk.light_correction import (
    REPORT_FRESH_SECONDS,
    LightCorrection,
)
from custom_components.eufy_sdk.property_sync import MAX_OBSERVATION_AGE_SECONDS


class FloodlightSafetyRound2Tests(unittest.IsolatedAsyncioTestCase):
    """Use the original synthetic fixture without duplicating its tests."""

    _entity = fixtures.FloodlightSafetyTests._entity
    _report = fixtures.FloodlightSafetyTests._report
    asyncSetUp = fixtures.FloodlightSafetyTests.asyncSetUp
    asyncTearDown = fixtures.FloodlightSafetyTests.asyncTearDown
    make = fixtures.FloodlightSafetyTests.make
    drain = fixtures.FloodlightSafetyTests.drain
    at = fixtures.FloodlightSafetyTests.at
    report = fixtures.FloodlightSafetyTests.report

    def runtime_ready(self, entity):
        entity._handle_property_changed(SimpleNamespace(data={"event": "ready"}))

    async def test_unknown_receipt_waits_full_bound_then_other_hub_lights_resume(self):
        entity, controller, client = await self.make()
        other, second, _ = await self.make(index=1, coordinator=entity.coordinator)
        send = client.set_property.side_effect

        async def lost_receipt(*_args, **kwargs):
            self.assertTrue(kwargs["_send_guard"]())
            message = "synthetic admitted failure"
            raise RuntimeError(message)

        client.set_property.side_effect = lost_receipt
        with self.assertRaises(RuntimeError):
            await entity.write(False)
        client.set_property.side_effect = send
        await other.write(False)
        await self.at(179)
        self.assertTrue(controller.receipt_unknown)
        self.assertTrue(controller.queue.busy())
        self.assertEqual(len(self.sent), 1)
        await self.at(180)
        self.assertFalse(controller.receipt_unknown)
        self.assertFalse(controller.recovery_pending)
        self.assertTrue(
            any(row[1] == second.entity._sn and row[3] for row in self.sent)
        )
        self.assertIn(
            "outcome_wait_expired", [row["kind"] for row in controller.journal]
        )
        self.assertNotIn("outcome_gap", controller.notice_kinds)

    async def test_stale_sdk_recovery_expires_at_due_plus_margin_even_offline(self):
        entity, controller, client = await self.make()
        other, second, _ = await self.make(index=1, coordinator=entity.coordinator)
        controller.track_recovery(
            CommandReceipt("pending_accepted", True, "unconfirmed", intent=7)
        )
        entity.device["cameraHealth"] = {"connectivity": "offline"}
        client.confirm_light.side_effect = RuntimeError("synthetic missing watch")
        await other.write(False)
        await self.at(224)
        self.assertTrue(controller.recovery_pending)
        self.assertEqual(len(self.sent), 1)
        await self.at(225)
        self.assertFalse(controller.recovery_pending)
        self.assertTrue(
            any(row[1] == second.entity._sn and row[3] for row in self.sent)
        )
        self.assertIn(
            "recovery_wait_expired", [row["kind"] for row in controller.journal]
        )

    async def test_disconnect_retains_wait_but_ready_clears_outcomes_immediately(self):
        entity, controller, _ = await self.make()
        controller.uncertain_outcome()
        controller.track_recovery(
            CommandReceipt("pending_accepted", True, "unconfirmed", intent=7)
        )
        entity._handle_property_changed(
            SimpleNamespace(data={"event": "bridgeDisconnected"})
        )
        self.assertTrue(controller.receipt_unknown)
        self.assertEqual(len(controller.recoveries), 1)
        self.now = 1
        self.runtime_ready(entity)
        self.assertFalse(controller.receipt_unknown)
        self.assertEqual(controller.recoveries, {})
        self.assertFalse(controller.queue.busy())

    async def test_old_explicit_rpc_cannot_restore_wait_after_new_runtime(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                entity, controller, client = await self.make(index=int(fail))
                started, release = asyncio.Event(), asyncio.Event()

                async def old_rpc(
                    *_args, started=started, release=release, fail=fail, **kwargs
                ):
                    self.assertTrue(kwargs["_send_guard"]())
                    started.set()
                    await release.wait()
                    if fail:
                        message = "synthetic old-runtime failure"
                        raise RuntimeError(message)
                    return CommandReceipt(
                        "pending_accepted", True, "unconfirmed", intent=7
                    )

                client.set_property.side_effect = old_rpc
                task = asyncio.create_task(entity.write(False))
                await started.wait()
                self.runtime_ready(entity)
                release.set()
                if fail:
                    with self.assertRaises(RuntimeError):
                        await task
                else:
                    await task
                self.assertFalse(controller.recovery_pending)
                self.assertEqual(controller.recoveries, {})

    async def test_old_automatic_rpc_cannot_restore_wait_after_new_runtime(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                entity, controller, client = await self.make(index=int(fail))
                await entity.write(False)
                started, release = asyncio.Event(), asyncio.Event()

                async def old_rpc(
                    *_args, started=started, release=release, fail=fail, **kwargs
                ):
                    self.assertTrue(kwargs["_send_guard"]())
                    started.set()
                    await release.wait()
                    if fail:
                        message = "synthetic old-runtime failure"
                        raise RuntimeError(message)
                    return CommandReceipt(
                        "pending_accepted", True, "unconfirmed", intent=7
                    )

                client.set_property.side_effect = old_rpc
                self.now += 60
                controller.check_policy()
                await started.wait()
                self.runtime_ready(entity)
                release.set()
                await self.drain()
                self.assertFalse(controller.recovery_pending)

    async def test_no_detection_push_fires_quiet_rule_at_fifteen_minutes(self):
        _, controller, _ = await self.make()
        self.report(controller, True)
        self.now = 899
        self.report(controller, True)
        await self.drain()
        self.assertEqual(self.sent, [])
        await self.at(900.01)
        self.assertEqual(len(self.sent), 1)

    async def test_detection_at_ten_minutes_delays_quiet_rule_until_twenty_five(self):
        entity, controller, _ = await self.make()
        self.report(controller, True)
        self.now = 600
        entity._handle_property_changed(
            SimpleNamespace(data={"event": "personDetected", "deviceSn": entity._sn})
        )
        for instant in (900, 1499):
            self.now = instant
            self.report(controller, True)
            await self.drain()
            self.assertEqual(self.sent, [])
        await self.at(1500)
        self.assertEqual(len(self.sent), 1)

    async def test_rtc_true_motion_counts_as_detection_then_false_allows_quiet(self):
        _, controller, _ = await self.make()
        self.report(controller, True)
        self.now = 600
        self.report(controller, True, person=True)
        self.now = 1499
        self.report(controller, True, person=False)
        await self.drain()
        self.assertEqual(self.sent, [])
        await self.at(1500.01)
        self.assertEqual(len(self.sent), 1)

    async def test_unhealthy_telemetry_cannot_qualify_quiet_or_dispatch(self):
        entity, controller, client = await self.make()
        self.report(controller, True)
        self.now = 901
        self.report(controller, True)
        for mode in ("disconnected", "offline", "parent_disconnected", "stale"):
            with self.subTest(mode=mode):
                client.connected = mode != "disconnected"
                entity.device["cameraHealth"] = {
                    "connectivity": "offline" if mode == "offline" else "online",
                    "parentTransport": "disconnected"
                    if mode == "parent_disconnected"
                    else "connected",
                }
                controller.observed_at = (
                    self.wall + self.now - (121 if mode == "stale" else 1)
                )
                self.assertFalse(controller._quiet())
                controller.check_policy()
                await self.drain()
                self.assertEqual(self.sent, [])
        client.connected = True
        entity.device["cameraHealth"] = {"connectivity": "online"}
        self.now = 1801
        self.report(controller, True, motion=True)
        await self.drain()
        self.assertFalse(controller._quiet())
        self.assertEqual(len(self.sent), 1)

    async def test_controller_120_second_window_preserves_presentation_window(self):
        self.assertEqual(MAX_OBSERVATION_AGE_SECONDS, 60)
        self.assertEqual(REPORT_FRESH_SECONDS, 120)
        for age, accepted in ((61, True), (120, True), (120.01, False)):
            with self.subTest(age=age):
                _, controller, _ = await self.make(index=len(self.controllers))
                self.now += 200
                controller.observe(
                    {
                        "source": "rtc",
                        "observedAtMs": (self.wall + self.now - age) * 1000,
                        "properties": {"light": True},
                    }
                )
                self.assertIs(controller.observed, True if accepted else None)
                self.assertEqual(controller._fresh(), accepted)
                self.now += 0.02
                if age == 120:
                    self.assertFalse(controller._fresh())

    async def test_aged_post_send_confirmation_uses_controller_window(self):
        entity, controller, _ = await self.make()
        await entity.write(False)
        self.now = 100
        controller.observe(
            {
                "source": "rtc",
                "observedAtMs": (self.wall + 1) * 1000,
                "properties": {"light": False},
            }
        )
        self.assertIsNone(controller.pending)

    async def test_off_problem_starts_at_five_minutes_not_first_retry(self):
        entity, controller, _ = await self.make()
        await entity.write(False)
        for instant in (60, 299):
            self.now = instant
            self.report(controller, True)
            await self.drain()
            self.assertNotIn("off_unconfirmed", controller.notice_kinds)
        await self.at(300)
        self.assertIn("off_unconfirmed", controller.notice_kinds)

    async def test_only_command_result_kinds_reach_phone(self):
        entity, controller, _ = await self.make()
        entity.coordinator.config_entry.options["light_safety_notify_service"] = (
            "notify.fixture_phone"
        )
        phone = ("off_unconfirmed", "now_off", "on_unconfirmed", "now_on")
        gaps = ("camera_gap", "intent_gap", "outcome_gap")
        restored = ("camera_restored", "intent_restored", "outcome_restored")
        for kind in ("safety_off", *phone, *gaps, *restored):
            controller._notice(kind, kind.endswith("unconfirmed") or kind in gaps)
            await self.drain()
        await self.drain()
        self.assertEqual(entity.hass.services.async_call.await_count, len(phone))
        self.assertEqual(self.notification.call_count, len(phone) + len(gaps))
        self.assertEqual(self.dismiss.call_count, len(restored))
        self.assertEqual(
            entity.hass.bus.async_fire.call_count,
            1 + len(phone) + len(gaps) + len(restored),
        )
        created = {
            call.kwargs["notification_id"] for call in self.notification.call_args_list
        }
        self.assertTrue(
            all(call.args[1] in created for call in self.dismiss.call_args_list)
        )

    async def test_gap_restore_does_not_suppress_off_success_phone_replacement(self):
        entity, controller, _ = await self.make()
        entity.coordinator.config_entry.options["light_safety_notify_service"] = (
            "notify.fixture_phone"
        )
        await entity.write(False)
        await self.at(301)
        self.assertIn("off_unconfirmed", controller.notice_kinds)
        self.assertIn("camera_gap", controller.notice_kinds)
        self.report(controller, False)
        await self.drain()
        kinds = [
            call.args[1]["kind"] for call in entity.hass.bus.async_fire.call_args_list
        ]
        self.assertIn("camera_restored", kinds)
        self.assertIn("now_off", kinds)
        calls = entity.hass.services.async_call.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            calls[0].args[2]["data"]["tag"], calls[1].args[2]["data"]["tag"]
        )

    async def test_gap_restoration_dismisses_only_its_own_bell(self):
        entity, controller, _ = await self.make()
        for kind in ("off_unconfirmed", "camera_gap", "intent_gap"):
            controller._notice(kind, True)
        await self.drain()
        ids = {
            call.args[1]["kind"]: self.notification.call_args_list[index].kwargs[
                "notification_id"
            ]
            for index, call in enumerate(entity.hass.bus.async_fire.call_args_list)
        }
        controller._notice("camera_restored", False)
        await self.drain()
        self.dismiss.assert_called_once_with(entity.hass, ids["camera_gap"])
        self.assertNotEqual(ids["camera_gap"], ids["off_unconfirmed"])
        self.assertNotEqual(ids["camera_gap"], ids["intent_gap"])

    async def test_confirmed_safety_off_keeps_event_without_phone_or_bell(self):
        entity, controller, _ = await self.make()
        entity._attr_name = "Front Yard FLC Light"
        entity.coordinator.config_entry.options["light_safety_notify_service"] = (
            "notify.fixture_phone"
        )
        self.report(controller, True)
        self.now = 959
        self.report(controller, True)
        await self.drain()
        self.report(controller, False)
        self.now = 2000  # Delivery delay must not change the captured ON duration.
        await self.drain()
        self.notification.assert_not_called()
        self.dismiss.assert_not_called()
        entity.hass.services.async_call.assert_not_awaited()
        data = entity.hass.bus.async_fire.call_args.args[1]
        self.assertEqual(
            entity.hass.bus.async_fire.call_args.args[0], "eufy_sdk_floodlight_safety"
        )
        self.assertEqual(data["kind"], "safety_off")
        self.assertEqual(data["entity"], entity.entity_id)
        self.assertEqual(data["name"], "Front Yard FLC Light")
        self.assertEqual(data["reported_on_minutes"], 15)
        self.assertIsNotNone(data["on_since"])

    async def test_ha_friendly_name_overrides_entity_short_name(self):
        entity, controller, _ = await self.make()
        entity.hass.states.get.side_effect = lambda _key: SimpleNamespace(
            name="Yard renamed in HA"
        )
        controller._notice("camera_gap", True)
        await self.drain()
        self.assertIn("Yard renamed in HA", self.notification.call_args.args[1])

    async def test_failed_phone_cannot_delay_gap_bell_or_restoration_event(self):
        entity, controller, _ = await self.make()
        entity.coordinator.config_entry.options["light_safety_notify_service"] = (
            "notify.fixture_phone"
        )
        entity.hass.services.async_call.side_effect = RuntimeError("synthetic failure")
        controller._notice("off_unconfirmed", True)
        await self.drain()
        controller._notice("camera_gap", True)
        self.assertEqual(self.notification.call_count, 2)
        controller._notice("camera_restored", False)
        self.dismiss.assert_called_once()
        self.assertEqual(entity.hass.bus.async_fire.call_count, 3)
        self.assertEqual(entity.hass.services.async_call.await_count, 1)

    async def test_active_rtc_motion_blocks_quiet_even_with_unknown_person(self):
        _, controller, _ = await self.make()
        self.report(controller, True, motion=True, person=None)
        self.now = 1000
        self.report(controller, True)
        self.assertFalse(controller._quiet())
        await self.drain()
        self.assertEqual(self.sent, [])
        controller.reset_observations()
        self.assertFalse(controller.motion_active)

    async def test_watchdog_dispatch_accepts_a_61_second_old_report(self):
        _, controller, _ = await self.make()
        self.report(controller, True)
        self.now = 839
        self.report(controller, True)
        await self.at(900.01)
        self.assertEqual(len(self.sent), 1)

    async def test_failed_problem_delivery_is_superseded_by_confirmed_success(self):
        entity, controller, _ = await self.make()
        entity.coordinator.config_entry.options["light_safety_notify_service"] = (
            "notify.fixture_phone"
        )
        await entity.write(False)
        entity.hass.services.async_call.side_effect = RuntimeError(
            "synthetic notify failure"
        )
        controller._notice("off_unconfirmed", True)
        await self.drain()
        self.assertIsNotNone(controller.notice_pending)
        self.report(controller, False)
        await self.at(60)
        entity.hass.services.async_call.side_effect = None
        await self.at(120)
        self.assertIsNone(controller.notice_pending)
        self.assertIn(
            "Off now (camera confirmed after 1 try).",
            entity.hass.services.async_call.call_args.args[2]["message"],
        )

    async def test_first_fresh_on_report_restores_gap_from_previous_off_episode(self):
        entity, controller, _ = await self.make()
        self.report(controller, False)
        await self.at(301)
        self.assertIn("camera_gap", controller.notice_kinds)
        self.report(controller, True)
        await self.drain()
        self.dismiss.assert_called_once()
        kinds = [
            call.args[1]["kind"] for call in entity.hass.bus.async_fire.call_args_list
        ]
        self.assertIn("camera_restored", kinds)

    async def test_new_explicit_command_preserves_tracking_of_active_gap_bell(self):
        entity, controller, _ = await self.make()
        self.report(controller, False)
        await self.at(301)
        await entity.write(True)
        self.assertIn("camera_gap", controller.notice_kinds)
        self.report(controller, True)
        await self.drain()
        self.dismiss.assert_called_once()

    async def test_recovered_phone_sends_only_success_after_confirmation(self):
        for desired in (False, True):
            with self.subTest(desired=desired):
                entity, controller, _ = await self.make(index=int(desired))
                entity.coordinator.config_entry.options[
                    "light_safety_notify_service"
                ] = "notify.fixture_phone"
                await entity.write(desired)
                problem = "on_unconfirmed" if desired else "off_unconfirmed"
                entity.hass.services.async_call.side_effect = RuntimeError("synthetic")
                controller._notice(problem, True)
                await self.drain()
                entity.hass.services.async_call.reset_mock()
                entity.hass.services.async_call.side_effect = None
                self.report(controller, desired)
                await self.drain()
                entity.hass.services.async_call.assert_awaited_once()
                data = entity.hass.services.async_call.call_args.args[2]
                self.assertNotIn("importance", data["data"])
                self.assertEqual(
                    data["message"],
                    f"{'On' if desired else 'Off'} now (camera confirmed after 1 try).",
                )
                self.assertIsNone(controller.notice_pending)

    async def test_queued_notice_dedupe_and_delivery_survive_restart(self):
        entity, controller, _ = await self.make()
        entity.coordinator.config_entry.options["light_safety_notify_service"] = (
            "notify.fixture_phone"
        )
        entity.hass.services.async_call.side_effect = RuntimeError(
            "synthetic notify failure"
        )
        controller._notice("off_unconfirmed", True)
        controller._notice("camera_gap", True)
        await self.drain()
        controller.stop("closed")
        restored = LightCorrection(entity)
        self.controllers.append(restored)
        entity._light_correction = restored
        entity.hass.services.async_call.side_effect = None
        await restored.async_added()
        await self.drain()
        self.assertIsNone(restored.notice_pending)
        self.assertEqual(len(restored.notice_queue), 0)
        kinds = [
            call.args[1]["kind"] for call in entity.hass.bus.async_fire.call_args_list
        ]
        self.assertEqual(kinds.count("off_unconfirmed"), 1)
        self.assertEqual(kinds.count("camera_gap"), 1)

    async def test_problem_and_resolution_text_priority_and_replacement(self):
        messages = {
            False: "Still on. OFF was sent, but the camera hasn't confirmed it "
            "after 5 minutes. The safety keeps trying.",
            True: "Didn't turn on. HA asked for ON 10 minutes ago and the camera "
            "hasn't confirmed it. The safety keeps trying while the schedule "
            "wants it on.",
        }
        for desired in (False, True):
            with self.subTest(desired=desired):
                self.notification.reset_mock()
                entity, controller, _ = await self.make(index=int(desired))
                entity._attr_name = "Back Left FLC Light"
                entity.coordinator.config_entry.options[
                    "light_safety_notify_service"
                ] = "notify.fixture_phone"
                await entity.write(desired)
                self.report(controller, not desired)
                await self.at(self.now + (600 if desired else 300))
                phone = entity.hass.services.async_call.call_args_list
                self.assertEqual(len(phone), 1)
                problem = phone[0].args[2]
                self.assertEqual(
                    problem["title"], "Floodlight problem: Back Left FLC Light"
                )
                self.assertEqual(problem["message"], messages[desired])
                self.assertEqual(problem["data"]["importance"], "high")
                self.assertIs(problem["data"]["sticky"], True)
                bell = next(
                    call
                    for call in self.notification.call_args_list
                    if call.kwargs["title"] == problem["title"]
                )
                self.assertEqual(bell.args[1], problem["message"])
                self.assertEqual(bell.kwargs["title"], problem["title"])
                self.report(controller, desired)
                await self.drain()
                phone = entity.hass.services.async_call.call_args_list
                self.assertEqual(len(phone), 2)
                resolved = phone[1].args[2]
                state = "On" if desired else "Off"
                attempts = entity.hass.bus.async_fire.call_args.args[1]["attempts"]
                tries = "1 try" if attempts == 1 else f"{attempts} tries"
                self.assertEqual(resolved["title"], "Floodlight: Back Left FLC Light")
                self.assertEqual(
                    resolved["message"],
                    f"{state} now (camera confirmed after {tries}).",
                )
                self.assertEqual(resolved["data"]["tag"], problem["data"]["tag"])
                self.assertNotIn("sticky", resolved["data"])
                self.assertNotIn("importance", resolved["data"])
                replacement = self.notification.call_args
                self.assertEqual(
                    replacement.kwargs["notification_id"],
                    bell.kwargs["notification_id"],
                )
                self.assertEqual(replacement.kwargs["title"], resolved["title"])
                self.assertEqual(replacement.args[1], resolved["message"])
                controller.stop("closed")

    async def test_notice_singular_and_plural_text(self):
        entity, controller, _ = await self.make()
        entity._attr_name = "Back Left FLC Light"
        for count, tries, minutes in (
            (1, "1 try", "1 minute"),
            (2, "2 tries", "2 minutes"),
        ):
            for kind, message in (
                ("now_off", f"Off now (camera confirmed after {tries})."),
                ("now_on", f"On now (camera confirmed after {tries})."),
                (
                    "safety_off",
                    f"Turned off after {minutes} on; the schedule wanted it off.",
                ),
            ):
                with self.subTest(count=count, kind=kind):
                    title, actual, _ = controller._publish_notice(
                        {"kind": kind, "attempts": count, "on_minutes": count}
                    )
                    self.assertEqual(title, "Floodlight: Back Left FLC Light")
                    self.assertEqual(actual, message)

    async def test_all_gap_and_restored_text_and_routing_unchanged(self):
        entity, controller, _ = await self.make()
        entity._attr_name = "Back Left FLC Light"
        entity.coordinator.config_entry.options["light_safety_notify_service"] = (
            "notify.fixture_phone"
        )
        messages = {
            "camera_gap": "Protection gap: no fresh camera reports for 5 minutes.",
            "intent_gap": "Protection gap: intent has been unknown or unavailable "
            "for 5 minutes.",
            "outcome_gap": "Protection gap: the previous SDK outcome is unavailable; "
            "automatic commands are waiting.",
            "camera_restored": "Fresh camera reports are available again.",
            "intent_restored": "Intent is available again or deliberately paused.",
            "outcome_restored": "The previous SDK outcome wait has ended.",
        }
        for kind, text in messages.items():
            with self.subTest(kind=kind):
                self.notification.reset_mock()
                self.dismiss.reset_mock()
                notice = {"kind": kind, "attempts": 0}
                title, message, _ = controller._publish_notice(notice)
                self.assertEqual(title, "Floodlight safety: Back Left FLC Light")
                self.assertEqual(message, f"Back Left FLC Light: {text}")
                bell_id = (
                    f"eufy_floodlight_safety_{entity.entity_id}_"
                    f"{kind.replace('_restored', '_gap')}"
                )
                if kind.endswith("_restored"):
                    self.notification.assert_not_called()
                    self.dismiss.assert_called_once_with(entity.hass, bell_id)
                else:
                    self.dismiss.assert_not_called()
                    self.notification.assert_called_once_with(
                        entity.hass, message, title=title, notification_id=bell_id
                    )
                controller._notice(kind, kind.endswith("_gap"))
                await self.drain()
                entity.hass.services.async_call.assert_not_awaited()
