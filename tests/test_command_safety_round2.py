"""Exercise replacement watchdog commands through the real bounded API."""

# ruff: noqa: ANN001, ANN201, ANN202, D102, FBT003, INP001, PT009, PT027, SLF001
import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace

import test_bounded_state_writes as bounded
import test_light_correction as fixtures


def capture_automatic_frame(frame):
    """Optionally export the synthetic wire frame for offline bridge replay."""
    if os.getenv("FLC_CAPTURE_AUTOMATIC_FRAME") == "1":
        Path(".offline/round2-automatic-frame.json").write_text(
            json.dumps(frame, indent=2) + "\n", encoding="utf-8", newline="\n"
        )


class AutomaticWireTests(fixtures.SafetyFixture, unittest.IsolatedAsyncioTestCase):
    """Drive the thirty-minute cap through real in-memory RPC frames."""

    async def check_watchdog_wire(self, *, degraded=False, early_health=False):
        entity, _, client, _ = await self.activate()
        self.observation(True)
        protocol = bounded.BoundedProtocolTests.client(self)
        protocol._set_late_contrary_capability(fixtures.CAPABILITY)
        send = protocol._ws.send_json.side_effect
        protocol._on_event = lambda event: entity._handle_property_changed(
            SimpleNamespace(data=event)
        )

        async def reply(payload):
            if payload["cmd"] != "device.set":
                await send(payload)
                return
            self.frames.append(payload)
            receipt = {
                "status": "cancelled_before_send" if degraded else "acknowledged",
                "sendAttempted": not degraded,
                "effect": "unconfirmed",
                "intent": 2,
            }
            if degraded:
                receipt["commandHealth"] = {
                    "status": "command_degraded",
                    "cooldownRemainingMs": 60000,
                    "revision": 1,
                }
            if early_health:
                protocol._dispatch_event(
                    {
                        "event": "commandHealth",
                        "sn": "fixture-device",
                        "property": "light",
                        **receipt["commandHealth"],
                    }
                )
            protocol._pending[payload["id"]].set_result(
                {"ok": True, "receipt": receipt}
            )

        protocol._ws.send_json.side_effect = reply
        client.set_property.side_effect = protocol.set_property
        await self.advance(1800)
        self.observation(True)
        await self.drain()
        writes = [frame for frame in self.frames if frame["cmd"] == "device.set"]
        self.assertEqual(len(writes), 1)
        self.assertIs(writes[0]["value"], False)
        execution = writes[0]["execution"]
        self.assertEqual(execution["profile"], "bounded-state-v1")
        self.assertIs(execution["automatic"], True)
        self.assertNotIn("correction", execution)
        self.assertNotIn("correctionExpiresAtMs", execution)
        self.assertEqual(entity._light_correction.attempts, 0 if degraded else 1)
        if degraded:
            self.assertEqual(
                entity._light_correction.health_projection["status"], "command_degraded"
            )
        capture_automatic_frame(writes[0])

    async def test_watchdog_marks_automatic_without_legacy_correction_window(self):
        await self.check_watchdog_wire()

    async def test_unsent_degraded_receipt_does_not_spend_watchdog_attempt(self):
        await self.check_watchdog_wire(degraded=True)

    async def test_health_before_receipt_keeps_unsent_attempt_unspent(self):
        await self.check_watchdog_wire(degraded=True, early_health=True)


class AutomaticProtocolTests(unittest.IsolatedAsyncioTestCase):
    """Keep automatic send permission separate from the correction watch contract."""

    client = bounded.BoundedProtocolTests.client

    async def test_automatic_and_legacy_execution_shapes(self):
        for automatic, correction in ((False, False), (True, False), (False, True)):
            with self.subTest(automatic=automatic, correction=correction):
                client = self.client()
                client._set_late_contrary_capability(fixtures.CAPABILITY)
                await client.set_property(
                    "fixture-device",
                    "light",
                    False,
                    bounded=True,
                    automatic=automatic,
                    correction=correction,
                )
                execution = self.frames[-1]["execution"]
                self.assertEqual(
                    execution.get("automatic", False), automatic or correction
                )
                self.assertEqual(execution.get("correction", False), correction)
                self.assertNotIn("correctionExpiresAtMs", execution)

    async def test_automatic_cannot_fall_back_to_unbounded_legacy_rpc(self):
        client = self.client()
        client._set_late_contrary_capability(fixtures.CAPABILITY)
        with self.assertRaisesRegex(
            fixtures.EufySdkApiClientCommunicationError,
            "^light_correction_unavailable$",
        ):
            await client.set_property("fixture-device", "light", False, automatic=True)
        self.assertFalse(self.frames)
