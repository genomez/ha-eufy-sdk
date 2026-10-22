"""Regression coverage for OFF-only correction and SDK-owned command health."""

# ruff: noqa: ANN001, ANN201, D102, FBT003, INP001, PT009, SLF001
import unittest
from types import SimpleNamespace

import test_light_correction as fixtures

from custom_components.eufy_sdk.api import CommandReceipt


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

    async def test_back_left_on_timeout_never_corrects_after_demand_ends(self):
        entity, _, client, _ = self.setup_light(desired=True)
        await entity.write(True)
        entity._light_correction.receipt(
            CommandReceipt(
                "ack_timeout",
                True,
                "unknown",
                intent=1,
            )
        )
        self.emit()
        await self.advance(188)
        self._report(entity, False)
        await self.advance(320)
        self.assertEqual([value for _, value, _ in self.commands], [True])
        client.confirm_light.assert_not_awaited()
        self.assertEqual(
            entity.extra_state_attributes["light_correction_suppression_reason"],
            "on_observation_only",
        )
        self.emit("verification_exhausted")
        await self.advance(600)
        self.notification.assert_not_called()

    async def test_front_yard_match_then_off_does_not_rearm_old_on(self):
        entity, _, client, _ = self.setup_light(desired=True)
        await entity.write(True)
        entity._light_correction.receipt(
            CommandReceipt(
                "pending_accepted",
                True,
                "unconfirmed",
                intent=1,
            )
        )
        self.emit()
        await self.advance(140)
        self.emit("matched")
        await self.advance(163)
        self.emit()
        await self.advance(317)
        self.assertEqual([value for _, value, _ in self.commands], [True])
        client.confirm_light.assert_not_awaited()
        await entity.async_turn_off()
        self.assertEqual([value for _, value, _ in self.commands], [True, False])
        self.assertIs(entity._light_correction.desired, False)
        await self.advance(600)
        self.notification.assert_not_called()

    async def test_off_correction_keeps_exact_bounds(self):
        entity, _, client, _ = self.setup_light()
        await entity.write(False)
        self.emit()
        await self.advance(600)
        self.assertEqual(
            [(at, value) for at, value, _ in self.commands],
            [(0, False), (15, False), (60, False), (300, False)],
        )
        self.assertEqual(entity._light_correction.attempts, 3)
        self.assertLessEqual(client.confirm_light.await_count, 16)
