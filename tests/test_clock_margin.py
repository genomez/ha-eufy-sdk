"""Regress clock quantization, refusal provenance and invocation-owned retries."""

# ruff: noqa: ANN001, ANN002, ANN003, ANN201, ANN202, D102, FBT003
# ruff: noqa: INP001, PT009, PT027, SLF001

import asyncio
import unittest
from unittest.mock import patch

import test_bounded_state_writes as bounded
from test_light_correction import SafetyFixture

from custom_components.eufy_sdk.api import (
    EufySdkApiClientError,
    _bounded_clock_uncertainty,
)

ACCEPTED = bounded.ACCEPTED


class ClockMarginTests(unittest.IsolatedAsyncioTestCase):
    """Exercise the real RPC boundary with synthetic bridge replies."""

    client = bounded.BoundedProtocolTests.client

    async def test_reserved_v1_declaration_remains_compatible_with_old_bridge(self):
        client = self.client()
        with patch(
            "custom_components.eufy_sdk.api.time.time", side_effect=[1000, 1000.04]
        ):
            await client.set_property("fixture", "light", False, bounded=True)
        execution = self.frames[-1]["execution"]
        handshake = self.frames[0]
        round_trip = execution["callerReceivedAtMs"] - handshake["callerSentAtMs"]
        # The df4259f bridge's unchanged v1 admission uses the unreserved exchange.
        legacy_uncertainty = round_trip / 2 + 1
        self.assertEqual(legacy_uncertainty, 21)
        self.assertEqual(execution["clockUncertaintyMs"], 28)
        self.assertGreaterEqual(execution["clockUncertaintyMs"], legacy_uncertainty)
        self.assertLessEqual(execution["clockUncertaintyMs"], 1000)
        self.assertEqual(len(self.frames), 2)

    def test_submillisecond_declaration_and_processing_envelope_boundary(self):
        clock = {
            "version": 1,
            "callerSentAtMs": 100000,
            "bridgeReceivedAtMs": 100000,
            "bridgeSentAtMs": 100002,
            "clockToken": "fixture",
        }
        with self.assertRaisesRegex(EufySdkApiClientError, "command_clock_unavailable"):
            _bounded_clock_uncertainty(clock, 100000, 100000.5, 0.5)
        self.assertEqual(_bounded_clock_uncertainty(clock, 100000, 100001, 1), 8)

    async def test_submillisecond_exchange_clamps_and_reserves(self):
        client = self.client()
        original = client._ws.send_json.side_effect

        async def reply(payload):
            if payload["cmd"] == "boundedStateClock":
                self.frames.append(payload)
                client._pending[payload["id"]].set_result(
                    {
                        "ok": True,
                        "version": 1,
                        "callerSentAtMs": payload["callerSentAtMs"],
                        "bridgeReceivedAtMs": 1000000,
                        "bridgeSentAtMs": 1000001,
                        "clockToken": "fractional",
                    }
                )
            else:
                await original(payload)

        client._ws.send_json.side_effect = reply
        with patch(
            "custom_components.eufy_sdk.api.time.time", side_effect=[1000, 1000.0006]
        ):
            await client.set_property("fixture", "light", False, bounded=True)
        self.assertEqual(self.frames[-1]["execution"]["clockUncertaintyMs"], 8)

    async def refusal(
        self, *, value=False, flag=False, persistent=False, guard=None, on_refusal=None
    ):
        client = self.client()
        original = client._ws.send_json.side_effect
        writes = []

        async def reply(payload):
            if payload["cmd"] == "device.set":
                self.frames.append(payload)
                writes.append(payload)
                if len(writes) == 1 or persistent:
                    if on_refusal:
                        on_refusal(client)
                    client._pending[payload["id"]].set_result(
                        {
                            "ok": False,
                            "code": "command_clock_unavailable",
                            **({} if flag is None else {"sendAttempted": flag}),
                        }
                    )
                else:
                    client._pending[payload["id"]].set_result(
                        {"ok": True, "receipt": ACCEPTED}
                    )
            else:
                await original(payload)
                if payload["cmd"] == "boundedStateClock":
                    client._pending[payload["id"]].result()["clockToken"] = (
                        f"clock-{payload['id']}"
                    )

        client._ws.send_json.side_effect = reply
        error = None
        try:
            await client.set_property(
                "fixture", "light", value, bounded=True, _send_guard=guard
            )
        except EufySdkApiClientError as caught:
            error = caught
        return client, writes, error

    async def test_off_retries_once_with_fresh_clock_id_and_original_expiry(self):
        _, writes, error = await self.refusal()
        self.assertIsNone(error)
        self.assertEqual(len(writes), 2)
        self.assertNotEqual(writes[0]["id"], writes[1]["id"])
        self.assertNotEqual(
            writes[0]["execution"]["clockToken"], writes[1]["execution"]["clockToken"]
        )
        self.assertEqual(
            [p["cmd"] for p in self.frames], ["boundedStateClock", "device.set"] * 2
        )
        self.assertEqual(
            writes[0]["execution"]["callerExpiresAtMs"],
            writes[1]["execution"]["callerExpiresAtMs"],
        )
        self.assertLessEqual(
            writes[1]["execution"]["remainingBudgetMs"],
            writes[0]["execution"]["remainingBudgetMs"],
        )

    async def test_persistent_refusal_stops_after_one_retry_and_retains_proof(self):
        _, writes, error = await self.refusal(persistent=True)
        self.assertEqual(len(writes), 2)
        self.assertIs(error.send_attempted, False)

    async def test_on_or_missing_malformed_or_possibly_sent_proof_never_retries(self):
        for value, flag in [
            (True, False),
            (False, None),
            (False, True),
            (False, 0),
            (False, "false"),
        ]:
            with self.subTest(value=value, flag=flag):
                _, writes, error = await self.refusal(value=value, flag=flag)
                self.assertEqual(len(writes), 1)
                self.assertIsNotNone(error)

    async def test_retry_revalidates_send_guard(self):
        allowed = [True]
        _, writes, error = await self.refusal(
            guard=lambda: allowed[0], on_refusal=lambda _: allowed.__setitem__(0, False)
        )
        self.assertEqual(len(writes), 1)
        self.assertEqual(str(error), "light_correction_paused_before_send")
        self.assertIs(error.send_attempted, False)

    async def test_retry_cannot_outlive_original_deadline(self):
        with patch("custom_components.eufy_sdk.api.BOUNDED_CALLER_SECONDS", 0.02):
            client = self.client()
            original = client._ws.send_json.side_effect
            writes = []

            async def reply(payload):
                if payload["cmd"] == "device.set":
                    writes.append(payload)
                    client._pending[payload["id"]].set_result(
                        {
                            "ok": False,
                            "code": "command_clock_unavailable",
                            "sendAttempted": False,
                        }
                    )
                elif len(writes):
                    await asyncio.sleep(0.04)
                else:
                    await original(payload)

            client._ws.send_json.side_effect = reply
            with self.assertRaises(EufySdkApiClientError) as caught:
                await client.set_property("fixture", "light", False, bounded=True)
            self.assertEqual(len(writes), 1)
            self.assertIs(caught.exception.send_attempted, False)

    async def test_newer_intent_supersedes_retry(self):
        _, writes, error = await self.refusal(
            on_refusal=lambda client: setattr(
                client._bounded_invocations[("fixture", "light")], "superseded", True
            )
        )
        self.assertEqual(len(writes), 1)
        self.assertIsNone(error)

    async def test_newer_on_invocation_supersedes_refused_off(self):
        client = self.client()
        original = client._ws.send_json.side_effect
        writes = []

        async def reply(payload):
            if payload["cmd"] == "device.set":
                writes.append(payload)
                if payload["value"] is False:
                    await client.set_property("fixture", "light", True, bounded=True)
                    client._pending[payload["id"]].set_result(
                        {
                            "ok": False,
                            "code": "command_clock_unavailable",
                            "sendAttempted": False,
                        }
                    )
                    return
            await original(payload)

        client._ws.send_json.side_effect = reply
        receipt = await client.set_property("fixture", "light", False, bounded=True)
        self.assertEqual(receipt.status, "superseded_before_send")
        self.assertEqual([p["value"] for p in writes], [False, True])

    async def _cancel_at_refusal_cleanup(self, *, active_watcher):
        client = self.client()
        original = client._ws.send_json.side_effect
        writes = []
        cancellations = []

        async def reply(payload):
            if payload["cmd"] == "device.set":
                writes.append(payload)
                if len(writes) == 1:
                    if active_watcher:
                        await asyncio.sleep(0)
                    task = asyncio.current_task()

                    def cancel():
                        cancellations.append(task.cancel())

                    asyncio.get_running_loop().call_soon(cancel)
                    client._pending[payload["id"]].set_result(
                        {
                            "ok": False,
                            "code": "command_clock_unavailable",
                            "sendAttempted": False,
                        }
                    )
                    return
            await original(payload)

        client._ws.send_json.side_effect = reply
        task = asyncio.create_task(
            client.set_property("fixture", "light", False, bounded=True)
        )
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(cancellations, [True])
        self.assertEqual(len(writes), 1)

    async def test_cancel_during_refusal_cleanup_prevents_retry(self):
        await self._cancel_at_refusal_cleanup(active_watcher=False)

    async def test_cancel_with_running_watcher_prevents_retry(self):
        await self._cancel_at_refusal_cleanup(active_watcher=True)


class KnownNotSentHoldTests(SafetyFixture, unittest.IsolatedAsyncioTestCase):
    """Preserve correction ownership only for possibly transmitted failures."""

    async def test_explicit_not_sent_avoids_hold_and_preserves_unrelated_recovery(self):
        entity, _, client, _ = await self.activate()
        controller = entity._light_correction
        controller.recoveries[99] = 500

        async def refuse(*_args, **kwargs):
            kwargs["_send_guard"]()
            error = EufySdkApiClientError("command_clock_unavailable")
            error.send_attempted = False
            raise error

        client.set_property.side_effect = refuse
        with self.assertRaises(EufySdkApiClientError):
            await entity.write(False)
        self.assertIsNone(controller.outcome_unknown_since)
        self.assertEqual(controller.recoveries, {99: 500})

    async def test_automatic_not_sent_avoids_hold_but_ambiguous_keeps_it(self):
        for proven in (False, True):
            entity, _, client, _ = await self.activate()
            controller = entity._light_correction

            async def refuse(*_args, proven=proven, **kwargs):
                kwargs["_send_guard"]()
                error = EufySdkApiClientError("command_clock_unavailable")
                if proven:
                    error.send_attempted = False
                raise error

            client.set_property.side_effect = refuse
            controller.start(False)
            controller.explicit_finished()
            self.now = 60
            self.observation(True)
            await controller._run(controller.generation)
            client.set_property.assert_awaited()
            self.assertEqual(controller.outcome_unknown_since is None, proven)
            controller.stop("closed")
