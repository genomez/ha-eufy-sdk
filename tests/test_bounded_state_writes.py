"""Exercise the negotiated caller contract without network or device access."""

# ruff: noqa: ANN001, ANN002, ANN003, ANN201, ANN202, D102, FBT003
# ruff: noqa: INP001, PT009, PT027, SLF001
import asyncio
import dataclasses
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

import test_floodlight_state_hold as fixtures
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from custom_components.eufy_sdk.api import (
    BOUNDED_PROFILE,
    COMMAND_ERROR_CODES,
    COMMAND_RECEIPT_STATUSES,
    CommandReceipt,
    EufySdkApiClient,
    EufySdkApiClientCommunicationError,
    EufySdkApiClientError,
    _command_receipt,
)

CAPABILITY = {
    "version": 1,
    "profile": BOUNDED_PROFILE,
    "admissionMaxMs": 60000,
    "activeMaxMs": 12000,
    "sdkTotalMaxMs": 72000,
    "bridgeMaxMs": 75000,
    "callerMaxMs": 80000,
    "cancellation": True,
    "receipts": True,
    "clock": {"version": 1, "maxUncertaintyMs": 1000},
}
ACCEPTED = {"status": "acknowledged", "sendAttempted": True, "effect": "unconfirmed"}


class BoundedProtocolTests(unittest.IsolatedAsyncioTestCase):
    """Negotiate clocks, deadlines, cancellations and finite protocol data."""

    def client(self, *, capability=True):
        client = EufySdkApiClient("synthetic-bridge", 3000, Mock())
        client._ws = Mock(closed=False)
        client._ws.close = AsyncMock()
        client._set_bounded_capability(CAPABILITY if capability else None)
        self.frames = []

        async def reply(payload):
            self.frames.append(payload)
            if payload["cmd"] == "boundedStateClock":
                result = {
                    "version": 1,
                    "callerSentAtMs": payload["callerSentAtMs"],
                    "bridgeReceivedAtMs": payload["callerSentAtMs"] + 9000,
                    "bridgeSentAtMs": payload["callerSentAtMs"] + 9000,
                    "clockToken": "synthetic-clock",
                }
            else:
                result = {"receipt": ACCEPTED}
            future = client._pending.get(payload["id"])
            if future is not None:
                future.set_result({"ok": True, **result})

        client._ws.send_json = AsyncMock(side_effect=reply)
        return client

    def test_capability_requires_complete_versioned_contract(self):
        client = self.client()
        self.assertTrue(client.bounded_state_writes_supported)
        for mutation in (
            None,
            {},
            {**CAPABILITY, "version": 2},
            {**CAPABILITY, "cancellation": False},
            {**CAPABILITY, "callerMaxMs": 90000},
            {**CAPABILITY, "clock": {"version": 1, "maxUncertaintyMs": 1001}},
            {**CAPABILITY, "version": True},
        ):
            with self.subTest(capability=mutation):
                client._set_bounded_capability(mutation)
                self.assertFalse(client.bounded_state_writes_supported)

    async def test_original_expiry_and_remaining_cross_clock_exchange(self):
        client = self.client()
        with patch(
            "custom_components.eufy_sdk.api.time.time", side_effect=[1000, 1000.4]
        ):
            receipt = await client.set_property(
                "synthetic-device", "light", True, bounded=True
            )
        self.assertEqual(receipt, CommandReceipt("acknowledged", True, "unconfirmed"))
        self.assertEqual(
            [frame["cmd"] for frame in self.frames], ["boundedStateClock", "device.set"]
        )
        execution = self.frames[1]["execution"]
        self.assertEqual(execution["callerExpiresAtMs"], 1080000)
        self.assertLessEqual(execution["remainingBudgetMs"], 79600)
        self.assertEqual(execution["callerReceivedAtMs"], 1000400)
        self.assertEqual(execution["clockUncertaintyMs"], 201)
        self.assertEqual(client._bounded_requests, {})
        self.assertEqual(client._cancel_tasks, set())

    async def test_mixed_versions_and_non_profile_calls_keep_ordinary_rpc(self):
        for capability, name, value, bounded in (
            (False, "light", True, True),
            (True, "lightPower", True, True),
            (True, "light", 1, True),
            (True, "light", True, False),
        ):
            with self.subTest(
                name=name, capability=capability, value=value, bounded=bounded
            ):
                client = self.client(capability=capability)
                rpc = client.rpc = AsyncMock(return_value={"ok": True})
                self.assertIsNone(
                    await client.set_property("synthetic", name, value, bounded=bounded)
                )
                rpc.assert_awaited_once_with(
                    "device.set", sn="synthetic", name=name, value=value
                )

    async def test_old_clock_reply_cannot_send_after_newer_intent_completed(self):
        client = self.client()
        original_send = client._ws.send_json.side_effect
        old_clock_started = asyncio.Event()
        release_old_clock = asyncio.Event()
        first_clock = True

        async def reply(payload):
            nonlocal first_clock
            if payload["cmd"] == "boundedStateClock" and first_clock:
                first_clock = False
                old_clock_started.set()
                await release_old_clock.wait()
            await original_send(payload)

        client._ws.send_json.side_effect = reply
        older = asyncio.create_task(
            client.set_property("synthetic", "light", True, bounded=True)
        )
        await old_clock_started.wait()
        newer = await client.set_property("synthetic", "light", False, bounded=True)
        self.assertTrue(newer.accepted)
        self.assertEqual(client._bounded_invocations, {})
        release_old_clock.set()
        self.assertEqual(
            await older,
            CommandReceipt("superseded_before_send", False, "unconfirmed"),
        )
        self.assertEqual(
            [frame["value"] for frame in self.frames if frame["cmd"] == "device.set"],
            [False],
        )
        self.assertEqual(client._bounded_invocations, {})
        self.assertEqual(client._pending, {})

    async def test_clock_uncertainty_or_wall_jump_prevents_property_send(self):
        for received in (1003, 999, float("inf")):
            with self.subTest(received=received):
                client = self.client()
                with (
                    patch(
                        "custom_components.eufy_sdk.api.time.time",
                        side_effect=[1000, received],
                    ),
                    self.assertRaisesRegex(
                        EufySdkApiClientCommunicationError,
                        "^command_clock_unavailable$",
                    ),
                ):
                    await client.set_property("synthetic", "light", False, bounded=True)
                self.assertEqual(len(self.frames), 1)

    async def test_clock_reply_validation_never_sends_property(self):
        for fields in (
            {"version": 2},
            {"callerSentAtMs": 0},
            {"bridgeSentAtMs": float("nan")},
            {"bridgeReceivedAtMs": -10, "bridgeSentAtMs": -20},
            {"clockToken": {}},
            {"bridgeSentAtMs": 10**400},
        ):
            with self.subTest(fields=fields):
                client = self.client()
                client.rpc = AsyncMock(
                    return_value={
                        "version": 1,
                        "callerSentAtMs": 1000000,
                        "bridgeReceivedAtMs": 1000000,
                        "bridgeSentAtMs": 1000000,
                        "clockToken": "clock",
                        **fields,
                    }
                )
                with (
                    patch(
                        "custom_components.eufy_sdk.api.time.time", return_value=1000
                    ),
                    self.assertRaises(EufySdkApiClientCommunicationError),
                ):
                    await client.set_property("synthetic", "light", True, bounded=True)
                self.assertEqual(client.rpc.await_count, 1)

    async def test_cancellation_uses_original_id_and_owning_transport(self):
        client = self.client()
        original = client._ws
        original.send_json = AsyncMock()
        task = asyncio.create_task(client.rpc("device.set", _cancel_on_abort=True))
        await asyncio.sleep(0)
        mid = next(iter(client._pending))
        replacement = Mock(closed=False, send_json=AsyncMock())
        client._ws = replacement
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.gather(*tuple(client._cancel_tasks))
        self.assertEqual(original.send_json.call_args.args[0]["requestId"], mid)
        self.assertEqual(
            original.send_json.call_args.args[0]["cmd"], "boundedStateCancel"
        )
        replacement.send_json.assert_not_called()
        self.assertEqual(client._pending, {})
        self.assertEqual(client._bounded_requests, {})

    async def test_timeout_cancels_but_terminal_receipt_does_not(self):
        client = self.client()
        client._ws.send_json = AsyncMock()
        with self.assertRaisesRegex(
            EufySdkApiClientCommunicationError, "^bridge_request_timeout$"
        ):
            await client.rpc("device.set", timeout=0, _cancel_on_abort=True)
        await asyncio.gather(*tuple(client._cancel_tasks))
        self.assertEqual(client._ws.send_json.await_count, 2)
        client = self.client()
        await client.rpc("device.set", _cancel_on_abort=True)
        self.assertEqual(len(self.frames), 1)

    async def test_ack_winning_cancellation_race_is_not_cancelled(self):
        client = self.client()

        async def reply(payload):
            client._pending[payload["id"]].set_result({"ok": True, "receipt": ACCEPTED})
            asyncio.current_task().cancel()
            await asyncio.sleep(0)

        client._ws.send_json = AsyncMock(side_effect=reply)
        with self.assertRaises(asyncio.CancelledError):
            await client.rpc("device.set", _cancel_on_abort=True)
        self.assertEqual(client._cancel_tasks, set())
        self.assertEqual(client._pending, {})

    async def test_terminal_reply_before_send_timeout_does_not_cancel_lineage(self):
        client = self.client()

        async def reply(payload):
            client._pending[payload["id"]].set_result({"ok": True, "receipt": ACCEPTED})
            raise TimeoutError

        client._ws.send_json = AsyncMock(side_effect=reply)
        with self.assertRaises(EufySdkApiClientCommunicationError):
            await client.rpc("device.set", _cancel_on_abort=True)
        self.assertEqual(client._cancel_tasks, set())
        self.assertEqual(client._pending, {})

    async def test_clock_jump_while_waiting_cancels_owning_request(self):
        client = self.client()
        original_send = client._ws.send_json.side_effect
        written = asyncio.Event()

        async def reply(payload):
            if payload["cmd"] == "boundedStateClock":
                await original_send(payload)
            elif payload["cmd"] == "device.set":
                written.set()
            else:
                self.frames.append(payload)

        client._ws.send_json.side_effect = reply
        now = [1000.0]
        with patch(
            "custom_components.eufy_sdk.api.time.time", side_effect=lambda: now[0]
        ):
            task = asyncio.create_task(
                client.set_property("synthetic", "light", True, bounded=True)
            )
            await written.wait()
            now[0] += 2
            with self.assertRaisesRegex(
                EufySdkApiClientCommunicationError, "^command_clock_unavailable$"
            ):
                await task
        await asyncio.gather(*tuple(client._cancel_tasks))
        self.assertEqual(self.frames[-1]["cmd"], "boundedStateCancel")
        self.assertEqual(client._pending, {})

    async def test_unload_cancels_pending_without_replaying_on_reconnect(self):
        client = self.client()
        client._ws.send_json = AsyncMock()
        transport = client._ws
        task = asyncio.create_task(client.rpc("device.set", _cancel_on_abort=True))
        await asyncio.sleep(0)
        await client.close()
        with self.assertRaises(EufySdkApiClientCommunicationError):
            await task
        self.assertEqual(
            transport.send_json.call_args.args[0]["cmd"], "boundedStateCancel"
        )
        self.assertEqual(client._pending, {})
        self.assertFalse(client.bounded_state_writes_supported)

    async def test_one_outer_deadline_includes_clock_and_send(self):
        client = self.client()
        original_timeout_at = asyncio.timeout_at
        deadlines = []

        def capture(deadline):
            deadlines.append(deadline)
            return original_timeout_at(deadline)

        start = asyncio.get_running_loop().time()
        with patch(
            "custom_components.eufy_sdk.api.asyncio.timeout_at", side_effect=capture
        ):
            await client.set_property("synthetic", "light", True, bounded=True)
        self.assertEqual(len(deadlines), 1)
        self.assertGreaterEqual(deadlines[0], start + 80)
        self.assertLess(deadlines[0], start + 80.1)

    def test_closed_receipts_preserve_finite_statuses_and_drop_private_fields(self):
        for status in COMMAND_RECEIPT_STATUSES:
            with self.subTest(status=status):
                sent = status in {
                    "acknowledged",
                    "pending_accepted",
                    "ack_timeout",
                    "rejected",
                    "cancelled_after_send",
                    "closed_after_send",
                    "expired_after_send",
                }
                receipt = _command_receipt(
                    {
                        "status": status,
                        "sendAttempted": sent,
                        "effect": "unknown",
                        "attempt": 3,
                        "intent": 4,
                        "private": "synthetic-private",
                    }
                )
                self.assertEqual(receipt.status, status)
                self.assertNotIn(
                    "synthetic-private", json.dumps(dataclasses.asdict(receipt))
                )
                with self.assertRaises(dataclasses.FrozenInstanceError):
                    receipt.status = "changed"

    def test_malformed_receipts_fail_closed(self):
        for receipt in (
            None,
            {},
            {**ACCEPTED, "status": "synthetic-private"},
            {**ACCEPTED, "effect": "synthetic-private"},
            {**ACCEPTED, "sendAttempted": False},
            {**ACCEPTED, "status": "superseded_before_send"},
            {**ACCEPTED, "superseded": "synthetic-private"},
            {**ACCEPTED, "superseded": 1},
        ):
            with (
                self.subTest(receipt=receipt),
                self.assertRaisesRegex(
                    EufySdkApiClientCommunicationError, "^bridge_invalid_receipt$"
                ),
            ):
                _command_receipt(receipt)

    def test_optional_supersession_is_finite_and_preserves_wire_outcome(self):
        for status in ("acknowledged", "pending_accepted"):
            for superseded in (False, True):
                with self.subTest(status=status, superseded=superseded):
                    receipt = _command_receipt(
                        {
                            **ACCEPTED,
                            "status": status,
                            "superseded": superseded,
                        }
                    )
                    self.assertEqual(receipt.status, status)
                    self.assertEqual(receipt.effect, "unconfirmed")
                    self.assertEqual(receipt.accepted, not superseded)

    async def test_new_error_allowlist_still_drops_untrusted_text(self):
        for code in COMMAND_ERROR_CODES:
            client = self.client()

            async def reply(payload, code=code, client=client):
                client._pending[payload["id"]].set_result(
                    {"ok": False, "code": code, "error": "synthetic-private"}
                )

            client._ws.send_json = AsyncMock(side_effect=reply)
            with self.assertRaises(EufySdkApiClientError) as caught:
                await client.rpc("device.set")
            self.assertEqual(str(caught.exception), code)


class BoundedEntityTests(unittest.IsolatedAsyncioTestCase):
    """Keep acceptance evidence owned by the newest entity invocation."""

    _entity = fixtures.FloodlightStateHoldTests._entity
    _report = fixtures.FloodlightStateHoldTests._report

    def bounded_entity(self):
        entity, coordinator, client, seen = self._entity()
        entity._spec["boundedStateWrites"] = True
        return entity, coordinator, client, seen

    async def test_pending_acceptance_is_unconfirmed_and_report_is_separate(self):
        entity, coordinator, client, _seen = self.bounded_entity()
        client.set_property.return_value = CommandReceipt(
            "pending_accepted", True, "unconfirmed"
        )
        await entity.write(True)
        client.set_property.assert_awaited_once_with(
            "fixture-device", "light", True, bounded=True
        )
        self.assertTrue(entity.is_on)
        self.assertFalse(coordinator.data["fixture-device"]["state"]["light"])
        self.assertEqual(
            entity.extra_state_attributes["command_status"], "pending_accepted"
        )
        self.assertEqual(entity.extra_state_attributes["command_effect"], "unconfirmed")

    async def test_external_newer_intent_prevents_current_invocation_optimism(self):
        for status in ("acknowledged", "pending_accepted"):
            with self.subTest(status=status):
                entity, coordinator, client, _seen = self.bounded_entity()
                client.set_property.return_value = _command_receipt(
                    {
                        **ACCEPTED,
                        "status": status,
                        "superseded": True,
                    }
                )
                with self.assertRaisesRegex(
                    EufySdkApiClientError, "^command_superseded_after_send$"
                ):
                    await entity.write(True)
                self.assertFalse(entity.is_on)
                self.assertFalse(coordinator.data["fixture-device"]["state"]["light"])
                self.assertIsNone(entity._optimistic_owner)
                self.assertIsNone(entity._light_guard_owner)
                attributes = entity.extra_state_attributes
                self.assertEqual(attributes["command_status"], status)
                self.assertEqual(attributes["command_effect"], "unconfirmed")
                self.assertTrue(attributes["command_superseded"])

    async def test_failure_supersession_cancel_install_no_hold(self):
        for status in (
            "superseded_before_send",
            "cancelled_before_send",
            "queue_full",
            "ack_timeout",
        ):
            with self.subTest(status=status):
                entity, _coordinator, client, _seen = self.bounded_entity()
                client.set_property.return_value = CommandReceipt(
                    status, status == "ack_timeout", "unknown"
                )
                with self.assertRaises(EufySdkApiClientError):
                    await entity.write(True)
                self.assertFalse(entity.is_on)
                self.assertIsNone(entity._light_guard_owner)
                self.assertIsNone(entity._optimistic_owner)

    async def test_late_old_receipt_cannot_override_new_receipt_or_report(self):
        entity, _coordinator, client, _seen = self.bounded_entity()
        gate = asyncio.Event()

        async def write(_sn, _name, value, **_kwargs):
            if value:
                await gate.wait()
            return CommandReceipt(
                "pending_accepted" if value else "acknowledged", True, "unconfirmed"
            )

        client.set_property.side_effect = write
        old = asyncio.create_task(entity.write(True))
        await asyncio.sleep(0)
        await entity.write(False)
        self._report(entity, False)
        gate.set()
        await old
        self.assertFalse(entity.is_on)
        self.assertEqual(
            entity.extra_state_attributes["command_status"], "acknowledged"
        )
        self.assertEqual(entity._light_guard_owner, 2)

    async def test_old_supersession_still_fails_its_caller_without_changing_new_hold(
        self,
    ):
        entity, _coordinator, client, _seen = self.bounded_entity()
        gate = asyncio.Event()

        async def write(_sn, _name, value, **_kwargs):
            if value:
                await gate.wait()
                return CommandReceipt("superseded_before_send", False, "unknown")
            return CommandReceipt("acknowledged", True, "unconfirmed")

        client.set_property.side_effect = write
        old = asyncio.create_task(entity.write(True))
        await asyncio.sleep(0)
        await entity.write(False)
        gate.set()
        with self.assertRaisesRegex(
            EufySdkApiClientError, "^command_superseded_before_send$"
        ):
            await old
        self.assertFalse(entity.is_on)
        self.assertEqual(
            entity.extra_state_attributes["command_status"], "acknowledged"
        )

    async def test_legacy_fallback_does_not_advertise_bounded_execution(self):
        entity, _coordinator, client, _seen = self.bounded_entity()
        client.set_property.return_value = None
        await entity.write(True)
        self.assertEqual(entity.extra_state_attributes["command_profile"], "legacy")
        self.assertNotIn("command_status", entity.extra_state_attributes)

    async def test_entity_unload_cancels_bounded_call_and_installs_no_hold(self):
        entity, _coordinator, client, _seen = self.bounded_entity()
        client.set_property.side_effect = lambda *_args, **_kwargs: None

        async def wait(*_args, **_kwargs):
            await asyncio.Event().wait()

        client.set_property.side_effect = wait
        task = asyncio.create_task(entity.write(True))
        await asyncio.sleep(0)
        with patch.object(
            CoordinatorEntity, "async_will_remove_from_hass", new=AsyncMock()
        ):
            await entity.async_will_remove_from_hass()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNone(entity._optimistic_owner)
