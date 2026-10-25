"""Exercise the long-on watchdog using fresh RTC reports and fake time."""

# ruff: noqa: ANN001, ANN201, ANN202, D102, FBT003, INP001, PT009, PT027, SLF001
import asyncio
import unittest

import test_light_correction as fixtures


class LightGraceTests(fixtures.SafetyFixture, unittest.IsolatedAsyncioTestCase):
    """Retain shared fixture entry points while testing the replacement watchdog."""

    setup_grace = fixtures.SafetyFixture.setup_light

    async def arm(self):
        result = self.setup_grace()
        await self.entity._light_correction.async_added()
        self.observation(True, telemetry={"motion": False})
        return result

    async def reports_until(self, target, *, telemetry=None):
        for moment in range(int(self.now) + 60, target + 1, 60):
            await self.advance(moment)
            self.observation(True, telemetry=telemetry or {})
            await self.drain()

    async def test_startup_on_uses_first_fresh_camera_report(self):
        entity, _, _, _ = await self.activate()
        self.assertIsNone(entity._light_correction.on_since)
        await self.advance(100)
        self.observation(True, telemetry={"motion": False})
        self.assertEqual(entity._light_correction.on_since, 100)
        await self.reports_until(940, telemetry={"motion": False})
        self.assertEqual(self.commands, [])
        await self.advance(1000)
        self.observation(True, telemetry={"motion": False})
        await self.drain()
        self.assertEqual(
            [(at, value) for at, value, _ in self.commands], [(1000, False)]
        )

    async def test_quiet_watchdog_waits_fifteen_minutes(self):
        await self.arm()
        await self.reports_until(840, telemetry={"motion": False})
        await self.advance(899)
        self.assertEqual(self.commands, [])
        await self.advance(900)
        self.observation(True, telemetry={"motion": False})
        await self.drain()
        self.assertEqual(
            [(at, value) for at, value, _ in self.commands], [(900, False)]
        )

    async def test_motion_extends_quiet_but_not_thirty_minute_cap(self):
        await self.activate()
        self.observation(True, telemetry={"motion": True})
        await self.reports_until(1740, telemetry={"motion": True})
        self.assertEqual(self.commands, [])
        await self.advance(1800)
        self.observation(True, telemetry={"motion": True})
        await self.drain()
        self.assertEqual(
            [(at, value) for at, value, _ in self.commands], [(1800, False)]
        )

    async def test_detection_silence_without_rtc_motion_uses_quiet_branch(self):
        await self.activate()
        self.observation(True)
        await self.reports_until(840)
        self.assertEqual(self.commands, [])
        await self.advance(900)
        self.observation(True)
        await self.drain()
        self.assertEqual(
            [(at, value) for at, value, _ in self.commands], [(900, False)]
        )

    async def test_three_minute_native_motion_cycle_has_no_alert(self):
        await self.activate()
        self.observation(True, telemetry={"motion": True})
        await self.advance(180)
        self.observation(False, telemetry={"motion": False})
        await self.advance(300)
        self.observation(False, telemetry={"motion": False})
        await self.drain()
        self.assertEqual(self.commands, [])
        self.notification.assert_not_called()

    async def test_other_camera_detection_does_not_count_as_this_camera_motion(self):
        entity, _, _, _ = await self.activate()
        self.motion(device="other-fixture")
        self.assertIsNone(entity._light_correction.last_motion)
        self.motion(kind="personDetected")
        self.assertEqual(entity._light_correction.last_motion, self.now)

    async def test_stale_on_report_cannot_authorize_watchdog(self):
        entity, _, _, _ = await self.activate()
        self.observation(True, telemetry={"motion": False})
        await self.advance(1800)
        self.assertEqual(self.commands, [])
        self.assertEqual(entity._light_correction.state, "protection gap")
        self.observation(True)
        await self.drain()
        self.assertEqual(len(self.commands), 1)

    async def test_reconnect_requires_new_report_and_restarts_on_duration(self):
        entity, _, _, _ = await self.activate()
        self.observation(True)
        await self.advance(1700)
        entity._light_correction.reset_observations()
        self.assertIsNone(entity._light_correction.on_since)
        self.observation(True)
        self.assertEqual(entity._light_correction.on_since, 1700)
        await self.advance(1800)
        self.observation(True)
        await self.drain()
        self.assertEqual(self.commands, [])


class GraceProtocolTests(unittest.IsolatedAsyncioTestCase):
    """Use actual bounded RPC plumbing with an in-memory transport."""

    client = fixtures.LightCorrectionProtocolTests.client

    async def test_bootstrap_guard_blocks_dispatch_after_clock_exchange(self):
        client = self.client()
        client._set_late_contrary_capability(fixtures.CAPABILITY)
        previous = client._ws.send_json.side_effect
        allowed = True

        async def clock_reply(payload):
            nonlocal allowed
            await previous(payload)
            allowed = False

        client._ws.send_json.side_effect = clock_reply
        with self.assertRaisesRegex(
            fixtures.EufySdkApiClientCommunicationError,
            "^light_correction_paused_before_send$",
        ):
            await client.set_property(
                "fixture",
                "light",
                False,
                bounded=True,
                _send_guard=lambda: allowed,
            )
        self.assertEqual([frame["cmd"] for frame in self.frames], ["boundedStateClock"])
        self.assertEqual(client._correction_requests, {})

    async def test_bootstrap_cancel_uses_existing_bounded_cancel_protocol(self):
        client = self.client()
        client._set_late_contrary_capability(fixtures.CAPABILITY)
        previous = client._ws.send_json.side_effect
        submitted = asyncio.Event()
        request_id = None

        async def reply(payload):
            nonlocal request_id
            if payload["cmd"] == "boundedStateClock":
                await previous(payload)
            elif payload["cmd"] == "device.set":
                self.frames.append(payload)
                self.assertNotIn("correction", payload["execution"])
                request_id = payload["id"]
                submitted.set()
            elif payload["cmd"] == "boundedStateCancel":
                self.frames.append(payload)
                self.assertNotIn("keepObservation", payload)
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
                _send_guard=lambda: True,
            )
        )
        await submitted.wait()
        client.pause_light_correction("fixture")
        receipt = await task
        self.assertEqual(receipt.status, "cancelled_before_send")
        self.assertEqual(client._correction_requests, {})
        self.assertEqual(
            len([f for f in self.frames if f["cmd"] == "boundedStateCancel"]), 1
        )
