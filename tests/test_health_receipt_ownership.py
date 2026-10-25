"""Exercise asynchronous health delivery during a bounded safety retry."""

# ruff: noqa: ANN001, ANN201, ANN202, D102, FBT003, INP001, PT009, SLF001
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import test_bounded_state_writes as bounded
import test_light_correction as fixtures


class HealthReceiptOwnershipTests(
    fixtures.SafetyFixture, unittest.IsolatedAsyncioTestCase
):
    """Use real API pending RPCs and asynchronously delivered entity callbacks."""

    async def check_health_before_receipt(
        self, *, sent, early_match=False, early_motion=False
    ):
        entity, _, client, _ = await self.activate()
        await entity.write(False)
        protocol = bounded.BoundedProtocolTests.client(self)
        protocol._set_late_contrary_capability(fixtures.CAPABILITY)
        loop = asyncio.get_running_loop()
        protocol._on_event = lambda event: loop.call_soon(
            entity._handle_property_changed, SimpleNamespace(data=event)
        )
        send = protocol._ws.send_json.side_effect
        delivered = []
        writes = []

        async def reply(payload):
            if payload["cmd"] != "device.set":
                await send(payload)
                return
            writes.append(payload)
            health = {
                "status": "command_degraded",
                "cooldownRemainingMs": 60000,
                "revision": 1,
            }
            protocol._dispatch_event(
                {
                    "event": "commandHealth",
                    "sn": "fixture-device",
                    "property": "light",
                    **health,
                }
            )
            future = protocol._pending[payload["id"]]

            def terminal():
                if early_motion:
                    self.motion()
                if early_match:
                    self.observation(False)
                if not future.done():
                    delivered.append(True)
                    future.set_result(
                        {
                            "ok": True,
                            "receipt": {
                                "status": "pending_accepted"
                                if sent
                                else "cancelled_before_send",
                                "sendAttempted": sent,
                                "effect": "unconfirmed",
                                "intent": 2,
                                "commandHealth": health,
                            },
                        }
                    )

            loop.call_soon(terminal)

        protocol._ws.send_json.side_effect = reply
        client.set_property.side_effect = protocol.set_property
        with patch(
            "custom_components.eufy_sdk.api.time.time", lambda: 10000 + self.now
        ):
            await self.advance(60)
        controller = entity._light_correction
        self.assertEqual(delivered, [True], "health must not cancel the terminal RPC")
        self.assertEqual(controller.attributes["command_health"], "command_degraded")
        self.assertEqual(controller.sdk_intent, 2)
        self.assertEqual(controller.attempts, 1 + int(sent))
        self.assertEqual(controller.recovery_pending, sent)
        self.assertEqual(controller.recovery_intent, 2 if sent else None)
        self.assertFalse(protocol._pending)
        self.assertFalse(protocol._bounded_requests)
        self.assertEqual(len(writes), 1)
        await self.advance(119)
        self.assertEqual(len(writes), 1, "health cooldown must prevent another send")
        if not early_match:
            self.assertIs(controller.pending, False)
            self.observation(False)
        self.assertIsNone(controller.pending)
        await self.advance(180)
        self.assertEqual(len(writes), 1, "fresh camera OFF must prevent retries")
        self.assertFalse(controller.recovery_pending)
        self.assertIsNone(controller.task)

    async def test_sent_health_before_receipt_retains_sdk_recovery_ownership(self):
        await self.check_health_before_receipt(sent=True)

    async def test_unsent_health_receipt_does_not_count_attempt_or_claim_recovery(self):
        await self.check_health_before_receipt(sent=False)

    async def test_camera_match_before_terminal_receipt_is_preserved(self):
        await self.check_health_before_receipt(sent=True, early_match=True)

    async def test_degraded_unsent_receipt_preserves_early_match_despite_motion(self):
        await self.check_health_before_receipt(
            sent=False, early_match=True, early_motion=True
        )
