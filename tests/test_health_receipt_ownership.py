"""Exercise asynchronous command health before passive-watch receipt transfer."""

# ruff: noqa: ANN001, ANN201, ANN202, D102, FBT003, INP001, PT009, SLF001
import asyncio
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

import test_bounded_state_writes as bounded
import test_light_correction as fixtures


class HealthReceiptOwnershipTests(unittest.IsolatedAsyncioTestCase):
    """Use the real API pending RPC and asynchronously delivered HA callbacks."""

    _entity = fixtures.LightCorrectionTests._entity
    _report = fixtures.LightCorrectionTests._report
    setup_light = fixtures.LightCorrectionTests.setup_light
    advance = fixtures.LightCorrectionTests.advance
    dispatch = fixtures.LightCorrectionTests.dispatch
    emit = fixtures.LightCorrectionTests.emit

    async def check_health_before_receipt(  # noqa: PLR0915
        self, *, sent, early_match=False, early_motion=False
    ):
        entity, _, client, _ = self.setup_light()
        protocol = bounded.BoundedProtocolTests.client(self)
        protocol._set_late_contrary_capability(fixtures.CAPABILITY)
        loop = asyncio.get_running_loop()
        origin = loop.time()
        fake_datetime = Mock(wraps=datetime)
        fake_datetime.now.side_effect = lambda tz: datetime.fromtimestamp(
            2000 + self.now, tz
        )
        protocol._on_event = lambda event: loop.call_soon(
            entity._handle_property_changed, SimpleNamespace(data=event)
        )
        send = protocol._ws.send_json.side_effect
        delivered = []

        async def reply(payload):
            if payload["cmd"] != "device.set":
                await send(payload)
                return
            self.frames.append(payload)
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
                    entity._light_correction.motion()
                    self.emit("contrary", intent=2)
                if early_match:
                    self.emit("matched", intent=2)
                if not future.done():
                    delivered.append(True)
                    future.set_result(
                        {
                            "ok": True,
                            "receipt": {
                                "status": (
                                    "pending_accepted"
                                    if sent
                                    else "cancelled_before_send"
                                ),
                                "sendAttempted": sent,
                                "effect": "unconfirmed",
                                "intent": 2,
                                "commandHealth": health,
                            },
                        }
                    )

            loop.call_later(0, terminal)

        protocol._ws.send_json.side_effect = reply
        client.pause_light_correction.side_effect = protocol.pause_light_correction
        with (
            patch.object(loop, "time", side_effect=lambda: origin + self.now),
            patch(
                "custom_components.eufy_sdk.light_correction.monotonic",
                side_effect=lambda: origin + self.now,
            ),
            patch(
                "custom_components.eufy_sdk.light_correction.datetime", fake_datetime
            ),
            patch(
                "custom_components.eufy_sdk.api.time.time",
                side_effect=lambda: 2000 + self.now,
            ),
        ):
            await entity.write(False)
            controller = entity._light_correction
            original_born = controller.born
            original_window = controller.window_until
            self.emit()
            client.set_property.side_effect = protocol.set_property
            await self.advance(15)
            for _ in range(12):
                await asyncio.sleep(0)
            self.assertEqual(controller.health_status, "command_degraded")
            self.assertEqual(delivered, [True], "bounded terminal RPC must survive")
            if early_motion:
                self.assertEqual(controller.state, "matched")
            self.assertEqual(controller.intent, 2, "receipt transfers watch ownership")
            self.assertEqual(controller.attempts, int(sent))
            self.assertEqual(controller.write_sent, sent)
            self.assertEqual(controller.born, original_born)
            self.assertEqual(controller.window_until, original_window)
            if not sent:
                self.assertIsNone(controller.last_correction)
            if not early_match:
                self.emit("matched", intent=2)
            self.assertEqual(controller.state, "matched")
            self.assertFalse(controller.transition)
            self.assertIsNone(controller.timer)
            self.assertIsNotNone(controller.expiry)
            cancels = [
                frame for frame in self.frames if frame["cmd"] == "boundedStateCancel"
            ]
            self.assertTrue(cancels)
            self.assertTrue(
                all(frame.get("keepObservation") is True for frame in cancels)
            )
            self.assertFalse(protocol._pending)
            self.assertFalse(protocol._bounded_requests)
            self.notification.assert_not_called()
            if early_motion:
                await self.advance(616)
                self.assertEqual(
                    len(
                        [frame for frame in self.frames if frame["cmd"] == "device.set"]
                    ),
                    1,
                    "buffered match must prevent a new grace OFF after cooldown",
                )
            controller.stop("closed")

    async def test_sent_health_before_receipt_preserves_passive_match(self):
        await self.check_health_before_receipt(sent=True)

    async def test_unsent_health_receipt_refunds_and_preserves_passive_match(self):
        await self.check_health_before_receipt(sent=False)

    async def test_match_before_terminal_receipt_is_replayed_after_health(self):
        await self.check_health_before_receipt(sent=True, early_match=True)

    async def test_degraded_receipt_replays_match_before_motion_handoff(self):
        await self.check_health_before_receipt(
            sent=False, early_match=True, early_motion=True
        )
