"""Offline regressions for lost guard intents and late matching state."""

# ruff: noqa: ANN001, ANN201, D102, INP001, PT009, SLF001
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from test_guard_confirmation import NEXT_RUNTIME, RUNTIME, receipt

from custom_components.eufy_sdk.api import EufySdkApiClient
from custom_components.eufy_sdk.guard_confirmation import (
    GuardNotifications,
    guard_attributes,
)

SERIAL = "synthetic-station"


class GuardReconciliationTests(unittest.IsolatedAsyncioTestCase):
    """Exercise production receipt, state and notification wiring without I/O."""

    def setUp(self):
        self.now = 0.0
        clock = SimpleNamespace(monotonic=lambda: self.now, time=lambda: self.now)
        for module in ("api", "guard_confirmation"):
            patcher = patch(f"custom_components.eufy_sdk.{module}.time", clock)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = EufySdkApiClient("synthetic", 3000, Mock())
        self.client._ws = Mock(closed=False)
        self.client.guard_confirmation_supported = True
        self.client._advertise_guard_runtime(
            {"version": 1, "runtimeGeneration": RUNTIME}
        )
        self.addCleanup(self.client.guard_reconciliation.close)
        self.hass = SimpleNamespace(
            async_create_task=asyncio.create_task,
            services=SimpleNamespace(async_call=AsyncMock()),
        )
        entry = SimpleNamespace(runtime_data=SimpleNamespace(client=self.client))
        self.owner = GuardNotifications(
            self.hass, {"light_safety_notify_service": "notify.synthetic"}, entry
        )
        self.addCleanup(self.owner.close)
        self.client._on_event = lambda event: (
            self.owner.apply(event) if event["event"] == "guardConfirmation" else None
        )
        for name, value in (
            ("dr.async_get", SimpleNamespace(async_get_device=lambda **_kwargs: None)),
            ("persistent_notification.async_create", None),
            ("persistent_notification.async_dismiss", None),
        ):
            patcher = patch(
                f"custom_components.eufy_sdk.guard_confirmation.{name}",
                **({"return_value": value} if value is not None else {}),
            )
            mocked = patcher.start()
            self.addCleanup(patcher.stop)
            if name.endswith("async_create"):
                self.create = mocked

    def event(self, outcome="pending", **fields: object):
        self.client._dispatch_event(
            {"event": "guardConfirmation", **receipt(outcome, **fields)}
        )

    def mode(self, mode, **fields: object):
        self.client._dispatch_event(
            {"event": "armingModeChanged", "deviceSn": SERIAL, "mode": mode, **fields}
        )

    def restart(self):
        self.now += 1
        self.client._dispatch_event(
            {"event": "guardRuntime", "version": 1, "runtimeGeneration": NEXT_RUNTIME}
        )

    def finish_grace(self):
        for key, (_, timer) in tuple(self.owner.deferred.items()):
            timer.cancel()
            self.owner._finish_deferred(key, self.client.guard_reconciliation)

    async def deliveries(self):
        await asyncio.gather(*self.owner.tasks.values())

    async def test_restart_matching_fresh_snapshot_resolves_without_alert(self):
        self.event()
        self.mode(0)
        self.restart()
        self.now += 1
        self.client.rpc = AsyncMock(
            return_value={"devices": [{"sn": SERIAL, "state": {"armingMode": 1}}]}
        )
        await self.client.list_devices()
        self.assertEqual(
            guard_attributes(self.client, SERIAL)["guard_outcome"], "reported"
        )
        self.assertEqual(
            guard_attributes(self.client, SERIAL)["guard_reason"],
            "reported_after_command",
        )
        self.assertEqual(self.client.guard_reconciliation.timers, {})
        self.create.assert_not_called()
        self.hass.services.async_call.assert_not_awaited()
        self.client.rpc.assert_awaited_once_with("devices.list")

    async def test_restart_different_fresh_state_alerts_with_restart_wording(self):
        self.event()
        self.restart()
        self.now += 1
        self.mode(0)
        self.assertEqual(self.client.guard_receipts[SERIAL]["reason"], "runtime_closed")
        self.finish_grace()
        await self.deliveries()
        self.create.assert_called_once()
        self.assertIn(
            "mode change could not be confirmed after a restart",
            self.create.call_args.args[1],
        )
        self.hass.services.async_call.assert_awaited_once()

    async def test_restart_unknown_state_is_bounded_and_does_not_reuse_cached_match(
        self,
    ):
        self.event()
        self.mode(1)
        self.restart()
        timer = self.client.guard_reconciliation.timers[SERIAL]
        self.assertAlmostEqual(timer.when() - asyncio.get_running_loop().time(), 60, 1)
        self.now += 59
        self.mode(None)
        self.create.assert_not_called()
        timer.cancel()
        self.client.guard_reconciliation._expire_loss(SERIAL)
        self.finish_grace()
        await self.deliveries()
        self.create.assert_called_once()
        self.hass.services.async_call.assert_awaited_once()

    async def test_null_recovery_retains_pending_until_fresh_mode_or_deadline(self):
        for generation, mode in enumerate((1, 0, None), 1):
            with self.subTest(mode=mode):
                self.now += 1
                self.event(generation=generation)
                self.client.rpc = AsyncMock(
                    side_effect=[
                        {"devices": [{"sn": SERIAL, "state": {"armingMode": mode}}]},
                        {"guardConfirmation": None},
                    ]
                )
                await self.client.list_devices()
                self.assertEqual(
                    self.client.guard_receipts[SERIAL]["outcome"],
                    "reported"
                    if mode == 1
                    else "not_reported"
                    if mode == 0
                    else "pending",
                )
                self.assertEqual(
                    [call.args[0] for call in self.client.rpc.await_args_list],
                    ["devices.list", "guard.confirmation.get"],
                )

    async def test_new_intent_retires_lost_reconciliation_and_old_callbacks(self):
        self.event()
        self.restart()
        retired = self.client.guard_reconciliation.timers[SERIAL]
        self.event(runtimeGeneration=NEXT_RUNTIME, requestedMode=0)
        self.assertTrue(retired.cancelled())
        self.client.guard_reconciliation._expire_loss(SERIAL)
        self.assertEqual(self.client.guard_receipts[SERIAL]["outcome"], "pending")
        self.create.assert_not_called()

    async def test_repeated_restart_fences_evidence_without_resetting_deadline(self):
        self.event()
        self.restart()
        timer = self.client.guard_reconciliation.timers[SERIAL]
        self.client.guard_reconciliation.observe(SERIAL, 1, 0)
        self.now += 20
        self.client._advertise_guard_runtime(
            {"version": 1, "runtimeGeneration": RUNTIME}
        )
        self.assertIs(self.client.guard_reconciliation.timers[SERIAL], timer)
        self.assertFalse(self.client.guard_reconciliation.matches(receipt()))

    async def test_inflight_snapshot_before_restart_cannot_confirm_lost_intent(self):
        self.event()

        async def snapshot(*_args: object, **_kwargs: object) -> dict:
            self.restart()
            return {"devices": [{"sn": SERIAL, "state": {"armingMode": 1}}]}

        self.client.rpc = AsyncMock(side_effect=snapshot)
        await self.client.list_devices()
        self.assertEqual(self.client.guard_receipts[SERIAL]["outcome"], "pending")
        self.assertIn(SERIAL, self.client.guard_reconciliation.timers)

    async def test_undispatched_match_keeps_attributes_and_logs_unclear_mapping(self):
        self.event(attempts=0, reason="waiting_dispatch")
        self.restart()
        with self.assertLogs("custom_components.eufy_sdk", level="WARNING"):
            self.mode(1)
        self.assertEqual(self.client.guard_receipts[SERIAL]["outcome"], "pending")
        self.create.assert_not_called()

    async def test_tonight_session_change_late_matching_report_suppresses_both_notices(
        self,
    ):
        # Relative seconds from 22:35:00Z: command .6, terminal .9, report .4.
        self.now = 32.6
        self.client.rpc = AsyncMock(return_value={"guardConfirmation": receipt()})
        await self.client.set_property(SERIAL, "armingMode", 1)
        self.now = 34.9
        terminal = receipt("not_reported", reason="session_changed", outcomeAtMs=34900)
        self.client._dispatch_event({"event": "guardConfirmation", **terminal})
        self.create.assert_not_called()
        timer = next(iter(self.owner.deferred.values()))[1]
        self.assertAlmostEqual(timer.when() - asyncio.get_running_loop().time(), 10, 1)
        self.now = 36.4
        self.mode(1)
        self.now = 44.9
        self.finish_grace()
        await self.deliveries()
        self.assertEqual(self.client.guard_receipts[SERIAL], terminal)
        self.create.assert_not_called()
        self.hass.services.async_call.assert_not_awaited()

    async def test_fresh_matching_state_before_terminal_suppresses_immediately(self):
        self.event()
        self.now += 1
        self.mode(1)
        self.event("not_reported")
        self.assertEqual(self.owner.deferred, {})
        self.create.assert_not_called()

    async def test_valid_property_push_suppresses_after_grace(self):
        self.event()
        self.event("not_reported")
        self.now += 1
        self.client._dispatch_event(
            {
                "event": "propertyChanged",
                "deviceSn": SERIAL,
                "property": "armingMode",
                "value": 1,
            }
        )
        self.finish_grace()
        self.create.assert_not_called()

    async def test_rtc_observations_use_source_time_and_reject_stale_evidence(self):
        self.now = 30
        self.event()
        self.event("not_reported")
        self.now = 32
        observation = {
            "event": "propertiesObserved",
            "deviceSn": SERIAL,
            "source": "rtc",
            "observedAtMs": 29000,
            "properties": {"armingMode": 1},
        }
        for fields in (
            {},
            {"source": "cloud", "observedAtMs": 31000},
            {"observedAtMs": 33000},
            {"observedAtMs": True},
            {"observedAtMs": float("inf")},
            {"observedAtMs": 0},
        ):
            self.client._dispatch_event({**observation, **fields})
            self.assertFalse(self.client.guard_reconciliation.matches(receipt()))
        self.client._dispatch_event({**observation, "observedAtMs": 31000})
        self.finish_grace()
        self.create.assert_not_called()

    async def test_stale_snapshot_cannot_replace_newer_different_mode(self):
        self.event()
        self.now = 2
        self.mode(0)
        self.client.guard_reconciliation.observe(SERIAL, 1, 1)
        self.event("not_reported")
        self.finish_grace()
        await self.deliveries()
        self.create.assert_called_once()

    async def test_recovery_uses_fresh_snapshot_before_considering_cached_match(self):
        self.event()
        self.now = 1
        self.mode(1)
        self.now = 2
        self.client.rpc = AsyncMock(
            side_effect=[
                {"devices": [{"sn": SERIAL, "state": {"armingMode": 0}}]},
                {"guardConfirmation": receipt("not_reported")},
            ]
        )
        await self.client.list_devices()
        self.assertEqual(len(self.owner.deferred), 1)
        self.finish_grace()
        await self.deliveries()
        self.create.assert_called_once()

    async def test_failed_local_rpc_cannot_lend_its_start_to_a_remote_intent(self):
        self.client.rpc = AsyncMock(side_effect=RuntimeError("synthetic failure"))
        with pytest.raises(RuntimeError, match="synthetic failure"):
            await self.client.set_property(SERIAL, "armingMode", 1)
        self.assertEqual(self.client.guard_reconciliation.preparing, {})
        self.now = 1
        self.mode(1)
        self.now = 2
        self.event()
        self.event("not_reported")
        self.finish_grace()
        await self.deliveries()
        self.create.assert_called_once()

    async def test_old_rpc_cleanup_cannot_retire_newer_preparation(self):
        reconciliation = self.client.guard_reconciliation
        old = reconciliation.begin(SERIAL)
        new = reconciliation.begin(SERIAL)
        reconciliation.end(SERIAL, old)
        self.assertIs(reconciliation.preparing[SERIAL], new)
        reconciliation.end(SERIAL, new)
        self.assertEqual(reconciliation.preparing, {})

    async def test_current_coordinator_mode_must_agree_with_fresh_evidence(self):
        self.event()
        self.now += 1
        self.mode(1)
        self.owner.entry.runtime_data.coordinator = SimpleNamespace(
            data={SERIAL: {"state": {"armingMode": 0}}}, last_update_success=True
        )
        self.event("not_reported")
        self.finish_grace()
        await self.deliveries()
        self.create.assert_called_once()

    async def test_unknown_restart_deadline_has_no_additional_ten_second_delay(self):
        self.event()
        self.restart()
        timer = self.client.guard_reconciliation.timers[SERIAL]
        self.now += 60
        timer.cancel()
        self.client.guard_reconciliation._expire_loss(SERIAL)
        delivery_timer = next(iter(self.owner.deferred.values()))[1]
        self.assertAlmostEqual(
            delivery_timer.when() - asyncio.get_running_loop().time(), 0, 1
        )
        self.finish_grace()
        await self.deliveries()
        self.create.assert_called_once()

    async def test_cached_precommand_match_cannot_suppress_failure(self):
        self.client.guard_reconciliation.begin(SERIAL)
        self.mode(1)
        self.now += 1
        self.client.rpc = AsyncMock(return_value={"guardConfirmation": receipt()})
        await self.client.set_property(SERIAL, "armingMode", 1)
        self.event("not_reported", reason="session_changed")
        self.finish_grace()
        await self.deliveries()
        self.create.assert_called_once()
        self.hass.services.async_call.assert_awaited_once()

    async def test_nonmatching_report_during_grace_delivers_once(self):
        self.event()
        self.event("not_reported")
        self.now += 1
        self.mode(0)
        self.event("not_reported")
        self.finish_grace()
        await self.deliveries()
        self.create.assert_called_once()
        self.hass.services.async_call.assert_awaited_once()

    async def test_arming_field_is_selected_mode_under_schedule(self):
        self.event(requestedMode=2)
        self.now += 1
        self.mode(1, arming=2)
        self.event("not_reported", requestedMode=2)
        self.assertEqual(self.owner.deferred, {})
        self.create.assert_not_called()

    async def test_invalid_and_enforced_only_modes_do_not_suppress(self):
        self.event(requestedMode=2)
        self.now += 1
        self.mode(2, arming=1)
        self.event("not_reported", requestedMode=2)
        self.finish_grace()
        await self.deliveries()
        self.create.assert_called_once()

    async def test_new_pending_does_not_erase_older_real_failure(self):
        self.event()
        self.event("not_reported")
        self.event(generation=2)
        self.now += 1
        self.mode(1)
        self.finish_grace()
        await self.deliveries()
        self.create.assert_called_once()

    async def test_new_reported_intent_cancels_older_deferred_failure(self):
        self.event()
        self.event("not_reported")
        timer = next(iter(self.owner.deferred.values()))[1]
        self.event("reported", generation=2)
        self.assertTrue(timer.cancelled())
        self.assertEqual(self.owner.deferred, {})
        self.create.assert_not_called()

    async def test_silent_reasons_and_contradictions_have_no_grace(self):
        for generation, reason in enumerate(("superseded", "cancelled"), 1):
            self.event("not_reported", generation=generation, reason=reason)
        self.assertEqual(self.owner.deferred, {})
        self.create.assert_not_called()
        self.event("contradicted", generation=3)
        await self.deliveries()
        self.create.assert_called_once()

    async def test_option_changes_preserve_pending_grace_and_use_current_service(self):
        self.event()
        self.event("not_reported")
        self.owner.options_updated({"light_safety_notify_service": "notify.changed"})
        self.finish_grace()
        await self.deliveries()
        self.assertEqual(
            self.hass.services.async_call.await_args.args[:2], ("notify", "changed")
        )

    async def test_unload_cancels_loss_and_notification_timers(self):
        self.event()
        self.restart()
        loss_timer = self.client.guard_reconciliation.timers[SERIAL]
        self.owner.apply(receipt("not_reported", generation=2))
        notice_timer = next(iter(self.owner.deferred.values()))[1]
        self.owner.close()
        self.client._ws.close = AsyncMock()
        await self.client.close()
        self.assertTrue(loss_timer.cancelled())
        self.assertTrue(notice_timer.cancelled())
        self.create.assert_not_called()

    async def test_all_failure_reasons_have_specific_friendly_text(self):
        expected = {
            "dispatch_rejected": "The station rejected the command.",
            "ack_missing": "The command may not have reached the station.",
            "transport_uncertain": "The command may not have reached the station.",
            "runtime_closed": "The add-on restarted before a report.",
            "session_changed": "The connection changed before a report.",
            "window_expired": "No report was received within the time window.",
            "budget_expired": "No report was received within the time window.",
        }
        for generation, (reason, sentence) in enumerate(expected.items(), 1):
            self.event("not_reported", generation=generation, reason=reason)
            self.finish_grace()
            await self.deliveries()
            message = self.create.call_args.args[1]
            self.assertIn(sentence, message)
            self.assertIn("HomeBase, requested guard mode Home.", message)
            self.assertNotIn(SERIAL, message)
            self.assertNotIn("intent(s)", message)
            if generation == 1:
                self.assertNotIn("unresolved intent", message)
            else:
                self.assertIn(f"{generation} unresolved intents.", message)
            self.assertIn(
                sentence, self.hass.services.async_call.await_args.args[2]["message"]
            )
