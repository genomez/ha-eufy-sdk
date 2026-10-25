"""Exercise command-health waits without treating optimistic state as evidence."""

# ruff: noqa: ANN001, ANN201, ANN202, D102, FBT003, INP001, PT009, PT027, SLF001
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import test_aligned_state as aligned
import test_light_correction as fixtures
from aiohttp import WSMsgType

from custom_components.eufy_sdk.api import (
    _command_receipt,
    command_health,
    command_health_event,
)


class CommandSafetyTests(fixtures.SafetyFixture, unittest.IsolatedAsyncioTestCase):
    """Keep health admission separate from explicit commands and retry attempts."""

    async def test_degraded_health_waits_without_spending_retry_attempts(self):
        entity, _, _, _ = await self.activate()
        await entity.write(False)
        self.health(remaining=120000)
        await self.advance(119)
        self.assertEqual([at for at, _, _ in self.commands], [0])
        self.assertEqual(entity._light_correction.attempts, 1)
        await self.advance(120)
        self.assertEqual([at for at, _, _ in self.commands], [0, 120])

    async def test_offline_camera_waits_then_retries_when_back(self):
        entity, _, _, _ = await self.activate()
        await entity.write(False)
        entity.device["cameraHealth"] = {"connectivity": "offline"}
        await self.advance(300)
        self.assertEqual([at for at, _, _ in self.commands], [0])
        entity.device["cameraHealth"] = {"connectivity": "online"}
        entity._light_correction.check_policy()
        await self.drain()
        self.assertEqual([at for at, _, _ in self.commands], [0, 300])

    async def test_explicit_request_bypasses_degraded_automatic_wait(self):
        entity, _, _, _ = await self.activate()
        self.health()
        await entity.write(True)
        self.assertEqual([value for _, value, _ in self.commands], [True])
        self.assertEqual(entity._light_correction.explicit_busy, 0)
        await self.advance(120)
        self.assertEqual(len(self.commands), 1)

    async def test_healthy_event_cannot_override_pause_or_new_on_intent(self):
        entity, coordinator, _, _ = await self.activate()
        coordinator.config_entry.options[fixtures.CONF_LIGHT_CORRECTION_PAUSE] = (
            "switch.pause"
        )
        await entity.write(False)
        self.health()
        await self.advance(120)
        self.paused = True
        self.health("healthy", 0, revision=2)
        await self.drain()
        self.assertEqual(len(self.commands), 1)
        self.binding("on")
        self.paused = False
        entity._light_correction.check_policy()
        await self.drain()
        self.assertEqual(len(self.commands), 1)

    async def test_duplicate_projection_does_not_extend_cooldown(self):
        entity, _, _, _ = await self.activate()
        self.health(remaining=120000)
        await self.advance(60)
        self.health(remaining=120000)
        self.assertEqual(entity._light_correction.health_until, 120)

    async def test_older_healthy_projection_cannot_clear_newer_degraded(self):
        entity, _, _, _ = await self.activate()
        self.health(remaining=120000, revision=2)
        self.health("healthy", 0, revision=1)
        self.assertEqual(entity._light_correction.health_until, 120)

    async def test_explicit_send_failure_always_releases_busy_reservation(self):
        entity, _, client, _ = await self.activate()
        client.get_properties.side_effect = fixtures.EufySdkApiClientCommunicationError(
            "synthetic"
        )
        with self.assertRaises(fixtures.EufySdkApiClientCommunicationError):
            await entity.write(False)
        self.assertEqual(entity._light_correction.explicit_busy, 0)

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
