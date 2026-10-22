"""Exercise automatic wire classification through real controller timers."""

# ruff: noqa: ANN001, ANN201, ANN202, D102, FBT003, INP001, PT009, PT027, SLF001
import asyncio
import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import test_bounded_state_writes as bounded
import test_light_correction as fixtures
import test_light_grace as grace


def capture_automatic_frame(frame):
    """Optionally export the synthetic HA wire frame for offline bridge replay."""
    if os.getenv("FLC_CAPTURE_AUTOMATIC_FRAME") == "1":
        Path(".offline/round2-automatic-frame.json").write_text(
            json.dumps(frame, indent=2) + "\n", encoding="utf-8", newline="\n"
        )


class AutomaticWireTests(unittest.IsolatedAsyncioTestCase):
    """Drive actual grace timers and the API without a local health projection."""

    _entity = fixtures.LightCorrectionTests._entity
    _report = fixtures.LightCorrectionTests._report
    setup_light = fixtures.LightCorrectionTests.setup_light
    advance = fixtures.LightCorrectionTests.advance
    dispatch = fixtures.LightCorrectionTests.dispatch
    setup_grace = grace.LightGraceTests.setup_grace
    observation = grace.LightGraceTests.observation
    arm = grace.LightGraceTests.arm

    async def check_grace_wire(self, *, degraded=False, health_before=False):  # noqa: PLR0915
        entity, _, client, _ = await self.arm()
        protocol = bounded.BoundedProtocolTests.client(self)
        protocol._set_late_contrary_capability(fixtures.CAPABILITY)
        send = protocol._ws.send_json.side_effect
        loop = asyncio.get_running_loop()
        # Controller and API deadlines must share the synthetic monotonic clock.
        clock = patch.object(loop, "time", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        protocol._on_event = lambda event: loop.call_soon(
            entity._handle_property_changed, SimpleNamespace(data=event)
        )
        client.pause_light_correction.side_effect = protocol.pause_light_correction
        sent = not degraded or health_before

        async def reply(payload):
            if payload["cmd"] != "device.set":
                await send(payload)
                return
            self.frames.append(payload)
            self.assertIsNone(entity._light_correction.health_status)
            receipt = {
                "status": (
                    "pending_accepted"
                    if health_before
                    else "cancelled_before_send"
                    if degraded
                    else "acknowledged"
                ),
                "sendAttempted": sent,
                "effect": "unconfirmed",
                "intent": 2,
            }
            if degraded:
                receipt["commandHealth"] = {
                    "status": "command_degraded",
                    "cooldownRemainingMs": 60000,
                    "revision": 1,
                }
            future = protocol._pending[payload["id"]]
            if health_before:
                protocol._dispatch_event(
                    {
                        "event": "commandHealth",
                        "sn": "fixture-device",
                        "property": "light",
                        **receipt["commandHealth"],
                    }
                )
                loop.call_later(0, future.set_result, {"ok": True, "receipt": receipt})
            else:
                future.set_result({"ok": True, "receipt": receipt})

        protocol._ws.send_json.side_effect = reply
        client.set_property.side_effect = protocol.set_property
        await self.advance(901)
        for _ in range(12):
            await asyncio.sleep(0)
        writes = [frame for frame in self.frames if frame["cmd"] == "device.set"]
        self.assertEqual(len(writes), 1)
        self.assertIs(writes[0]["value"], False)
        execution = writes[0]["execution"]
        self.assertEqual(execution["profile"], "bounded-state-v1")
        self.assertIs(execution.get("automatic"), True)
        self.assertNotIn("correction", execution)
        self.assertNotIn("correctionExpiresAtMs", execution)
        controller = entity._light_correction
        self.assertTrue(controller.uncommanded_cycle)
        self.assertEqual(
            (controller.intent, controller.attempts, controller.born),
            (2, int(sent), 901),
        )
        self.assertIsNotNone(controller.expiry)
        if degraded:
            self.assertEqual(controller.health_status, "command_degraded")
            if not sent:
                self.assertIsNone(controller.last_correction)
        if health_before:
            grace.LightGraceTests.emit(self, "matched", intent=2)
            self.assertEqual(controller.state, "matched")
            cancels = [
                frame for frame in self.frames if frame["cmd"] == "boundedStateCancel"
            ]
            self.assertTrue(cancels)
            self.assertTrue(
                all(frame.get("keepObservation") is True for frame in cancels)
            )
        capture_automatic_frame(writes[0])
        controller.stop("closed")

    async def test_grace_timer_marks_automatic_without_changing_bootstrap_watch(self):
        await self.check_grace_wire()

    async def test_grace_maps_unsent_receipt_when_health_delivery_lags(self):
        await self.check_grace_wire(degraded=True)

    async def test_grace_health_before_receipt_preserves_bootstrap_observation(self):
        await self.check_grace_wire(degraded=True, health_before=True)


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
