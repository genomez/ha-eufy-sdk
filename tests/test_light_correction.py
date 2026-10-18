"""Exercise HA correction ownership with synthetic SDK evidence and fake time."""

# ruff: noqa: ANN001, ANN002, ANN003, ANN201, ANN202, D102, FBT003
# ruff: noqa: INP001, PT009, PT027, SLF001, PLR2004
import asyncio
import json
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import test_bounded_state_writes as bounded
import test_floodlight_state_hold as fixtures
from aiohttp import WSMsgType

from custom_components.eufy_sdk.api import (
    CommandReceipt,
    EufySdkApiClientCommunicationError,
    late_contrary_event,
)
from custom_components.eufy_sdk.const import (
    CONF_LIGHT_CORRECTION,
    CONF_LIGHT_CORRECTION_PAUSE,
)
from custom_components.eufy_sdk.light_correction import FAILURE_EVENT

CAPABILITY = {"version": 1, "watchWindowMs": 600000, "confirmation": True}


class LightCorrectionTests(unittest.IsolatedAsyncioTestCase):
    """Use actual entity write/event layers around a single controller task."""

    _entity = fixtures.FloodlightStateHoldTests._entity
    _report = fixtures.FloodlightStateHoldTests._report

    def setup_light(self, *, enabled=True, desired=False):
        entity, coordinator, client, seen = self._entity(initial=not desired)
        self.now = 0.0
        self.events = []
        self.tasks = []
        self.commands = []
        self.next_intent = 0
        self.paused = False
        self.entity, self.client = entity, client
        coordinator.config_entry.options = {CONF_LIGHT_CORRECTION: enabled}
        client.bounded_state_writes_supported = True
        client.late_contrary_effects_supported = True
        client.get_properties = AsyncMock(
            return_value=[
                {
                    "name": "light",
                    "boundedStateWrites": True,
                }
            ]
        )

        async def send(_sn, _name, value, **kwargs):
            kwargs.pop("_send_guard", None)
            self.next_intent += 1
            self.commands.append((self.now, value, kwargs))
            return CommandReceipt(
                "acknowledged", True, "unconfirmed", intent=self.next_intent
            )

        client.set_property.side_effect = send
        client.pause_light_correction = Mock()

        async def confirm(_sn, intent):
            return {
                "intent": intent,
                "desiredValue": desired,
                "observedValue": not desired,
                "confirmed": True,
                "recoveryBusy": False,
                "ageMs": self.now * 1000,
            }

        client.confirm_light = AsyncMock(side_effect=confirm)
        entity.entity_id = "switch.fixture_light"
        entity.hass.states.get.side_effect = lambda _: SimpleNamespace(
            state="on" if self.paused else "off"
        )

        def later(_hass, seconds, callback):
            event = [self.now + seconds, callback, False]
            self.events.append(event)
            return lambda: event.__setitem__(2, True)

        def task(coro):
            result = asyncio.create_task(coro)
            self.tasks.append(result)
            return result

        entity.hass.async_create_task.side_effect = task
        patches = (
            patch(
                "custom_components.eufy_sdk.light_correction.monotonic",
                lambda: self.now,
            ),
            patch(
                "custom_components.eufy_sdk.light_correction.async_call_later", later
            ),
            patch(
                "custom_components.eufy_sdk.light_correction.persistent_notification.async_create"
            ),
        )
        for item in patches:
            started = item.start()
            self.addCleanup(item.stop)
            if item is patches[-1]:
                self.notification = started
        return entity, coordinator, client, seen

    def emit(self, state="contrary", *, intent=None, observed=None, busy=False):
        controller = self.entity._light_correction
        event = {
            "event": "lateContraryEffect",
            "deviceSn": "fixture-device",
            "property": "light",
            "intent": controller.intent if intent is None else intent,
            "desiredValue": controller.desired,
            "ageMs": self.now * 1000,
            "state": state,
            "recoveryBusy": busy,
        }
        if observed is not None:
            event["observedValue"] = observed
        elif state in {"contrary", "matched"}:
            event["observedValue"] = (
                controller.desired if state == "matched" else not controller.desired
            )
        self.entity._handle_property_changed(SimpleNamespace(data=event))

    async def advance(self, target):
        while True:
            due = [
                event for event in self.events if not event[2] and event[0] <= target
            ]
            if not due:
                break
            event = min(due, key=lambda event: event[0])
            self.now, event[2] = event[0], True
            event[1](None)
            for _ in range(5):
                await asyncio.sleep(0)
            completed = [task for task in self.tasks if task.done()]
            if completed:
                await asyncio.gather(*completed, return_exceptions=False)
                self.tasks = [task for task in self.tasks if not task.done()]
        self.now = target

    async def test_incident_match_15_8_then_contrary_32_corrects_at_47(self):
        entity, _, _, _ = self.setup_light()
        await entity.write(False)
        original_receipt = entity._command_receipt
        await self.advance(15.8)
        self.emit("matched")
        await self.advance(32)
        self.emit()
        await self.advance(46.999)
        self.assertEqual(len(self.commands), 1)
        await self.advance(47)
        self.assertEqual(
            self.commands[-1], (47, False, {"bounded": True, "correction": True})
        )
        self.assertIs(entity._command_receipt, original_receipt)
        self.assertEqual(entity._light_correction.born, 0)

    async def test_both_directions_and_reported_state_stays_honest(self):
        for desired in (False, True):
            with self.subTest(desired=desired):
                entity, _, _, _ = self.setup_light(desired=desired)
                await entity.write(desired)
                self._report(entity, desired)
                self.now = 32
                self._report(entity, not desired)
                entity._clear_light_settle()
                self.emit()
                await self.advance(47)
                self.assertIs(entity.is_on, not desired)
                self.assertIs(self.commands[-1][1], desired)
                self.assertEqual(
                    entity.extra_state_attributes["command_status"], "acknowledged"
                )

    async def test_unconfirmed_fresh_read_does_not_send_or_loop(self):
        entity, _, client, _ = self.setup_light()
        await entity.write(False)
        client.confirm_light.side_effect = None
        client.confirm_light.return_value = {
            "desiredValue": False,
            "confirmed": False,
            "recoveryBusy": False,
        }
        self.emit()
        await self.advance(590)
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(client.confirm_light.await_count, 1)

    async def test_property_changed_alone_never_authorizes_correction(self):
        entity, _, client, _ = self.setup_light()
        await entity.write(False)
        self._report(entity, True)
        await self.advance(590)
        client.confirm_light.assert_not_awaited()
        self.assertEqual(len(self.commands), 1)

    async def test_match_before_correction_cancels_due_work(self):
        entity, _, client, _ = self.setup_light()
        await entity.write(False)
        self.emit()
        await self.advance(14)
        self.emit("matched")
        await self.advance(100)
        client.confirm_light.assert_not_awaited()

    async def test_fresh_match_before_send_never_corrects(self):
        entity, _, client, _ = self.setup_light()
        await entity.write(False)
        client.confirm_light.side_effect = None
        client.confirm_light.return_value = {
            "desiredValue": False,
            "confirmed": True,
            "recoveryBusy": False,
            "observedValue": False,
        }
        self.emit()
        await self.advance(100)
        self.assertEqual(len(self.commands), 1)

    async def test_match_arrives_during_fresh_read_wins_over_contrary_reply(self):
        entity, _, client, _ = self.setup_light()
        await entity.write(False)

        async def read(*_):
            self.emit("matched")
            return {
                "desiredValue": False,
                "confirmed": True,
                "recoveryBusy": False,
                "observedValue": True,
            }

        client.confirm_light.side_effect = read
        self.emit()
        await self.advance(100)
        self.assertEqual(len(self.commands), 1)

    async def test_newer_automation_command_cancels_old_work_immediately(self):
        entity, _, client, _ = self.setup_light()
        await entity.write(False)
        old_intent = entity._light_correction.intent
        self.emit()
        await self.advance(10)
        await entity.write(True)
        self.emit(intent=old_intent)
        await self.advance(100)
        self.assertEqual(len(self.commands), 2)
        client.confirm_light.assert_not_awaited()

    async def test_newer_command_during_read_cancels_before_corrective_send(self):
        entity, _, client, _ = self.setup_light()
        await entity.write(False)
        started = asyncio.Event()
        blocker = asyncio.Event()

        async def read(*_):
            started.set()
            await blocker.wait()
            return {
                "desiredValue": False,
                "confirmed": True,
                "recoveryBusy": False,
                "observedValue": True,
            }

        client.confirm_light.side_effect = read
        self.emit()
        await self.advance(15)
        self.assertTrue(started.is_set())
        await entity.write(True)
        blocker.set()
        await asyncio.sleep(0)
        self.assertEqual(len(self.commands), 2)

    async def test_pause_off_resumes_overdue_offset_without_resetting_window(self):
        entity, coordinator, _, _ = self.setup_light()
        coordinator.config_entry.options[CONF_LIGHT_CORRECTION_PAUSE] = (
            "input_boolean.pause"
        )
        self.paused = True
        await entity.write(False)
        self.emit()
        await self.advance(70)
        self.assertEqual(len(self.commands), 1)
        self.paused = False
        entity._light_correction._pause_changed(
            SimpleNamespace(data={"entity_id": "input_boolean.pause"})
        )
        await self.advance(70)
        self.assertEqual([command[0] for command in self.commands], [0, 70])
        await self.advance(114.999)
        self.assertEqual(len(self.commands), 2)
        await self.advance(115)
        self.assertEqual([command[0] for command in self.commands], [0, 70, 115])
        self.assertEqual(entity._light_correction.born, 0)

    async def test_unavailable_pause_helper_prevents_send(self):
        entity, coordinator, _, _ = self.setup_light()
        coordinator.config_entry.options[CONF_LIGHT_CORRECTION_PAUSE] = "switch.pause"
        entity.hass.states.get.side_effect = None
        entity.hass.states.get.return_value = None
        await entity.write(False)
        self.emit()
        await self.advance(400)
        self.assertEqual(len(self.commands), 1)

    async def test_pause_before_ws_send_refunds_known_unsent_attempt(self):
        entity, coordinator, client, _ = self.setup_light()
        coordinator.config_entry.options[CONF_LIGHT_CORRECTION_PAUSE] = "switch.pause"
        await entity.write(False)
        original_intent = entity._light_correction.intent

        async def paused_send(*_, **kwargs):
            self.paused = True
            self.assertFalse(kwargs["_send_guard"]())
            message = "light_correction_paused_before_send"
            raise EufySdkApiClientCommunicationError(message)

        previous = client.set_property.side_effect
        client.set_property.side_effect = paused_send
        self.emit()
        await self.advance(20)
        self.assertEqual(entity._light_correction.attempts, 0)
        self.assertEqual(entity._light_correction.intent, original_intent)
        self.assertEqual(len(self.commands), 1)
        client.set_property.side_effect = previous
        self.paused = False
        entity._light_correction._pause_changed(
            SimpleNamespace(data={"entity_id": "switch.pause"})
        )
        await self.advance(20)
        self.assertEqual(len(self.commands), 2)

    async def test_pause_cancellation_receipt_preserves_mapping_and_budget_rules(self):
        for sent in (False, True):
            with self.subTest(sent=sent):
                entity, coordinator, client, _ = self.setup_light()
                coordinator.config_entry.options[CONF_LIGHT_CORRECTION_PAUSE] = (
                    "switch.pause"
                )
                await entity.write(False)
                original_receipt = entity._command_receipt
                previous = client.set_property.side_effect

                async def paused_send(
                    *args, previous=previous, entity=entity, sent=sent, **kwargs
                ):
                    receipt = await previous(*args, **kwargs)
                    self.paused = True
                    entity._light_correction._pause_changed(
                        SimpleNamespace(data={"entity_id": "switch.pause"})
                    )
                    return CommandReceipt(
                        "cancelled_after_send" if sent else "cancelled_before_send",
                        sent,
                        "unknown",
                        intent=receipt.intent,
                    )

                client.set_property.side_effect = paused_send
                self.emit()
                await self.advance(20)
                client.pause_light_correction.assert_called_once_with("fixture-device")
                self.assertEqual(entity._light_correction.intent, 2)
                self.assertEqual(entity._light_correction.attempts, int(sent))
                self.assertEqual(entity._light_correction.born, 0)
                self.assertIs(entity._command_receipt, original_receipt)
                self.assertEqual(entity._light_correction.state, "paused")
                client.set_property.side_effect = previous
                self.paused = False
                entity._light_correction._pause_changed(
                    SimpleNamespace(data={"entity_id": "switch.pause"})
                )
                await self.advance(60 if sent else 20)
                self.assertEqual(len(self.commands), 3)

    async def test_pause_on_during_read_prevents_send_without_spending_attempt(self):
        entity, coordinator, client, _ = self.setup_light()
        coordinator.config_entry.options[CONF_LIGHT_CORRECTION_PAUSE] = "switch.pause"
        await entity.write(False)
        previous = client.confirm_light.side_effect

        async def read(*args):
            self.paused = True
            entity._light_correction._pause_changed(
                SimpleNamespace(data={"entity_id": "switch.pause"})
            )
            return await previous(*args)

        client.confirm_light.side_effect = read
        self.emit()
        await self.advance(20)
        self.assertEqual(entity._light_correction.attempts, 0)
        self.assertEqual(len(self.commands), 1)

    async def test_pause_off_during_guard_cleanup_cannot_lose_resume(self):
        entity, coordinator, client, _ = self.setup_light()
        coordinator.config_entry.options[CONF_LIGHT_CORRECTION_PAUSE] = "switch.pause"
        await entity.write(False)
        previous = client.set_property.side_effect

        async def paused_send(*_, **kwargs):
            self.paused = True
            entity._light_correction._pause_changed(
                SimpleNamespace(data={"entity_id": "switch.pause"})
            )
            self.assertFalse(kwargs["_send_guard"]())
            self.paused = False
            entity._light_correction._pause_changed(
                SimpleNamespace(data={"entity_id": "switch.pause"})
            )
            client.set_property.side_effect = previous
            message = "light_correction_paused_before_send"
            raise EufySdkApiClientCommunicationError(message)

        client.set_property.side_effect = paused_send
        self.emit()
        await self.advance(20)
        self.assertEqual(len(self.commands), 2)
        self.assertEqual(entity._light_correction.attempts, 1)

    async def test_cancelled_or_repeated_timer_callback_cannot_start_another_task(self):
        entity, _, client, _ = self.setup_light()
        await entity.write(False)
        self.emit()
        old_callback = self.events[-1][1]
        self.emit()
        current_callback = self.events[-1][1]
        release = asyncio.Event()
        previous = client.confirm_light.side_effect

        async def blocked_read(*args):
            await release.wait()
            return await previous(*args)

        client.confirm_light.side_effect = blocked_read
        self.now = 15
        old_callback(None)
        self.assertEqual(len(self.tasks), 0)
        current_callback(None)
        first_task = entity._light_correction.task
        current_callback(None)
        old_callback(None)
        await asyncio.sleep(0)
        self.assertEqual(client.confirm_light.await_count, 1)
        self.assertEqual(len(self.tasks), 1)
        self.assertIs(entity._light_correction.task, first_task)
        release.set()
        await first_task
        self.assertEqual(len(self.commands), 2)

    async def test_task_rechecks_exhausted_attempt_budget_after_fresh_read(self):
        entity, _, _, _ = self.setup_light()
        await entity.write(False)
        self.emit()
        controller = entity._light_correction
        controller.attempts = 3
        controller.pending_final = False
        await controller._run(controller.generation)
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(controller.attempts, 3)

    async def test_slow_receipt_spaces_next_send_from_completion(self):
        entity, _, client, _ = self.setup_light()
        await entity.write(False)
        previous = client.set_property.side_effect
        receipt_ready = asyncio.Event()

        async def delayed_send(*args, **kwargs):
            receipt = await previous(*args, **kwargs)
            if len(self.commands) == 2:
                await receipt_ready.wait()
            return receipt

        client.set_property.side_effect = delayed_send
        self.emit()
        await self.advance(100)
        self.assertEqual(len(self.commands), 2)
        receipt_ready.set()
        for _ in range(5):
            await asyncio.sleep(0)
        await self.advance(144.999)
        self.assertEqual(len(self.commands), 2)
        await self.advance(145)
        self.assertEqual([command[0] for command in self.commands], [0, 15, 145])

    async def test_match_or_recovery_busy_during_dispatch_preparation_prevents_send(
        self,
    ):
        for state in ("matched", "contrary"):
            with self.subTest(state=state):
                entity, _, client, _ = self.setup_light()
                await entity.write(False)

                async def delayed_send(*_, state=state, **kwargs):
                    self.emit(state, busy=state == "contrary")
                    self.assertFalse(kwargs["_send_guard"]())
                    message = "light_correction_paused_before_send"
                    raise EufySdkApiClientCommunicationError(message)

                client.set_property.side_effect = delayed_send
                self.emit()
                await self.advance(20)
                self.assertEqual(len(self.commands), 1)
                self.assertEqual(entity._light_correction.attempts, 0)
                self.assertEqual(
                    entity._light_correction.state,
                    "matched" if state == "matched" else "recovery_busy",
                )

    async def test_three_failures_notify_and_event_once_no_fourth(self):
        entity, _, client, _ = self.setup_light()
        await entity.write(False)
        self.emit()
        await self.advance(314.999)
        self.assertEqual([command[0] for command in self.commands], [0, 15, 60, 300])
        self.notification.assert_not_called()
        await self.advance(315)
        self.assertEqual(client.confirm_light.await_count, 4)
        self.notification.assert_called_once()
        failure_events = [
            call
            for call in entity.hass.bus.async_fire.call_args_list
            if call.args[0] == FAILURE_EVENT
        ]
        self.assertEqual(len(failure_events), 1)
        self.emit()
        await self.advance(599)
        self.assertEqual(len(self.commands), 4)
        self.notification.assert_called_once()

    async def test_third_correction_matching_final_read_suppresses_failure(self):
        entity, _, client, _ = self.setup_light()
        await entity.write(False)
        previous = client.confirm_light.side_effect

        async def read(sn, intent):
            result = await previous(sn, intent)
            if len(self.commands) == 4:
                result["observedValue"] = False
            return result

        client.confirm_light.side_effect = read
        self.emit()
        await self.advance(500)
        self.assertEqual(len(self.commands), 4)
        self.notification.assert_not_called()

    async def test_matching_event_during_final_settle_cancels_failure_read(self):
        entity, _, client, _ = self.setup_light()
        await entity.write(False)
        self.emit()
        await self.advance(310)
        self.assertEqual(len(self.commands), 4)
        self.assertEqual(client.confirm_light.await_count, 3)
        self.emit("matched")
        await self.advance(590)
        self.assertEqual(client.confirm_light.await_count, 3)
        self.notification.assert_not_called()

    async def test_passive_detection_before_any_ha_command_never_gains_authority(self):
        entity, _, client, _ = self.setup_light(enabled=False)
        entity._handle_property_changed(
            SimpleNamespace(
                data={
                    "event": "lateContraryEffect",
                    "deviceSn": "fixture-device",
                    "property": "light",
                    "intent": 4,
                    "desiredValue": False,
                    "observedValue": True,
                    "ageMs": 32000,
                    "state": "contrary",
                    "recoveryBusy": False,
                }
            )
        )
        self.assertIs(
            entity.extra_state_attributes["light_contrary_observed_value"], True
        )
        self.assertIsNone(entity._light_correction.desired)
        await self.advance(500)
        client.confirm_light.assert_not_awaited()
        self.assertEqual(self.commands, [])

    async def test_window_display_uses_utc_but_wall_changes_do_not_extend_authority(
        self,
    ):
        entity, _, _, _ = self.setup_light()
        await entity.write(False)
        display = entity.extra_state_attributes["correction_window_until"]
        self.assertEqual(datetime.fromisoformat(display).utcoffset().total_seconds(), 0)
        self.emit()
        self.assertIsNotNone(entity.extra_state_attributes["last_contrary_at"])
        await self.advance(600)
        self.assertEqual(entity._light_correction.state, "expired")
        self.assertEqual(
            entity.extra_state_attributes["correction_window_until"], display
        )

    async def test_third_correction_matching_early_event_suppresses_failure(self):
        entity, _, client, _ = self.setup_light()
        await entity.write(False)
        previous = client.set_property.side_effect

        async def send(*args, **kwargs):
            receipt = await previous(*args, **kwargs)
            if len(self.commands) == 4:
                self.emit("matched", intent=receipt.intent)
            return receipt

        client.set_property.side_effect = send
        self.emit()
        await self.advance(500)
        self.assertEqual(len(self.commands), 4)
        self.notification.assert_not_called()

    async def test_recovery_busy_waits_for_settle_then_fresh_read(self):
        entity, _, client, _ = self.setup_light()
        await entity.write(False)
        self.emit(busy=True)
        await self.advance(20)
        client.confirm_light.assert_not_awaited()
        self.emit("recovery_settled")
        await self.advance(20)
        self.assertEqual(len(self.commands), 2)
        self.assertEqual(client.confirm_light.await_count, 1)

    async def test_read_busy_waits_for_settle_instead_of_repeating_reads(self):
        entity, _, client, _ = self.setup_light()
        await entity.write(False)
        previous = client.confirm_light.side_effect

        async def read(sn, intent):
            result = await previous(sn, intent)
            result["recoveryBusy"] = client.confirm_light.await_count == 1
            return result

        client.confirm_light.side_effect = read
        self.emit()
        await self.advance(25)
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(client.confirm_light.await_count, 1)
        self.emit("recovery_settled")
        await self.advance(25)
        self.assertEqual(len(self.commands), 2)

    async def test_own_superseded_transition_cannot_cancel_lineage(self):
        entity, _, client, _ = self.setup_light()
        await entity.write(False)
        previous = client.set_property.side_effect

        async def send(*args, **kwargs):
            old_intent = entity._light_correction.intent
            self.emit("superseded", intent=old_intent)
            return await previous(*args, **kwargs)

        client.set_property.side_effect = send
        self.emit()
        await self.advance(61)
        self.assertEqual(len(self.commands), 3)
        self.assertEqual(entity._light_correction.born, 0)

    async def test_oscillation_cannot_renew_attempt_budget_or_window(self):
        entity, _, _, _ = self.setup_light()
        await entity.write(False)
        self.emit()
        await self.advance(15)
        self.emit("matched")
        await self.advance(59)
        self.emit()
        await self.advance(60)
        self.emit("matched")
        await self.advance(299)
        self.emit()
        await self.advance(300)
        self.assertEqual(len(self.commands), 4)
        self.assertEqual(entity._light_correction.attempts, 3)
        self.assertEqual(entity._light_correction.born, 0)

    async def test_original_early_events_are_replayed_only_for_receipt_intent(self):
        entity, _, client, _ = self.setup_light()
        previous = client.set_property.side_effect

        async def send(*args, **kwargs):
            self.emit(intent=999)
            self.emit(intent=1)
            return await previous(*args, **kwargs)

        client.set_property.side_effect = send
        await entity.write(False)
        await self.advance(15)
        self.assertEqual(len(self.commands), 2)

    async def test_option_off_preserves_detection_without_reads_or_writes(self):
        entity, _, client, _ = self.setup_light(enabled=False)
        await entity.write(False)
        self.emit()
        await self.advance(500)
        self.assertEqual(
            entity.extra_state_attributes["light_correction_state"], "contrary"
        )
        client.confirm_light.assert_not_awaited()
        self.assertEqual(len(self.commands), 1)

    async def test_exact_expiry_disconnect_and_unload_stop_corrections(self):
        for termination in ("expiry", "disconnect", "unload"):
            with self.subTest(termination=termination):
                entity, _, client, _ = self.setup_light()
                await entity.write(False)
                await self.advance(590)
                self.emit()
                if termination == "disconnect":
                    entity._handle_property_changed(
                        SimpleNamespace(data={"event": "bridgeDisconnected"})
                    )
                elif termination == "unload":
                    await entity.async_will_remove_from_hass()
                await self.advance(610)
                client.confirm_light.assert_not_awaited()
                self.assertEqual(len(self.commands), 1)

    async def test_other_target_and_invalid_event_do_not_arm(self):
        entity, _, client, _ = self.setup_light()
        await entity.write(False)
        for data in (
            {"event": "lateContraryEffect", "deviceSn": "other"},
            {"event": "lateContraryEffect", "deviceSn": "fixture-device"},
        ):
            entity._handle_property_changed(SimpleNamespace(data=data))
        await self.advance(400)
        client.confirm_light.assert_not_awaited()


class LightCorrectionProtocolTests(unittest.IsolatedAsyncioTestCase):
    """Use the actual RPC/WS layer with in-memory bridge replies."""

    client = bounded.BoundedProtocolTests.client

    async def test_correction_flag_only_in_negotiated_bounded_write(self):
        client = self.client()
        with self.assertRaises(EufySdkApiClientCommunicationError):
            await client.set_property(
                "fixture", "light", False, bounded=True, correction=True
            )
        self.assertEqual(self.frames, [])
        client._set_late_contrary_capability(CAPABILITY)
        await client.set_property(
            "fixture", "light", False, bounded=True, correction=True
        )
        self.assertIs(self.frames[-1]["execution"]["correction"], True)

    async def test_fresh_confirmation_is_read_only_and_projected(self):
        client = self.client()
        client._set_late_contrary_capability(CAPABILITY)
        confirmation = {
            "intent": 1,
            "desiredValue": False,
            "observedValue": True,
            "confirmed": True,
            "recoveryBusy": False,
            "ageMs": 33000,
        }

        async def reply(payload):
            self.frames.append(payload)
            client._pending[payload["id"]].set_result(
                {
                    "ok": True,
                    "confirmation": {**confirmation, "private": "discard"},
                }
            )

        client._ws.send_json.side_effect = reply
        self.assertEqual(await client.confirm_light("fixture", 1), confirmation)
        self.assertEqual(self.frames[0]["cmd"], "device.confirmLight")

    async def test_pause_during_clock_handshake_blocks_device_set_dispatch(self):
        client = self.client()
        client._set_late_contrary_capability(CAPABILITY)
        allowed = True
        previous = client._ws.send_json.side_effect

        async def clock_reply(payload):
            nonlocal allowed
            await previous(payload)
            allowed = False

        client._ws.send_json.side_effect = clock_reply
        with self.assertRaisesRegex(
            EufySdkApiClientCommunicationError, "^light_correction_paused_before_send$"
        ):
            await client.set_property(
                "fixture",
                "light",
                False,
                bounded=True,
                correction=True,
                _send_guard=lambda: allowed,
            )
        self.assertEqual([frame["cmd"] for frame in self.frames], ["boundedStateClock"])
        self.assertEqual(client._correction_requests, {})

    async def test_pause_cancel_keeps_original_rpc_future_for_terminal_receipt(self):
        client = self.client()
        client._set_late_contrary_capability(CAPABILITY)
        previous = client._ws.send_json.side_effect
        submitted = asyncio.Event()
        request_id = None

        async def reply(payload):
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
                self.assertEqual(payload["requestId"], request_id)
                self.assertFalse(client._pending[request_id].done())
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
                correction=True,
                _send_guard=lambda: True,
            )
        )
        await submitted.wait()
        client.pause_light_correction("fixture")
        receipt = await task
        self.assertEqual(receipt.intent, 2)
        self.assertEqual(receipt.status, "cancelled_before_send")
        self.assertEqual(client._correction_requests, {})
        self.assertEqual(
            len(
                [frame for frame in self.frames if frame["cmd"] == "boundedStateCancel"]
            ),
            1,
        )

    async def test_invalid_confirmation_fails_closed(self):
        client = self.client()
        client._set_late_contrary_capability(CAPABILITY)
        for result in (
            None,
            {},
            {"intent": True},
            {"intent": 2, "desiredValue": False},
        ):
            client.rpc = AsyncMock(return_value={"confirmation": result})
            with self.assertRaises(EufySdkApiClientCommunicationError):
                await client.confirm_light("fixture", 1)

    def test_capability_and_event_validation_fail_closed(self):
        client = self.client()
        for value in (
            None,
            {},
            {**CAPABILITY, "version": True},
            {**CAPABILITY, "watchWindowMs": 120000},
            {**CAPABILITY, "confirmation": False},
        ):
            client._set_late_contrary_capability(value)
            self.assertFalse(client.late_contrary_effects_supported)
        event = {
            "event": "lateContraryEffect",
            "deviceSn": "fixture",
            "property": "light",
            "intent": 1,
            "desiredValue": False,
            "observedValue": True,
            "ageMs": 32000,
            "state": "contrary",
            "recoveryBusy": False,
        }
        self.assertEqual(late_contrary_event({**event, "private": "discard"}), event)
        for key, value in (
            ("intent", True),
            ("intent", 0),
            ("ageMs", float("nan")),
            ("ageMs", 600001),
            ("observedValue", 1),
            ("state", "unknown"),
        ):
            self.assertIsNone(late_contrary_event({**event, key: value}))
        missing_observation = event.copy()
        missing_observation.pop("observedValue")
        self.assertIsNone(late_contrary_event(missing_observation))

    async def test_actual_receive_loop_projects_events_and_emits_disconnect(self):
        client = self.client()
        events = []
        client._on_event = events.append
        hello = {
            "event": "hello",
            "boundedStateWrites": bounded.CAPABILITY,
            "lateContraryEffects": CAPABILITY,
        }
        event = {
            "event": "lateContraryEffect",
            "deviceSn": "fixture",
            "property": "light",
            "intent": 1,
            "desiredValue": False,
            "observedValue": True,
            "ageMs": 32000,
            "state": "contrary",
            "recoveryBusy": False,
        }

        async def frames():
            for value in (
                hello,
                {**event, "private": "discard"},
                {**event, "intent": False},
            ):
                yield SimpleNamespace(
                    type=WSMsgType.TEXT, json=lambda value=value: value
                )

        client._ws.__aiter__ = lambda _: frames()
        client._closing = True
        await client._receive_loop()
        self.assertEqual(events, [hello, event, {"event": "bridgeDisconnected"}])
        self.assertFalse(client.late_contrary_effects_supported)
        self.assertNotIn("discard", json.dumps(events))
