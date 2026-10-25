"""Keep unsent safety retries separate from SDK recovery ownership."""

# ruff: noqa: ANN001, ANN002, ANN003, ANN201, ANN202, D102, FBT003
# ruff: noqa: INP001, PT009, SLF001, PLR2004
import unittest
from unittest.mock import patch

import test_bounded_state_writes as bounded
import test_light_correction as fixtures

from custom_components.eufy_sdk.api import _command_receipt


def unsent_bootstrap(*, cooldown_ms=60000):
    """Replay a synthetic unsent degraded receipt without a correction watch."""
    return {
        "status": "cancelled_before_send",
        "sendAttempted": False,
        "effect": "unconfirmed",
        "intent": 2,
        "commandHealth": {
            "status": "command_degraded",
            "cooldownRemainingMs": cooldown_ms,
            "revision": 1,
        },
    }


class BootstrapRecoveryTests(fixtures.SafetyFixture, unittest.IsolatedAsyncioTestCase):
    """Exercise replacement OFF retries and real bounded dispatch guards."""

    async def test_unsent_retry_waits_for_health_without_claiming_sdk_recovery(self):
        entity, _, client, _ = await self.activate()
        await entity.write(False)
        send = client.set_property.side_effect
        calls = []

        async def reply(*args, **kwargs):
            calls.append(self.now)
            if len(calls) == 1:
                self.assertTrue(kwargs["_send_guard"]())
                return _command_receipt(unsent_bootstrap())
            return await send(*args, **kwargs)

        client.set_property.side_effect = reply
        controller = entity._light_correction
        await self.advance(60)
        self.assertEqual(controller.attempts, 1)
        self.assertIs(controller.pending, False)
        self.assertFalse(controller.recovery_pending)
        await self.advance(119)
        self.assertEqual(calls, [60])
        await self.advance(120)
        self.assertEqual(calls, [60, 120])
        self.assertEqual(controller.attempts, 2)
        self.assertIsNone(controller.recovery_intent)

    async def check_api_recovery(self, *, cooldown_ms=60000, clock_change=None):
        entity, _, client, _ = await self.activate()
        await entity.write(False)
        controller = entity._light_correction
        protocol = bounded.BoundedProtocolTests.client(self)
        protocol._set_late_contrary_capability(fixtures.CAPABILITY)
        send = protocol._ws.send_json.side_effect
        writes = []
        clocks = []

        async def reply(payload):
            if payload["cmd"] == "device.set":
                writes.append(payload)
                receipt = (
                    unsent_bootstrap(cooldown_ms=cooldown_ms)
                    if len(writes) == 1
                    else {
                        "status": "acknowledged",
                        "sendAttempted": True,
                        "effect": "unconfirmed",
                        "intent": 3,
                    }
                )
                protocol._pending[payload["id"]].set_result(
                    {"ok": True, "receipt": receipt}
                )
            else:
                if payload["cmd"] == "boundedStateClock":
                    clocks.append(payload)
                    if len(clocks) == 2 and clock_change:
                        clock_change(controller)
                await send(payload)

        protocol._ws.send_json.side_effect = reply
        client.set_property.side_effect = protocol.set_property
        with patch(
            "custom_components.eufy_sdk.api.time.time", lambda: 10000 + self.now
        ):
            await self.advance(60)
            self.assertEqual(len(writes), 1)
            self.assertEqual(controller.attempts, 1)
            self.assertFalse(controller.recovery_pending)
            resume = 60 + cooldown_ms / 1000
            await self.advance(resume - 1)
            self.assertEqual(len(writes), 1)
            await self.advance(resume)
            self.assertEqual(len(clocks), 2)
        self.assertFalse(protocol._pending)
        self.assertFalse(protocol._bounded_requests)
        return controller, writes

    async def test_real_api_retries_unsent_receipt_without_old_correction_watch(self):
        controller, writes = await self.check_api_recovery()
        self.assertEqual(len(writes), 2)
        self.assertEqual(controller.attempts, 2)
        self.assertEqual(controller.sdk_intent, 3)
        for frame in writes:
            execution = frame["execution"]
            self.assertIs(execution["automatic"], True)
            self.assertNotIn("correction", execution)
            self.assertNotIn("correctionExpiresAtMs", execution)
            self.assertLessEqual(execution["remainingBudgetMs"], 80000)

    async def test_real_api_long_health_wait_preserves_unconfirmed_off_retry(self):
        controller, writes = await self.check_api_recovery(cooldown_ms=600000)
        self.assertEqual(len(writes), 2)
        self.assertIs(controller.pending, False)
        self.assertEqual(controller.attempts, 2)
        self.assertGreater(controller.retry_due, self.now)
        with patch(
            "custom_components.eufy_sdk.api.time.time", lambda: 10000 + self.now
        ):
            await self.advance(1860)
        self.assertGreater(len(writes), 3)
        self.assertIs(controller.pending, False)

    async def test_real_api_intent_flip_during_clock_prevents_retry_dispatch(self):
        def change(_controller):
            self.binding("on")

        controller, writes = await self.check_api_recovery(clock_change=change)
        self.assertEqual(len(writes), 1)
        self.assertIsNone(controller.pending)
        self.assertIs(controller.intent(), True)

    async def test_real_api_new_health_during_clock_prevents_retry_dispatch(self):
        def change(controller):
            controller.health(
                {
                    "status": "command_degraded",
                    "cooldownRemainingMs": 120000,
                    "revision": 2,
                }
            )

        controller, writes = await self.check_api_recovery(clock_change=change)
        self.assertEqual(len(writes), 1)
        self.assertEqual(controller.attempts, 1)
        self.assertGreater(controller.health_until, self.now)

    async def test_sent_retry_requires_fresh_camera_report_not_passive_match(self):
        entity, _, _, _ = await self.activate()
        await entity.write(False)
        await self.advance(60)
        controller = entity._light_correction
        self.emit("matched", observed=False)
        self.assertIs(controller.pending, False)
        self.observation(False)
        self.assertIsNone(controller.pending)
        await self.advance(400)
        self.assertEqual(len(self.commands), 2)
