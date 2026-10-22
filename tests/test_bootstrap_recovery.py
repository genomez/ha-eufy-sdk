"""Keep unsent grace bootstraps separate from SDK correction watches."""

# ruff: noqa: ANN001, ANN003, ANN201, ANN202, D102, FBT003, INP001, PT009, SLF001
# ruff: noqa: PLR2004
import asyncio
import unittest
from datetime import datetime
from unittest.mock import Mock, patch

import test_bounded_state_writes as bounded
import test_light_correction as fixtures
import test_light_grace as grace

from custom_components.eufy_sdk.api import (
    CommandReceipt,
    EufySdkApiClientError,
    _command_receipt,
)


def unsent_bootstrap(*, cooldown_ms=60000):
    """Replay the review's real SDK receipt without a temporary-file dependency."""
    return {
        "status": "cancelled_before_send",
        "sendAttempted": False,
        "effect": "unconfirmed",
        "attempt": 3,
        "intent": 2,
        "superseded": False,
        "commandHealth": {
            "status": "command_degraded",
            "cooldownRemainingMs": cooldown_ms,
            "revision": 1,
        },
    }


class BootstrapRecoveryTests(unittest.IsolatedAsyncioTestCase):
    """Advance actual grace/health timers with controller and real API fixtures."""

    _entity = fixtures.LightCorrectionTests._entity
    _report = fixtures.LightCorrectionTests._report
    setup_light = fixtures.LightCorrectionTests.setup_light
    advance = fixtures.LightCorrectionTests.advance
    dispatch = fixtures.LightCorrectionTests.dispatch
    observation = grace.LightGraceTests.observation
    arm = grace.LightGraceTests.arm
    emit = grace.LightGraceTests.emit

    def setup_grace(self):
        result = grace.LightGraceTests.setup_grace(self)
        loop = asyncio.get_running_loop()
        self.origin = loop.time()
        fake_datetime = Mock(wraps=datetime)
        fake_datetime.now.side_effect = lambda tz: datetime.fromtimestamp(
            2000 + self.now, tz
        )
        for clock in (
            patch.object(loop, "time", side_effect=lambda: self.origin + self.now),
            patch(
                "custom_components.eufy_sdk.light_correction.monotonic",
                side_effect=lambda: self.origin + self.now,
            ),
            patch(
                "custom_components.eufy_sdk.light_correction.datetime", fake_datetime
            ),
            patch(
                "custom_components.eufy_sdk.api.time.time",
                side_effect=lambda: 2000 + self.now,
            ),
        ):
            clock.start()
            self.addCleanup(clock.stop)
        return result

    async def drain(self):
        for _ in range(12):
            await asyncio.sleep(0)

    async def test_controller_retries_bootstrap_without_confirming_absent_watch(self):
        entity, _, client, _ = await self.arm()
        controller = entity._light_correction
        attempts = []
        watch = None

        async def send(_sn, _name, _value, **kwargs):
            nonlocal watch
            attempts.append((self.now, kwargs))
            self.assertTrue(kwargs["_send_guard"]())
            if len(attempts) == 1:
                return _command_receipt(unsent_bootstrap())
            watch = len(attempts) + 1
            return CommandReceipt("acknowledged", True, "unconfirmed", intent=watch)

        async def confirm(_sn, intent):
            if watch is None or intent != watch:
                message = "late_contrary_unavailable"
                raise EufySdkApiClientError(message)
            return {
                "desiredValue": False,
                "observedValue": True,
                "confirmed": True,
                "recoveryBusy": False,
            }

        client.set_property.side_effect = send
        client.confirm_light.side_effect = confirm
        await self.advance(901)
        original = (controller.born, controller.window_until, controller.expiry)
        self.assertEqual((controller.attempts, controller.write_sent), (0, False))
        self.assertIsNone(controller.last_correction)
        await self.advance(960.999)
        self.assertEqual(len(attempts), 1)
        await self.advance(962)
        self.assertEqual(len(attempts), 2, "unsent bootstrap has no watch to confirm")
        self.assertEqual(
            (controller.born, controller.window_until, controller.expiry), original
        )
        self.assertEqual(controller.confirmation_reads, 0)
        client.confirm_light.assert_not_awaited()
        self.assertEqual((controller.intent, controller.attempts, watch), (3, 1, 3))
        self.assertFalse(controller.bootstrap)
        for _, kwargs in attempts:
            self.assertIs(kwargs["automatic"], True)
            self.assertIs(kwargs["correction"], False)
        # Remaining attempts use the real watch, retain spacing and stop at three.
        await self.advance(1400)
        self.assertEqual([at for at, _ in attempts], [901, 961, 1006, 1186])
        self.assertEqual(controller.attempts, 3)
        self.assertTrue(all(kwargs["correction"] for _, kwargs in attempts[2:]))
        controller.stop("closed")

    async def check_api_recovery(self, *, cooldown_ms=60000, expire_during_clock=False):  # noqa: PLR0915
        entity, _, client, _ = await self.arm()
        controller = entity._light_correction
        protocol = bounded.BoundedProtocolTests.client(self)
        protocol._set_late_contrary_capability(fixtures.CAPABILITY)
        default = protocol._ws.send_json.side_effect
        writes, confirmations = [], []
        watch = None

        async def reply(payload):
            nonlocal watch
            if payload["cmd"] == "device.set":
                writes.append(payload)
                if len(writes) == 1:
                    receipt = unsent_bootstrap(cooldown_ms=cooldown_ms)
                else:
                    watch = 3
                    receipt = {
                        "status": "acknowledged",
                        "sendAttempted": True,
                        "effect": "unconfirmed",
                        "intent": watch,
                    }
                protocol._pending[payload["id"]].set_result(
                    {"ok": True, "receipt": receipt}
                )
            elif payload["cmd"] == "device.confirmLight":
                confirmations.append(payload)
                valid = watch is not None and payload["intent"] == watch
                protocol._pending[payload["id"]].set_result(
                    {
                        "ok": valid,
                        "code": "light_confirmation_failed",
                        "confirmation": {
                            "intent": watch,
                            "desiredValue": False,
                            "observedValue": False,
                            "confirmed": True,
                            "recoveryBusy": False,
                            "ageMs": 0,
                        },
                    }
                )
            else:
                if (
                    expire_during_clock
                    and writes
                    and payload["cmd"] == "boundedStateClock"
                ):
                    # Clock preparation outlives the original authority before dispatch.
                    self.now = 1501
                await default(payload)

        protocol._ws.send_json.side_effect = reply
        client.set_property.side_effect = protocol.set_property
        client.confirm_light.side_effect = protocol.confirm_light
        client.pause_light_correction.side_effect = protocol.pause_light_correction
        await self.advance(901)
        await self.drain()
        self.assertEqual(
            (len(writes), controller.attempts, controller.write_sent), (1, 0, False)
        )
        original = (controller.born, controller.window_until, controller.expiry)
        reads = controller.confirmation_reads
        recovery_at = 901 + cooldown_ms / 1000
        await self.advance(recovery_at - 0.001)
        self.assertEqual(len(writes), 1)
        await self.advance(max(1502 if expire_during_clock else 962, recovery_at + 0.1))
        await self.drain()
        self.assertEqual(controller.health_status, "healthy")
        self.assertEqual(controller.born, original[0])
        self.assertEqual(controller.window_until, original[1])
        if recovery_at >= 1501 or expire_during_clock:
            self.assertEqual(
                len(writes), 1, "expired bootstrap must never renew authority"
            )
            self.assertIsNone(watch)
            self.assertEqual(confirmations, [])
            controller.stop("closed")
            return
        self.assertEqual(len(writes), 2, "recovery must establish its first watch")
        self.assertEqual(controller.expiry, original[2])
        self.assertEqual(controller.confirmation_reads, reads)
        self.assertEqual(confirmations, [])
        self.assertEqual((controller.intent, controller.attempts, watch), (3, 1, 3))
        self.assertFalse(controller.bootstrap)
        for frame in writes:
            execution = frame["execution"]
            self.assertIs(execution["automatic"], True)
            self.assertNotIn("correction", execution)
            self.assertNotIn("correctionExpiresAtMs", execution)
        execution = writes[-1]["execution"]
        authority_ms = (2000 + 1501) * 1000
        self.assertLessEqual(execution["callerExpiresAtMs"], authority_ms)
        self.assertLessEqual(
            execution["remainingBudgetMs"], (1501 - recovery_at) * 1000
        )
        if recovery_at < 1500:
            # A fresh confirmation now succeeds under the sent bootstrap's watch.
            await self.advance(recovery_at + 45)
            await self.drain()
            self.assertEqual(len(confirmations), 1)
            self.assertEqual(confirmations[0]["intent"], watch)
            self.assertEqual(controller.state, "matched")
            self.assertEqual(len(writes), 2)
        controller.stop("closed")

    async def test_real_api_replays_unsent_receipt_then_establishes_watch(self):
        await self.check_api_recovery()

    async def test_real_api_late_bootstrap_carries_only_remaining_authority(self):
        await self.check_api_recovery(cooldown_ms=599000)

    async def test_real_api_recovery_after_authority_expiry_sends_nothing(self):
        await self.check_api_recovery(cooldown_ms=600000)

    async def test_real_api_authority_expiring_during_clock_sends_nothing(self):
        await self.check_api_recovery(cooldown_ms=599000, expire_during_clock=True)

    async def test_sent_bootstrap_keeps_normal_correction_watch(self):
        entity, _, client, _ = await self.arm()
        await self.advance(901)
        controller = entity._light_correction
        self.assertFalse(controller.bootstrap)
        self.assertTrue(controller.write_sent)
        self.assertEqual(controller.attempts, 1)
        client.confirm_light.assert_not_awaited()
        self.emit("matched")
        await self.advance(962)
        self.assertEqual(controller.state, "matched")
        self.assertEqual(len(self.commands), 2)  # Original explicit OFF and bootstrap.
        client.confirm_light.assert_not_awaited()
        controller.stop("closed")
