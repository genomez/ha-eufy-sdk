"""Guard receipts never replace the panel state or replay commands."""

# ruff: noqa: ANN001, ANN201, D102, INP001, PT009, SLF001
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from custom_components.eufy_sdk.alarm_control_panel import EufySdkAlarmControlPanel
from custom_components.eufy_sdk.api import EufySdkApiClient
from custom_components.eufy_sdk.guard_confirmation import (
    GuardNotifications,
    guard_attributes,
    guard_capability,
    guard_receipt,
)
from custom_components.eufy_sdk.notification_delivery import (
    NOTICE_RETRY_DELAYS,
    PHONE_FAILURE_SENTENCE,
)
from custom_components.eufy_sdk.select import EufySdkSelect

RUNTIME = "00000000-0000-4000-8000-000000000001"


def receipt(outcome="pending", generation=1, **fields: object):
    """Build one synthetic finite guard receipt."""
    return {
        "version": 1,
        "runtimeGeneration": RUNTIME,
        "handle": f"00000000-0000-4000-8000-{generation:012d}",
        "generation": generation,
        "deviceSn": "synthetic-station",
        "requestedMode": 1,
        "outcome": outcome,
        "reason": {
            "pending": "waiting_report",
            "reported": "reported_after_command",
            "contradicted": "different_mode_reported",
            "not_reported": "window_expired",
        }[outcome],
        "attempts": 1,
        "outcomeAtMs": None if outcome == "pending" else 1000,
        **fields,
    }


class GuardConfirmationTests(unittest.IsolatedAsyncioTestCase):
    """Project SDK outcomes without changing device state."""

    def client(self):
        client = EufySdkApiClient("synthetic", 3000, Mock())
        client._ws = Mock(closed=False)
        client.guard_confirmation_supported = True
        client._on_event = Mock()
        return client

    async def test_ack_event_race_and_terminal_ordering(self):
        client = self.client()
        client._dispatch_event(
            {"event": "guardConfirmation", **receipt("contradicted", 2)}
        )
        client.rpc = AsyncMock(
            return_value={"guardConfirmation": receipt("pending", 2)}
        )
        await client.set_property("synthetic-station", "armingMode", 1)
        self.assertEqual(
            client.guard_receipts["synthetic-station"]["outcome"], "contradicted"
        )
        client._dispatch_event({"event": "guardConfirmation", **receipt("reported", 1)})
        client._dispatch_event(
            {
                "event": "guardConfirmation",
                **receipt(
                    "reported",
                    3,
                    runtimeGeneration="00000000-0000-4000-8000-000000000002",
                ),
            }
        )
        self.assertEqual(client._on_event.call_count, 1)
        client.rpc.assert_awaited_once_with(
            "device.set", timeout=60, sn="synthetic-station", name="armingMode", value=1
        )

    async def test_lost_terminal_event_recovers_by_get_without_send(self):
        client = self.client()
        client._dispatch_event({"event": "guardConfirmation", **receipt()})
        client.rpc = AsyncMock(
            side_effect=[
                {"devices": []},
                {"guardConfirmation": receipt("not_reported")},
            ]
        )
        await client.list_devices()
        self.assertEqual(
            client.guard_receipts["synthetic-station"]["outcome"], "not_reported"
        )
        self.assertEqual(
            [call.args[0] for call in client.rpc.await_args_list],
            ["devices.list", "guard.confirmation.get"],
        )

    async def test_recovery_failure_or_expired_handle_cannot_fail_inventory(self):
        client = self.client()
        client._dispatch_event({"event": "guardConfirmation", **receipt()})
        client.rpc = AsyncMock(
            side_effect=[
                {"devices": []},
                RuntimeError("synthetic recovery unavailable"),
            ]
        )
        self.assertEqual(await client.list_devices(), [])
        self.assertEqual(
            client.guard_receipts["synthetic-station"]["outcome"], "pending"
        )
        client.rpc.side_effect = [{"devices": []}, {"guardConfirmation": None}]
        self.assertEqual(await client.list_devices(), [])
        self.assertNotIn("synthetic-station", client.guard_receipts)

    async def test_legacy_capability_has_no_guard_attributes_notifications_or_get(self):
        client = self.client()
        client.guard_confirmation_supported = False
        client.rpc = AsyncMock(return_value={"devices": []})
        client._dispatch_event(
            {"event": "guardConfirmation", **receipt("not_reported")}
        )
        await client.list_devices()
        self.assertEqual(guard_attributes(client, "synthetic-station"), {})
        client._on_event.assert_not_called()
        client.rpc.assert_awaited_once_with("devices.list")
        for raw in (
            None,
            {},
            {"version": True, "maxAttempts": 1, "enabledRetry": False},
            {"version": 1, "maxAttempts": 2, "enabledRetry": False},
        ):
            self.assertFalse(guard_capability(raw))

    async def test_attributes_do_not_mutate_normal_panel_or_select_mode(self):
        client = self.client()
        client._dispatch_event({"event": "guardConfirmation", **receipt("reported")})
        coordinator = SimpleNamespace(
            data={"synthetic-station": {"state": {"armingMode": 0}}},
            config_entry=SimpleNamespace(runtime_data=SimpleNamespace(client=client)),
            last_update_success=True,
        )
        panel = EufySdkAlarmControlPanel(coordinator, "synthetic-station")
        select = EufySdkSelect(
            coordinator,
            "synthetic-station",
            {
                "name": "armingMode",
                "enumValues": {"0": "Away", "1": "Home"},
                "type": "enum",
            },
        )
        self.assertEqual(str(panel.alarm_state), "armed_away")
        self.assertEqual(select.current_option, "Away")
        for entity in (panel, select):
            self.assertEqual(entity.extra_state_attributes["guard_outcome"], "reported")
            self.assertEqual(entity.extra_state_attributes["guard_requested_mode"], 1)

    async def test_malformed_receipts_are_unknown(self):
        for key, value in (
            ("outcome", []),
            ("reason", {}),
            ("attempts", True),
            ("requestedMode", True),
            ("version", True),
            ("outcomeAtMs", -1),
            ("runtimeGeneration", "private"),
        ):
            self.assertIsNone(guard_receipt(receipt("reported") | {key: value}))
        self.assertNotIn("private", str(guard_receipt(receipt(raw="private"))))


class GuardNotificationTests(unittest.IsolatedAsyncioTestCase):
    """Reuse configured phone delivery and notification fallback."""

    def owner(self, service=None):
        hass = SimpleNamespace(
            async_create_task=asyncio.create_task,
            services=SimpleNamespace(async_call=AsyncMock()),
        )
        return GuardNotifications(hass, {"light_safety_notify_service": service})

    async def test_one_problem_per_intent_and_only_later_report_clears(self):
        owner = self.owner()
        with (
            patch(
                "custom_components.eufy_sdk.guard_confirmation.persistent_notification.async_create"
            ) as create,
            patch(
                "custom_components.eufy_sdk.guard_confirmation.persistent_notification.async_dismiss"
            ) as dismiss,
        ):
            owner.apply(receipt("not_reported"))
            owner.apply(receipt("not_reported"))
            owner.apply(receipt("contradicted", 2))
            self.assertEqual(create.call_count, 2)
            owner.apply(receipt("reported", 1))
            dismiss.assert_not_called()
            owner.apply(receipt("pending", 3))
            dismiss.assert_not_called()
            owner.apply(receipt("reported", 3))
            self.assertEqual(dismiss.call_count, 2)
        owner.close()

    async def test_configured_phone_service_reuses_delivery_envelope(self):
        owner = self.owner("notify.synthetic")
        with patch(
            "custom_components.eufy_sdk.guard_confirmation.persistent_notification.async_create"
        ):
            owner.apply(receipt("contradicted"))
            await asyncio.gather(*owner.tasks.values())
        call = owner.hass.services.async_call.await_args
        self.assertEqual(call.args[:2], ("notify", "synthetic"))
        self.assertTrue(call.args[2]["data"]["sticky"])
        owner.close()

    async def test_phone_failure_retries_existing_delays_and_keeps_visible_fallback(
        self,
    ):
        owner = self.owner("notify.synthetic")
        owner.hass.services.async_call.side_effect = RuntimeError("synthetic")
        with (
            patch(
                "custom_components.eufy_sdk.guard_confirmation.persistent_notification.async_create"
            ) as create,
            patch(
                "custom_components.eufy_sdk.guard_confirmation.asyncio.sleep",
                new_callable=AsyncMock,
            ) as sleep,
        ):
            owner.apply(receipt("not_reported"))
            await asyncio.gather(*owner.tasks.values())
            self.assertEqual(
                [call.args[0] for call in sleep.await_args_list],
                list(NOTICE_RETRY_DELAYS),
            )
            self.assertIn(PHONE_FAILURE_SENTENCE, create.call_args.args[1])
            self.assertEqual(owner.hass.services.async_call.await_count, 5)
        owner.close()

    async def test_unload_cancels_delivery_without_any_device_write(self):
        owner = self.owner("notify.synthetic")
        gate = asyncio.Event()
        owner.hass.services.async_call.side_effect = lambda *_args, **_kwargs: (
            gate.wait()
        )
        with patch(
            "custom_components.eufy_sdk.guard_confirmation.persistent_notification.async_create"
        ):
            owner.apply(receipt("not_reported"))
            tasks = list(owner.tasks.values())
            owner.close()
            await asyncio.gather(*tasks, return_exceptions=True)
        self.assertTrue(all(task.cancelled() for task in tasks))
