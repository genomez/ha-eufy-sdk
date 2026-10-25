"""Exercise automatic safety cancellation through the actual bounded RPC path."""

# ruff: noqa: ANN001, ANN201, ANN202, D102, FBT003, INP001, PT009, PT027, SLF001
import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import test_bounded_state_writes as bounded

from custom_components.eufy_sdk.api import (
    AUTOMATIC_LIGHT_CANCEL_SECONDS,
    EufySdkApiClientCommunicationError,
    EufySdkApiClientError,
)

PENDING = {
    "status": "pending_accepted",
    "sendAttempted": True,
    "effect": "unconfirmed",
    "intent": 1,
}


class AutomaticCancellationTests(unittest.IsolatedAsyncioTestCase):
    """Keep SDK child cancellation reachable after the parent RPC returns."""

    def setUp(self):
        self.now = 1000.0
        # Replace only the API's clock; asyncio deadlines still use real time.
        self.clock = patch(
            "custom_components.eufy_sdk.api.time",
            SimpleNamespace(monotonic=lambda: self.now, time=time.time),
        )
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def client(self):
        client = bounded.BoundedProtocolTests.client(self)
        client._set_late_contrary_capability(
            {"version": 1, "watchWindowMs": 600000, "confirmation": True}
        )
        original_send = client._ws.send_json.side_effect

        async def reply(payload):
            if payload["cmd"] != "device.set":
                await original_send(payload)
                return
            self.frames.append(payload)
            client._pending[payload["id"]].set_result(
                {"ok": True, "receipt": dict(PENDING)}
            )

        client._ws.send_json.side_effect = reply
        return client

    async def automatic(self, client, sn="synthetic-light"):
        return await client.set_property(
            sn, "light", True, bounded=True, automatic=True, _send_guard=lambda: True
        )

    def writes(self):
        return [frame for frame in self.frames if frame["cmd"] == "device.set"]

    def cancels(self):
        return [frame for frame in self.frames if frame["cmd"] == "boundedStateCancel"]

    async def drain_cancels(self, client):
        if client._cancel_tasks:
            await asyncio.gather(*tuple(client._cancel_tasks))

    async def test_pause_after_pending_receipt_cancels_child_authority_once(self):
        client = self.client()
        receipt = await self.automatic(client)
        request_id = self.writes()[0]["id"]
        self.assertEqual(receipt.status, "pending_accepted")
        self.assertTrue(self.writes()[0]["execution"]["automatic"])
        self.assertEqual(client._pending, {})
        self.assertEqual(client._correction_requests, {})
        self.assertFalse(self.cancels())

        self.now += AUTOMATIC_LIGHT_CANCEL_SECONDS - 0.001
        client.pause_light_correction("other-light", keep_observation=True)
        self.assertFalse(client._cancel_tasks)
        client.pause_light_correction("synthetic-light", keep_observation=True)
        client.pause_light_correction("synthetic-light", keep_observation=True)
        await self.drain_cancels(client)
        self.assertEqual(
            self.cancels(),
            [
                {
                    "id": request_id + 1,
                    "cmd": "boundedStateCancel",
                    "version": 1,
                    "requestId": request_id,
                    "keepObservation": True,
                }
            ],
        )
        self.assertEqual(client._retained_automatic_light_requests, {})
        self.assertEqual(client._observation_preserving_aborts, set())

    async def test_failed_cancel_keeps_ownership_and_retries_on_the_owning_transport(
        self,
    ):
        client = self.client()
        await self.automatic(client)
        request_id = self.writes()[0]["id"]
        original = client._ws.send_json.side_effect
        real_sleep = asyncio.sleep
        attempts = []

        async def transient(payload):
            if payload["cmd"] == "boundedStateCancel":
                attempts.append(payload["requestId"])
                if len(attempts) == 1:
                    self.assertIn(request_id, client._retained_automatic_light_requests)
                    message = "synthetic transient send failure"
                    raise OSError(message)
            await original(payload)

        async def advance(seconds):
            self.now += seconds
            await real_sleep(0)

        client._ws.send_json.side_effect = transient
        with patch("custom_components.eufy_sdk.api.asyncio.sleep", side_effect=advance):
            client.pause_light_correction("synthetic-light", keep_observation=True)
            await self.drain_cancels(client)
        self.assertEqual(attempts, [request_id, request_id])
        self.assertEqual(len(self.cancels()), 1)
        self.assertEqual(client._retained_automatic_light_requests, {})
        self.assertEqual(client._safety_cancel_inflight, set())

    async def test_pause_cancels_all_retained_for_target_only(self):
        client = self.client()
        await self.automatic(client)
        await self.automatic(client, "other-light")
        await self.automatic(client)
        writes = self.writes()
        client.pause_light_correction("synthetic-light", keep_observation=True)
        await self.drain_cancels(client)
        self.assertEqual(
            [frame["requestId"] for frame in self.cancels()],
            [writes[0]["id"], writes[2]["id"]],
        )
        self.assertEqual(
            list(client._retained_automatic_light_requests), [writes[1]["id"]]
        )
        self.assertTrue(all(frame["keepObservation"] for frame in self.cancels()))

    async def start_active(self, client):
        original_send = client._ws.send_json.side_effect
        submitted = asyncio.Event()

        async def hold(payload):
            if payload["cmd"] == "device.set":
                self.frames.append(payload)
                submitted.set()
            else:
                await original_send(payload)

        client._ws.send_json.side_effect = hold
        task = asyncio.create_task(self.automatic(client))
        await asyncio.wait_for(submitted.wait(), 1)
        return task, self.writes()[-1]["id"]

    async def test_pause_cancels_active_and_retained_preserving_reply_future(self):
        client = self.client()
        await self.automatic(client)
        retained_id = self.writes()[0]["id"]
        task, active_id = await self.start_active(client)
        client.pause_light_correction("synthetic-light", keep_observation=True)
        client.pause_light_correction("synthetic-light", keep_observation=True)
        await self.drain_cancels(client)
        self.assertFalse(task.done())
        self.assertFalse(client._pending[active_id].done())
        self.assertEqual(
            {frame["requestId"] for frame in self.cancels()}, {retained_id, active_id}
        )
        self.assertEqual(len(self.cancels()), 2)
        client._pending[active_id].set_result({"ok": True, "receipt": PENDING})
        await task
        client.pause_light_correction("synthetic-light", keep_observation=True)
        await self.drain_cancels(client)
        self.assertEqual(len(self.cancels()), 2)
        self.assertEqual(client._retained_automatic_light_requests, {})
        self.assertEqual(client._cancelled_correction_requests, set())

    async def test_task_abort_after_pause_does_not_duplicate_active_cancel(self):
        client = self.client()
        task, request_id = await self.start_active(client)
        client.pause_light_correction("synthetic-light", keep_observation=True)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self.drain_cancels(client)
        self.assertEqual(len(self.cancels()), 1)
        self.assertEqual(self.cancels()[0]["requestId"], request_id)
        self.assertIs(self.cancels()[0]["keepObservation"], True)
        self.assertEqual(client._pending, {})
        self.assertEqual(client._cancelled_correction_requests, set())
        self.assertEqual(client._observation_preserving_aborts, set())
        self.assertEqual(client._retained_automatic_light_requests, {})

    async def test_concurrent_active_requests_for_same_target_are_all_cancelled(self):
        client = self.client()
        first, first_id = await self.start_active(client)
        second, second_id = await self.start_active(client)
        client.pause_light_correction("synthetic-light", keep_observation=True)
        await self.drain_cancels(client)
        self.assertEqual(
            {frame["requestId"] for frame in self.cancels()}, {first_id, second_id}
        )
        for task in (first, second):
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        await self.drain_cancels(client)
        self.assertEqual(len(self.cancels()), 2)

    async def test_retained_request_cannot_cancel_on_replacement_transport(self):
        client = self.client()
        await self.automatic(client)
        original = client._ws
        replacement = Mock(closed=False, send_json=AsyncMock())
        client._ws = replacement
        client.pause_light_correction("synthetic-light", keep_observation=True)
        await self.drain_cancels(client)
        self.assertFalse(self.cancels())
        self.assertEqual(original.send_json.await_count, 2)
        replacement.send_json.assert_not_called()
        self.assertEqual(client._retained_automatic_light_requests, {})

    async def test_closed_transport_is_pruned_without_cancel(self):
        client = self.client()
        await self.automatic(client)
        client._ws.closed = True
        client.pause_light_correction("synthetic-light", keep_observation=True)
        self.assertFalse(client._cancel_tasks)
        self.assertEqual(client._retained_automatic_light_requests, {})

    async def test_retention_window_starts_at_terminal_receipt_and_expires_at_45(self):
        for elapsed, expected in ((44.999, 1), (45, 0), (46, 0)):
            with self.subTest(elapsed=elapsed):
                client = self.client()
                original_send = client._ws.send_json.side_effect

                async def delayed(payload, send=original_send):
                    if payload["cmd"] == "device.set":
                        self.now += 60
                    await send(payload)

                client._ws.send_json.side_effect = delayed
                await self.automatic(client)
                self.now += elapsed
                client.pause_light_correction("synthetic-light", keep_observation=True)
                await self.drain_cancels(client)
                self.assertEqual(len(self.cancels()), expected)
                self.assertEqual(client._retained_automatic_light_requests, {})

    async def test_new_automatic_request_prunes_expired_retention_without_timers(self):
        client = self.client()
        await self.automatic(client)
        first_id = self.writes()[0]["id"]
        self.now += AUTOMATIC_LIGHT_CANCEL_SECONDS
        await self.automatic(client, "other-light")
        self.assertNotIn(first_id, client._retained_automatic_light_requests)
        self.assertEqual(len(client._retained_automatic_light_requests), 1)
        self.assertFalse(self.cancels())
        self.assertFalse(client._cancel_tasks)

    async def test_capacity_evicts_oldest_without_sending_commands(self):
        client = self.client()
        with patch("custom_components.eufy_sdk.api.MAX_AUTOMATIC_LIGHT_REQUESTS", 2):
            for _ in range(3):
                await self.automatic(client)
                self.assertLessEqual(len(client._retained_automatic_light_requests), 2)
        self.assertEqual(
            list(client._retained_automatic_light_requests),
            [frame["id"] for frame in self.writes()[1:]],
        )
        self.assertFalse(self.cancels())

    async def test_connection_reset_and_close_clear_retained_ownership(self):
        for operation in ("reset", "close"):
            with self.subTest(operation=operation):
                client = self.client()
                await self.automatic(client)
                self.assertTrue(client._retained_automatic_light_requests)
                if operation == "reset":
                    client._schedule_reconnect = Mock()
                    await client.reset_connection()
                else:
                    await client.close()
                self.assertEqual(client._retained_automatic_light_requests, {})
                client.pause_light_correction("synthetic-light", keep_observation=True)
                self.assertFalse(self.cancels())

    async def test_explicit_and_legacy_calls_do_not_retain_or_add_background_work(self):
        for options in (
            {},
            {"bounded": True},
            {"bounded": True, "_send_guard": lambda: True},
            {"bounded": True, "correction": True},
        ):
            with self.subTest(options=options):
                client = self.client()
                with patch(
                    "custom_components.eufy_sdk.api.asyncio.create_task",
                    wraps=asyncio.create_task,
                ) as create_task:
                    await client.set_property(
                        "synthetic-light", "light", False, **options
                    )
                    self.assertEqual(
                        create_task.call_count, int(options.get("bounded", False))
                    )
                    client.pause_light_correction(
                        "synthetic-light", keep_observation=True
                    )
                    self.assertEqual(
                        create_task.call_count, int(options.get("bounded", False))
                    )
                self.assertEqual(client._retained_automatic_light_requests, {})
                self.assertEqual(client._correction_requests, {})
                self.assertFalse(client._cancel_tasks)
                self.assertFalse(self.cancels())

    async def test_revoked_dispatch_guard_never_retains_or_sends(self):
        client = self.client()
        with self.assertRaisesRegex(
            EufySdkApiClientCommunicationError, "^light_correction_paused_before_send$"
        ):
            await client.set_property(
                "synthetic-light",
                "light",
                True,
                bounded=True,
                automatic=True,
                _send_guard=lambda: False,
            )
        self.assertFalse(self.writes())
        self.assertEqual(client._retained_automatic_light_requests, {})

    async def test_rejected_reply_does_not_retain(self):
        client = self.client()
        original_send = client._ws.send_json.side_effect

        async def reject(payload):
            if payload["cmd"] == "device.set":
                self.frames.append(payload)
                client._pending[payload["id"]].set_result(
                    {"ok": False, "code": "command_validation_failed"}
                )
            else:
                await original_send(payload)

        client._ws.send_json.side_effect = reject
        with self.assertRaises(EufySdkApiClientError):
            await self.automatic(client)
        client.pause_light_correction("synthetic-light", keep_observation=True)
        self.assertFalse(self.cancels())
        self.assertEqual(client._retained_automatic_light_requests, {})

    async def test_terminal_reply_from_retired_generation_is_not_retained(self):
        client = self.client()
        original_send = client._ws.send_json.side_effect

        async def replace(payload):
            await original_send(payload)
            if payload["cmd"] == "device.set":
                client._reset_command_health()

        client._ws.send_json.side_effect = replace
        with self.assertRaisesRegex(
            EufySdkApiClientCommunicationError, "^bridge_connection_changed$"
        ):
            await self.automatic(client)
        self.assertEqual(client._retained_automatic_light_requests, {})
