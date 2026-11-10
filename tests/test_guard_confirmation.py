"""Guard receipts never replace the panel state or replay commands."""

# ruff: noqa: ANN001, ANN201, D102, INP001, PT009, SLF001
import asyncio
import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import custom_components.eufy_sdk as integration
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
NEXT_RUNTIME = "00000000-0000-4000-8000-000000000002"


def lifecycle_replay() -> dict:
    """Read the synthetic replay produced by the offline bridge contract test."""
    return json.loads(
        Path(os.environ["EUFY_GUARD_LIFECYCLE_REPLAY"]).read_text(encoding="utf-8")
    )


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

    async def test_only_advertisements_replace_runtime_scope(
        self,
    ):
        client = self.client()
        client._dispatch_event({"event": "guardConfirmation", **receipt("reported")})
        next_receipt = receipt("not_reported", runtimeGeneration=NEXT_RUNTIME)
        client._dispatch_event({"event": "auth", "state": "ok"})
        client._dispatch_event({"event": "guardConfirmation", **next_receipt})
        self.assertEqual(
            client.guard_receipts["synthetic-station"]["outcome"], "reported"
        )
        client._dispatch_event(
            {"event": "guardRuntime", "version": 2, "runtimeGeneration": NEXT_RUNTIME}
        )
        self.assertEqual(client._guard_runtime, RUNTIME)
        client._dispatch_event(
            {"event": "guardRuntime", "version": 1, "runtimeGeneration": NEXT_RUNTIME}
        )
        self.assertEqual(client.guard_receipts, {})
        client._dispatch_event({"event": "guardConfirmation", **next_receipt})
        client._dispatch_event({"event": "guardConfirmation", **receipt("reported", 9)})
        self.assertEqual(client.guard_receipts["synthetic-station"], next_receipt)

    async def test_ack_started_before_runtime_rollover_cannot_repopulate_retired_scope(
        self,
    ):
        client = self.client()
        client._advertise_guard_runtime({"version": 1, "runtimeGeneration": RUNTIME})
        gate = asyncio.get_running_loop().create_future()
        client.rpc = AsyncMock(side_effect=lambda *_args, **_kwargs: None)

        async def delayed(*_args: object, **_kwargs: object) -> Any:
            return await gate

        client.rpc.side_effect = delayed
        task = asyncio.create_task(
            client.set_property("synthetic-station", "armingMode", 1)
        )
        await asyncio.sleep(0)
        client._dispatch_event(
            {"event": "guardRuntime", "version": 1, "runtimeGeneration": NEXT_RUNTIME}
        )
        gate.set_result({"guardConfirmation": receipt()})
        await task
        self.assertEqual(client.guard_receipts, {})
        client._on_event.assert_not_called()

    async def test_recovery_before_rollover_cannot_change_new_receipts(
        self,
    ):
        for recovered in (receipt("not_reported"), None):
            with self.subTest(recovered=recovered is None):
                client = self.client()
                client._dispatch_event({"event": "guardConfirmation", **receipt()})
                started = asyncio.Event()
                gate = asyncio.get_running_loop().create_future()

                async def delayed(
                    *_args: object,
                    started: asyncio.Event = started,
                    gate: asyncio.Future = gate,
                    **_kwargs: object,
                ) -> Any:
                    started.set()
                    return await gate

                client.rpc = AsyncMock(side_effect=delayed)
                task = asyncio.create_task(
                    client._recover_guard_receipts(
                        client._ws, client._connection_generation
                    )
                )
                await started.wait()
                client._dispatch_event(
                    {
                        "event": "guardRuntime",
                        "version": 1,
                        "runtimeGeneration": NEXT_RUNTIME,
                    }
                )
                next_receipt = receipt(runtimeGeneration=NEXT_RUNTIME)
                client._dispatch_event({"event": "guardConfirmation", **next_receipt})
                gate.set_result({"guardConfirmation": recovered})
                await task
                self.assertEqual(
                    client.guard_receipts["synthetic-station"], next_receipt
                )

    @unittest.skipUnless(
        os.environ.get("EUFY_GUARD_LIFECYCLE_REPLAY"),
        "requires offline SDK/bridge lifecycle replay",
    )
    async def test_sdk_bridge_watchdog_auth_replay_reaches_notifications(
        self,
    ):
        replay = lifecycle_replay()
        client = self.client()
        hass = SimpleNamespace(
            async_create_task=asyncio.create_task,
            services=SimpleNamespace(async_call=AsyncMock()),
        )
        owner = GuardNotifications(
            hass, {"light_safety_notify_service": "notify.synthetic"}
        )
        client._on_event = lambda event: (
            owner.apply(event) if event["event"] == "guardConfirmation" else None
        )
        with (
            patch(
                "custom_components.eufy_sdk.guard_confirmation.dr.async_get",
                return_value=SimpleNamespace(async_get_device=lambda **_kwargs: None),
            ),
            patch(
                "custom_components.eufy_sdk.guard_confirmation.persistent_notification.async_create"
            ) as create,
        ):
            for event in replay["events"]:
                if event["event"] == "hello":
                    client._set_guard_capability(event)
                else:
                    client._dispatch_event(event)
            await asyncio.gather(*owner.tasks.values())
            serial = replay["next"]["deviceSn"]
            self.assertEqual(client.guard_receipts[serial]["outcome"], "not_reported")
            self.assertEqual(client._guard_runtime, replay["next"]["runtimeGeneration"])
            self.assertEqual(create.call_count, 1)
            self.assertNotIn(serial, create.call_args.args[1])
            hass.services.async_call.assert_awaited_once()
            client._dispatch_event({"event": "guardConfirmation", **replay["old"]})
            self.assertEqual(client.guard_receipts[serial]["outcome"], "not_reported")
            self.assertEqual(replay["sends"], 2)
        owner.close()

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

    async def test_same_runtime_reconnect_recovers_terminal_missed_during_gap(self):
        cap = {"version": 1, "maxAttempts": 1, "enabledRetry": False}

        def hello(runtime) -> dict:
            return {
                "event": "hello",
                "guardConfirmation": cap,
                "guardRuntime": {"version": 1, "runtimeGeneration": runtime},
            }

        client = self.client()
        client._set_guard_capability(hello(RUNTIME))
        client._dispatch_event({"event": "guardConfirmation", **receipt()})
        revision = client._guard_scope_revision
        client._set_guard_capability(hello(RUNTIME))
        self.assertGreater(client._guard_scope_revision, revision)
        self.assertEqual(client._guard_runtime, RUNTIME)
        self.assertIn("synthetic-station", client.guard_receipts)
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
        client._set_guard_capability(hello(NEXT_RUNTIME))
        self.assertEqual(client.guard_receipts, {})

    async def test_legacy_hello_without_advertisement_keeps_recoverable_receipts(self):
        client = self.client()
        client._set_guard_capability(
            {
                "event": "hello",
                "guardConfirmation": {
                    "version": 1,
                    "maxAttempts": 1,
                    "enabledRetry": False,
                },
            }
        )
        client._dispatch_event({"event": "guardConfirmation", **receipt()})
        client._set_guard_capability(
            {
                "event": "hello",
                "guardConfirmation": {
                    "version": 1,
                    "maxAttempts": 1,
                    "enabledRetry": False,
                },
            }
        )
        self.assertIsNone(client._guard_runtime)
        self.assertIn("synthetic-station", client.guard_receipts)

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
        registry = SimpleNamespace(async_get_device=Mock(return_value=None))
        patcher = patch(
            "custom_components.eufy_sdk.guard_confirmation.dr.async_get",
            return_value=registry,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.registry = registry
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
            self.assertEqual(dismiss.call_count, 1)
        owner.close()

    async def test_station_problem_remains_bounded_and_counts_unresolved_intents(self):
        owner = self.owner()
        with (
            patch(
                "custom_components.eufy_sdk.guard_confirmation.persistent_notification.async_create"
            ) as create,
            patch(
                "custom_components.eufy_sdk.guard_confirmation.persistent_notification.async_dismiss"
            ) as dismiss,
        ):
            for generation in range(1, 1101):
                owner.apply(receipt("not_reported", generation))
            self.assertEqual(len(owner.seen), 1024)
            self.assertEqual(len(owner.notices), 1)
            self.assertEqual(len(owner.latest), 1)
            self.assertEqual(
                len({call.kwargs["notification_id"] for call in create.call_args_list}),
                1,
            )
            self.assertIn("1100 unresolved intent(s)", create.call_args.args[1])
            owner.apply(receipt("not_reported", 1))
            self.assertEqual(owner.unresolved["synthetic-station"], 1100)
            owner.apply(receipt("reported", 1101))
            dismiss.assert_called_once()
            self.assertEqual(owner.notices, {})
            self.assertEqual(owner.unresolved, {})
        owner.close()

    async def test_station_capacity_bounds_all_bookkeeping(self):
        owner = self.owner()
        with patch(
            "custom_components.eufy_sdk.guard_confirmation.persistent_notification.async_create"
        ):
            for generation in range(1, 1101):
                owner.apply(
                    receipt(
                        "not_reported", generation, deviceSn=f"synthetic-{generation}"
                    )
                )
        for mapping in (owner.latest, owner.notices, owner.unresolved, owner.seen):
            self.assertEqual(len(mapping), 1024)
        owner.close()

    async def test_cancelled_or_superseded_receipts_do_not_notify(
        self,
    ):
        owner = self.owner("notify.synthetic")
        client = GuardConfirmationTests.client(self)
        client._on_event = owner.apply
        with patch(
            "custom_components.eufy_sdk.guard_confirmation.persistent_notification.async_create"
        ) as create:
            for generation, reason in enumerate(("superseded", "cancelled"), 1):
                client._dispatch_event(
                    {
                        "event": "guardConfirmation",
                        **receipt("not_reported", generation, reason=reason),
                    }
                )
            create.assert_not_called()
            owner.hass.services.async_call.assert_not_awaited()
            self.assertEqual(owner.tasks, {})
            self.assertEqual(
                guard_attributes(client, "synthetic-station")["guard_reason"],
                "cancelled",
            )
        owner.close()

    async def test_friendly_names_mode_labels_and_outcome_specific_text_exclude_serial(
        self,
    ):
        owner = self.owner("notify.synthetic")
        serial = "synthetic-sensitive-station-serial"
        cases = [
            ("User station", "Default station", "User station"),
            (None, "Default station", "Default station"),
            (None, None, "HomeBase"),
            (serial, serial, "HomeBase"),
        ]
        with patch(
            "custom_components.eufy_sdk.guard_confirmation.persistent_notification.async_create"
        ) as create:
            for generation, (user, name, expected) in enumerate(cases, 1):
                self.registry.async_get_device.return_value = SimpleNamespace(
                    name_by_user=user, name=name
                )
                outcome = "contradicted" if generation % 2 else "not_reported"
                owner.apply(
                    receipt(outcome, generation, deviceSn=serial, requestedMode=47)
                )
                await asyncio.gather(*owner.tasks.values())
                persistent = create.call_args.args[1]
                phone = owner.hass.services.async_call.await_args.args[2]["message"]
                for message in (persistent, phone):
                    self.assertNotIn(serial, message)
                    self.assertIn(expected, message)
                    self.assertIn("Geofencing", message)
                    self.assertIn(
                        "reported a different guard mode"
                        if outcome == "contradicted"
                        else "No matching report was received in time",
                        message,
                    )
                self.assertNotIn(serial, create.call_args.kwargs["notification_id"])
        owner.close()

    async def test_notice_mode_labels_match_select_manifest_and_known_fallbacks(self):
        owner = self.owner()
        spec = {"name": "armingMode", "enumValues": {"47": "geo"}, "type": "enum"}
        coordinator = SimpleNamespace(
            data={"synthetic-station": {"state": {"armingMode": 47}}}
        )
        select = EufySdkSelect(coordinator, "synthetic-station", spec)
        owner.entry = SimpleNamespace(
            runtime_data=SimpleNamespace(properties={"synthetic-station": [spec]})
        )
        self.assertEqual(
            owner._mode_label("synthetic-station", 47), select.current_option
        )
        for mode, label in {
            0: "Away",
            1: "Home",
            2: "Schedule",
            3: "Custom 1",
            4: "Custom 2",
            5: "Custom 3",
            6: "Off",
            63: "Disarmed",
            99: "mode 99",
        }.items():
            self.assertEqual(owner._mode_label("synthetic-station", mode), label)
        owner.close()

    async def test_live_options_enable_change_disable_reconcile_pending_phone_retries(
        self,
    ):
        owner = self.owner()
        entry = SimpleNamespace(options={}, data={}, entry_id="synthetic-entry")
        entry.runtime_data = SimpleNamespace(
            options_snapshot={},
            entry_data_snapshot={},
            guard_notifications=owner,
            coordinator=SimpleNamespace(_light_safety_controllers=set()),
        )
        owner.hass.config_entries = SimpleNamespace(async_reload=AsyncMock())

        async def update(service: str | None) -> None:
            entry.options = {"light_safety_notify_service": service} if service else {}
            await integration._async_reload_on_update(owner.hass, entry)

        with patch(
            "custom_components.eufy_sdk.guard_confirmation.persistent_notification.async_create"
        ):
            owner.apply(receipt("not_reported"))
            self.assertEqual(owner.tasks, {})
            await update("notify.synthetic_old")
            await asyncio.gather(*owner.tasks.values())
            self.assertEqual(
                owner.hass.services.async_call.await_args.args[:2],
                ("notify", "synthetic_old"),
            )
            owner.hass.services.async_call.side_effect = RuntimeError("synthetic")
            sleeping = asyncio.Event()
            sleep_gate = asyncio.Event()

            async def retry_wait(_delay: float) -> None:
                sleeping.set()
                await sleep_gate.wait()

            with patch(
                "custom_components.eufy_sdk.guard_confirmation.asyncio.sleep",
                side_effect=retry_wait,
            ):
                owner.apply(receipt("not_reported", 2))
                await sleeping.wait()
                retired = list(owner.tasks.values())
                owner.hass.services.async_call.side_effect = None
                await update("notify.synthetic_new")
                await asyncio.gather(*retired, return_exceptions=True)
                self.assertTrue(all(task.cancelled() for task in retired))
                await asyncio.gather(*owner.tasks.values())
                self.assertEqual(
                    owner.hass.services.async_call.await_args.args[:2],
                    ("notify", "synthetic_new"),
                )
                sleeping.clear()
                owner.hass.services.async_call.side_effect = RuntimeError("synthetic")
                owner.apply(receipt("not_reported", 3))
                await sleeping.wait()
                retired = list(owner.tasks.values())
                await update(None)
                await asyncio.gather(*retired, return_exceptions=True)
                self.assertTrue(all(task.cancelled() for task in retired))
                self.assertEqual(owner.tasks, {})
            owner.hass.services.async_call.reset_mock()
            owner.apply(receipt("not_reported", 4))
            owner.hass.services.async_call.assert_not_awaited()
        owner.hass.config_entries.async_reload.assert_not_awaited()
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
