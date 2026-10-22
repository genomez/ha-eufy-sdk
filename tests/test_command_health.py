"""Regression coverage for OFF-only correction and SDK-owned command health."""

# ruff: noqa: ANN001, ANN002, ANN201, ANN202, D102, FBT003, INP001, PT009, PT027, SLF001
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import test_aligned_state as aligned
import test_light_correction as fixtures
import test_light_grace as grace
from aiohttp import WSMsgType

from custom_components.eufy_sdk.api import (
    _command_receipt,
    command_health,
    command_health_event,
)
from custom_components.eufy_sdk.const import CONF_LIGHT_CORRECTION_PAUSE


class CommandSafetyTests(unittest.IsolatedAsyncioTestCase):
    """Drive real entity callbacks and controller timers with synthetic evidence."""

    _entity = fixtures.LightCorrectionTests._entity
    _report = fixtures.LightCorrectionTests._report
    setup_light = fixtures.LightCorrectionTests.setup_light
    emit = fixtures.LightCorrectionTests.emit
    advance = fixtures.LightCorrectionTests.advance
    dispatch = fixtures.LightCorrectionTests.dispatch

    def health(self, status="command_degraded", remaining=600000):
        self.entity._handle_property_changed(
            SimpleNamespace(
                data={
                    "event": "commandHealth",
                    "sn": "fixture-device",
                    "property": "light",
                    "status": status,
                    "cooldownRemainingMs": remaining,
                }
            )
        )

    async def test_degraded_event_cancels_timer_keeps_watch_and_resumes_off(self):
        entity, _, client, _ = self.setup_light()
        await entity.write(False)
        self.emit()
        self.health(remaining=120000)
        self.assertIsNone(entity._light_correction.timer)
        self.assertEqual(
            entity.extra_state_attributes["command_health"], "command_degraded"
        )
        await self.advance(119)
        self.assertEqual(len(self.commands), 1)
        client.confirm_light.assert_not_awaited()
        self.emit("matched")
        self.emit()
        self.health("healthy", 0)
        await self.advance(120)
        self.assertEqual(len(self.commands), 2)
        self.assertEqual(entity._light_correction.born, 0)

    async def test_degraded_during_read_cancels_task_before_dispatch(self):
        entity, _, client, _ = self.setup_light()
        await entity.write(False)
        blocker = asyncio.get_running_loop().create_future()

        async def read(*_):
            await blocker

        client.confirm_light.side_effect = read
        self.emit()
        await self.advance(15)
        task = entity._light_correction.task
        self.assertIsNotNone(task)
        self.health()
        await asyncio.sleep(0)
        self.assertTrue(task.cancelled())
        self.assertEqual(len(self.commands), 1)
        self.assertIsNotNone(entity._light_correction.expiry)

    async def test_degraded_receipt_preserves_passive_lineage_no_false_failure(self):
        entity, _, _, _ = self.setup_light()
        await entity.write(False)
        receipt = _command_receipt(
            {
                "status": "cancelled_before_send",
                "sendAttempted": False,
                "effect": "unconfirmed",
                "intent": 1,
                "commandHealth": {
                    "status": "command_degraded",
                    "cooldownRemainingMs": 600000,
                },
                "rejectionCode": -108,
                "responseLatencyMs": 89,
            }
        )
        entity._light_correction.receipt(receipt, correction=True)
        self.emit()
        await self.advance(600)
        self.assertEqual(len(self.commands), 1)
        self.notification.assert_not_called()
        self.assertEqual(entity.extra_state_attributes["command_health"], "healthy")

    async def test_explicit_command_is_allowed_during_degraded_cooldown(self):
        entity, _, _, _ = self.setup_light()
        await entity.write(False)
        self.health()
        await entity.async_turn_on()
        self.assertEqual([value for _, value, _ in self.commands], [False, True])
        self.assertEqual(
            entity.extra_state_attributes["command_health"], "command_degraded"
        )
        self.emit()
        await self.advance(600)
        self.assertEqual(len(self.commands), 2)
        self.notification.assert_not_called()

    async def test_cooldown_expiry_resumes_only_remaining_off_budget(self):
        entity, _, _, _ = self.setup_light()
        await entity.write(False)
        self.emit()
        self.health(remaining=120000)
        await self.advance(600)
        self.assertEqual([at for at, _, _ in self.commands], [0, 120, 165, 300])
        self.assertEqual(entity._light_correction.born, 0)
        self.assertEqual(entity._light_correction.attempts, 3)

    async def test_healthy_receipt_precedes_event_and_does_not_claim_state(self):
        entity, _, _, _ = self.setup_light()
        await entity.write(False)
        self.health()
        self.emit()
        entity._light_correction.receipt(
            _command_receipt(
                {
                    "status": "acknowledged",
                    "sendAttempted": True,
                    "effect": "unconfirmed",
                    "intent": 1,
                    "commandHealth": {"status": "healthy", "cooldownRemainingMs": 0},
                }
            )
        )
        self.assertEqual(entity.extra_state_attributes["command_health"], "healthy")
        self.assertEqual(entity.extra_state_attributes["command_effect"], "unconfirmed")
        self.assertIs(entity._light_correction.observed, True)
        self.emit()
        await self.advance(15)
        self.assertEqual(len(self.commands), 2)

    async def test_recovery_respects_pause_and_new_on_supersession(self):
        entity, coordinator, client, _ = self.setup_light()
        coordinator.config_entry.options[CONF_LIGHT_CORRECTION_PAUSE] = "switch.pause"
        await entity.write(False)
        self.emit()
        self.health(remaining=60000)
        self.paused = True
        await self.advance(60)
        self.assertEqual(len(self.commands), 1)
        await entity.async_turn_on()
        self.paused = False
        entity._light_correction._pause_changed(
            SimpleNamespace(data={"entity_id": "switch.pause"})
        )
        await self.advance(590)
        self.assertEqual([value for _, value, _ in self.commands], [False, True])
        client.confirm_light.assert_not_awaited()

    async def test_health_final_admission_and_stale_recovery_are_guarded(self):
        entity, _, _, _ = self.setup_light()
        await entity.write(False)
        self.emit()
        controller = entity._light_correction
        generation = controller.generation
        self.health(remaining=60000)
        recovery = self.events[-1][1]
        self.assertFalse(controller._admit_write(generation))
        controller.stop("closed")
        self.dispatch(recovery, None)
        await self.advance(1000)
        self.assertIsNone(controller.health_timer)
        self.assertEqual(len(self.commands), 1)
        self.notification.assert_not_called()

    async def test_snapshot_health_is_adopted_once_without_cooldown_extension(self):
        entity, coordinator, _, _ = self.setup_light()
        await entity.write(False)
        self.emit()
        coordinator.data["fixture-device"]["commandHealth"] = {
            "status": "command_degraded",
            "cooldownRemainingMs": 120000,
        }
        entity._handle_coordinator_update()
        await self.advance(60)
        entity._handle_coordinator_update()
        self.assertEqual(
            entity.extra_state_attributes["command_health_cooldown_remaining_ms"], 60000
        )
        await self.advance(120)
        self.assertEqual(len(self.commands), 2)

    async def test_health_snapshot_race_preserves_newer_event(self):
        coordinator = aligned.coordinator_for(Mock())
        coordinator._clear_fast_retry = Mock()

        async def snapshot():
            coordinator.record_command_health(
                "station", {"status": "command_degraded", "cooldownRemainingMs": 600000}
            )
            return {
                "station": {
                    "commandHealth": {"status": "healthy", "cooldownRemainingMs": 0}
                }
            }

        coordinator._read_snapshot = snapshot
        result = await coordinator._async_update_data()
        self.assertEqual(
            result["station"]["commandHealth"]["status"], "command_degraded"
        )
        self.assertIsNone(coordinator._pending_command_health)

    def test_health_schema_rejects_malformed_values_and_strips_extras(self):
        valid = {"status": "command_degraded", "cooldownRemainingMs": 600000}
        self.assertEqual(command_health({**valid, "private": "drop"}), valid)
        for value in (
            None,
            {},
            {**valid, "status": "offline"},
            {**valid, "status": []},
            *(
                {**valid, "cooldownRemainingMs": number}
                for number in (True, -1, 600001, float("nan"), float("inf"), 10**1000)
            ),
        ):
            with self.subTest(value=value):
                self.assertIsNone(command_health(value))
        event = {
            "event": "commandHealth",
            "sn": "fixture",
            "property": "light",
            **valid,
        }
        self.assertEqual(command_health_event({**event, "private": "drop"}), event)
        for change in (
            {"sn": ""},
            {"sn": "x" * 129},
            {"property": "lightPower"},
            {"event": "other"},
        ):
            with self.subTest(change=change):
                self.assertIsNone(command_health_event({**event, **change}))

    def test_receipt_preserves_raw_code_latency_and_status_without_local_gate(self):
        base = {
            "status": "pending_accepted",
            "sendAttempted": True,
            "effect": "unconfirmed",
            "rejectionCode": -108,
            "responseLatencyMs": 89,
        }
        receipt = _command_receipt(base)
        self.assertEqual(receipt.status, "pending_accepted")
        self.assertEqual(receipt.rejection_code, -108)
        self.assertEqual(receipt.response_latency_ms, 89)
        self.assertIsNone(receipt.command_health)
        for value in (True, -108.1, float("nan"), float("inf"), 10**1000):
            with self.subTest(code=value):
                self.assertIsNone(
                    _command_receipt({**base, "rejectionCode": value}).rejection_code
                )
        for value in (True, -1, float("nan"), float("inf"), 10**1000):
            with self.subTest(latency=value):
                self.assertIsNone(
                    _command_receipt(
                        {**base, "responseLatencyMs": value}
                    ).response_latency_ms
                )

    async def test_suppressed_correction_maps_receipt_intent_and_refunds_unsent(self):
        entity, _, client, _ = self.setup_light()
        await entity.write(False)
        original_send = client.set_property.side_effect
        client.set_property.side_effect = None
        client.set_property.return_value = _command_receipt(
            {
                "status": "cancelled_before_send",
                "sendAttempted": False,
                "effect": "unconfirmed",
                "intent": 2,
                "commandHealth": {
                    "status": "command_degraded",
                    "cooldownRemainingMs": 60000,
                },
            }
        )
        self.emit()
        await self.advance(15)
        controller = entity._light_correction
        self.assertEqual(controller.intent, 2)
        self.assertEqual(controller.attempts, 0)
        self.assertEqual(controller.born, 0)
        self.assertIsNotNone(controller.expiry)
        self.notification.assert_not_called()
        self.emit()
        self.next_intent = 2
        client.set_property.side_effect = original_send
        await self.advance(75)
        self.assertEqual(client.confirm_light.call_args.args[1], 2)
        self.assertEqual(controller.intent, 3)
        self.assertEqual(controller.attempts, 1)
        self.assertEqual(self.commands[-1][:2], (75, False))


class HealthProtocolTests(unittest.IsolatedAsyncioTestCase):
    """Validate real API projections without opening any transport."""

    client = fixtures.bounded.BoundedProtocolTests.client

    async def test_receive_loop_filters_and_projects_health(self):
        client = self.client()
        seen = []
        client._on_event = seen.append
        event = {
            "event": "commandHealth",
            "sn": "fixture",
            "property": "light",
            "status": "command_degraded",
            "cooldownRemainingMs": 600000,
        }

        async def frames():
            for value in (
                {**event, "private": "drop"},
                {**event, "cooldownRemainingMs": -1},
            ):
                yield SimpleNamespace(
                    type=WSMsgType.TEXT, json=lambda value=value: value
                )

        client._ws.__aiter__ = lambda _: frames()
        client._closing = True
        await client._receive_loop()
        self.assertEqual(seen, [event])

    async def test_receipt_health_projects_before_return_and_switch_attributes(self):
        client = self.client()
        seen = []
        client._on_event = seen.append
        raw = {
            "status": "pending_accepted",
            "sendAttempted": True,
            "effect": "unconfirmed",
            "intent": 1,
            "rejectionCode": -108,
            "responseLatencyMs": 97,
            "commandHealth": {
                "status": "command_degraded",
                "cooldownRemainingMs": 600000,
            },
        }
        send = client._ws.send_json.side_effect

        async def reply(payload):
            if payload["cmd"] == "device.set":
                client._pending[payload["id"]].set_result({"ok": True, "receipt": raw})
            else:
                await send(payload)

        client._ws.send_json.side_effect = reply
        with patch(
            "custom_components.eufy_sdk.api.time.time", side_effect=[1000, 1000.4]
        ):
            receipt = await client.set_property("fixture", "light", False, bounded=True)
        self.assertEqual(
            seen,
            [
                {
                    "event": "commandHealth",
                    "sn": "fixture",
                    "property": "light",
                    **raw["commandHealth"],
                }
            ],
        )
        entity, _, _, _ = fixtures.fixtures.FloodlightStateHoldTests._entity(self)
        entity._command_receipt = receipt
        attributes = entity.extra_state_attributes
        self.assertEqual(attributes["command_rejection_code"], -108)
        self.assertEqual(attributes["command_response_latency_ms"], 97)
        self.assertEqual(attributes["command_effect"], "unconfirmed")

    async def test_device_snapshot_projection_strips_invalid_health(self):
        client = self.client()
        valid = {"status": "healthy", "cooldownRemainingMs": 0}
        client.rpc = AsyncMock(
            return_value={
                "devices": [
                    {"sn": "fixture", "commandHealth": {**valid, "private": "drop"}},
                    {"sn": "other", "commandHealth": {"status": "offline"}},
                ]
            }
        )
        devices = await client.list_devices()
        self.assertEqual(devices[0]["commandHealth"], valid)
        self.assertNotIn("commandHealth", devices[1])

    async def test_paused_correction_task_abort_keeps_passive_observation(self):
        client = self.client()
        client._set_late_contrary_capability(fixtures.CAPABILITY)
        send = client._ws.send_json.side_effect

        async def reply(payload):
            if payload["cmd"] in {"device.set", "boundedStateCancel"}:
                self.frames.append(payload)
            else:
                await send(payload)

        client._ws.send_json.side_effect = reply
        task = asyncio.create_task(
            client.set_property(
                "fixture",
                "light",
                False,
                bounded=True,
                correction=True,
            )
        )
        for _ in range(20):
            await asyncio.sleep(0)
            if client._correction_requests:
                break
        self.assertTrue(client._correction_requests)
        client.pause_light_correction("fixture")
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.gather(*tuple(client._cancel_tasks))
        cancels = [
            frame for frame in self.frames if frame["cmd"] == "boundedStateCancel"
        ]
        self.assertTrue(cancels)
        self.assertTrue(all(frame.get("keepObservation") is True for frame in cancels))


class GraceHealthTests(unittest.IsolatedAsyncioTestCase):
    """Keep the existing grace and cap despite a command cooldown."""

    _entity = fixtures.LightCorrectionTests._entity
    _report = fixtures.LightCorrectionTests._report
    setup_light = fixtures.LightCorrectionTests.setup_light
    advance = fixtures.LightCorrectionTests.advance
    dispatch = fixtures.LightCorrectionTests.dispatch
    setup_grace = grace.LightGraceTests.setup_grace
    observation = grace.LightGraceTests.observation
    motion = grace.LightGraceTests.motion
    arm = grace.LightGraceTests.arm
    health = CommandSafetyTests.health

    async def test_health_recovery_does_not_restart_motion_grace(self):
        entity, _, _, _ = await self.arm()
        self.health(remaining=60000)
        await self.advance(661)
        self.assertEqual(entity._light_correction.grace_due, 901)
        await self.advance(901)
        self.assertEqual(self.commands[-1][:2], (901, False))

    async def test_motion_during_cooldown_preserves_original_cap(self):
        entity, _, _, _ = await self.arm()
        self.health(remaining=600000)
        for moment in (800, 1000, 1200):
            await self.advance(moment)
            self.motion()
        self.assertEqual(len(self.commands), 1)
        await self.advance(1201)
        self.assertEqual(entity._light_correction.grace_due, 1500)
        await self.advance(1400)
        self.motion()
        self.assertEqual(entity._light_correction.grace_due, 1501)
        await self.advance(1501)
        self.assertEqual(self.commands[-1][:2], (1501, False))
