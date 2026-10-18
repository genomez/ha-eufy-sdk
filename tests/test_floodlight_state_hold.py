"""Keep floodlight command presentation stable while uncorrelated reports settle."""

# Modified for genomez S1 experimental source preview, 2026-10-17.
# Light RPC success presents the request while contrary reports remain bounded.
# ruff: noqa: ANN001, ANN201, ANN202, D102, FBT003, INP001, PT009, PT027, SLF001

import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, Mock, patch

from homeassistant.core import HomeAssistant

import custom_components.eufy_sdk as integration
from custom_components.eufy_sdk.const import CONF_HOST, CONF_PORT
from custom_components.eufy_sdk.entity import (
    LIGHT_CONTRADICTION_SECS,
    POST_WRITE_REFRESH_SECS,
)
from custom_components.eufy_sdk.property_sync import (
    apply_observation_event,
    apply_property_event,
)
from custom_components.eufy_sdk.switch import EufySdkSwitch

ROOT = str(Path(__file__).resolve().parents[1])


class FloodlightStateHoldTests(unittest.IsolatedAsyncioTestCase):
    """Exercise real entity/cache methods with synthetic RPC and timer boundaries."""

    def _entity(self, *, initial=False, prop="light"):
        offline_timer = patch(
            "custom_components.eufy_sdk.entity.async_call_later", return_value=Mock()
        )
        self.offline_timer = offline_timer.start()
        self.addCleanup(offline_timer.stop)
        client = SimpleNamespace(
            set_property=AsyncMock(), bounded_state_writes_supported=False
        )
        coordinator = SimpleNamespace(
            data={"fixture-device": {"state": {prop: initial}}},
            config_entry=SimpleNamespace(runtime_data=SimpleNamespace(client=client)),
            async_request_refresh=AsyncMock(),
            async_config_entry_first_refresh=AsyncMock(),
            last_update_success=True,
            snapshot_generation=0,
            completed_snapshot_generation=0,
            property_observations=None,
            record_state_event=Mock(),
            async_add_listener=Mock(return_value=Mock()),
            _clear_fast_retry=Mock(),
        )
        entity = EufySdkSwitch(
            coordinator, "fixture-device", {"name": prop, "type": "bool"}
        )
        entity.hass = Mock()
        entity.async_on_remove = Mock()
        seen = []

        def render():
            value = entity.is_on
            if not seen or seen[-1] != value:
                seen.append(value)

        entity.async_write_ha_state = Mock(side_effect=render)
        coordinator.async_update_listeners = entity._handle_coordinator_update
        return entity, coordinator, client, seen

    def _report(self, entity, value):
        data = {
            "event": "propertyChanged",
            "deviceSn": "fixture-device",
            "property": entity._prop,
            "value": value,
        }
        self.assertTrue(apply_property_event(entity.coordinator, data))
        entity._handle_property_changed(SimpleNamespace(data=data))

    def _receipt(self, entity, value):
        with patch("custom_components.eufy_sdk.property_sync.time", return_value=100):
            self.assertTrue(
                apply_observation_event(
                    entity.coordinator,
                    {
                        "event": "propertiesObserved",
                        "deviceSn": "fixture-device",
                        "source": "rtc",
                        "observedAtMs": 100000,
                        "properties": {entity._prop: value},
                    },
                )
            )

    async def _matching_then_stale(self, target):
        entity, coordinator, _client, seen = self._entity(initial=not target)
        cancel = Mock()
        with patch(
            "custom_components.eufy_sdk.entity.async_call_later", return_value=cancel
        ) as timer:
            await entity.write(target)
        owner = entity._optimistic_owner
        self._report(entity, target)
        self._receipt(entity, not target)
        self.assertIs(coordinator.data["fixture-device"]["state"]["light"], not target)
        self.assertEqual(
            entity.extra_state_attributes,
            {
                "device_observed_at": 100,
                "device_observed_value": not target,
                "device_observation_source": "rtc",
            },
        )
        self._report(entity, target)
        self.assertEqual(seen, [target])
        self.assertIsNone(entity._optimistic_owner)
        self.assertEqual(entity._light_guard_owner, owner)
        self.assertIs(entity._post_write_unsub, cancel)
        self.assertEqual(entity._post_write_owner, owner)
        self.assertEqual(entity._report_generation, 2)
        self.assertEqual(timer.call_count, 2)
        timer.assert_any_call(entity.hass, POST_WRITE_REFRESH_SECS, ANY)
        timer.assert_any_call(entity.hass, 30, ANY)
        cancel.assert_not_called()

    async def test_on_matching_report_then_stale_receipt_has_no_reversal(self):
        await self._matching_then_stale(True)

    async def test_off_matching_report_then_stale_receipt_has_no_reversal(self):
        await self._matching_then_stale(False)

    async def test_contradictory_report_updates_cache_and_receipt_while_held(self):
        for target in (False, True):
            with self.subTest(target=target):
                entity, coordinator, _client, seen = self._entity(initial=not target)
                cancel = Mock()
                with patch(
                    "custom_components.eufy_sdk.entity.async_call_later",
                    return_value=cancel,
                ):
                    await entity.write(target)
                self._report(entity, not target)
                self.assertIs(
                    coordinator.data["fixture-device"]["state"]["light"], not target
                )
                coordinator.record_state_event.assert_called_with(
                    "fixture-device", {"light": not target}
                )
                # A property change does not fabricate a device observation receipt.
                self.assertEqual(entity.extra_state_attributes, {})
                self._receipt(entity, not target)
                self.assertIs(
                    entity.extra_state_attributes["device_observed_value"], not target
                )
                self.assertEqual(seen, [target])
                cancel.assert_not_called()

    async def test_ineffective_write_debounces_contradictory_new_snapshot(self):
        entity, coordinator, _client, seen = self._entity()
        tasks = []
        entity.hass.async_create_task.side_effect = lambda coro: tasks.append(
            asyncio.create_task(coro)
        )

        async def refresh():
            coordinator.snapshot_generation += 1
            coordinator.completed_snapshot_generation = coordinator.snapshot_generation
            coordinator.async_update_listeners()

        coordinator.async_request_refresh.side_effect = refresh
        with patch(
            "custom_components.eufy_sdk.entity.async_call_later", return_value=Mock()
        ) as timer:
            await entity.write(True)
            self._report(entity, False)
            self.assertEqual(seen, [True])
            next(
                call.args[2]
                for call in timer.call_args_list
                if call.args[1] == POST_WRITE_REFRESH_SECS
            )(None)
            await asyncio.gather(*tasks)
            self.assertEqual(seen, [True])
            self.assertEqual(timer.call_args.args[1], 8)
            timer.call_args.args[2](None)
        self.assertEqual(seen, [True, False])
        self.assertIsNone(entity._optimistic_owner)
        self.assertIsNone(entity._post_write_unsub)
        coordinator.async_request_refresh.assert_awaited_once()

    async def test_queued_refresh_and_reports_wait_for_newer_completed_snapshot(self):
        entity, coordinator, _client, seen = self._entity()
        coordinator.snapshot_generation = 5
        coordinator.completed_snapshot_generation = 4
        with patch(
            "custom_components.eufy_sdk.entity.async_call_later", return_value=Mock()
        ):
            await entity.write(True)
        owner = entity._optimistic_owner
        await entity._reconcile(owner)
        pending = entity._pending_reconcile
        self._report(entity, False)
        self._receipt(entity, False)
        self.assertEqual(entity._pending_reconcile, pending)
        coordinator.completed_snapshot_generation = 5
        entity._handle_coordinator_update()
        self.assertEqual(seen, [True])
        coordinator.snapshot_generation = coordinator.completed_snapshot_generation = 6
        entity._handle_coordinator_update()
        self.assertEqual(seen, [True])
        self.assertEqual(self.offline_timer.call_args.args[1], 8)
        self.offline_timer.call_args.args[2](None)
        self.assertEqual(seen, [True, False])
        self.assertIsNone(entity._optimistic_owner)

    async def test_rpc_error_installs_no_hold_or_success(self):
        entity, _coordinator, client, seen = self._entity()
        client.set_property.side_effect = RuntimeError("synthetic rejection")
        with (
            patch("custom_components.eufy_sdk.entity.async_call_later") as timer,
            self.assertRaisesRegex(RuntimeError, "synthetic rejection"),
        ):
            await entity.write(True)
        self.assertIsNone(entity._optimistic_owner)
        self.assertIsNone(entity._assumed_value)
        self.assertFalse(entity.is_on)
        self.assertEqual(seen, [])
        timer.assert_not_called()

    async def test_cancelled_rpc_installs_no_hold(self):
        entity, _coordinator, client, seen = self._entity()
        gate = asyncio.Event()

        async def pending(*_args: object):
            await gate.wait()

        client.set_property.side_effect = pending
        with patch("custom_components.eufy_sdk.entity.async_call_later") as timer:
            task = asyncio.create_task(entity.write(True))
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertIsNone(entity._optimistic_owner)
        self.assertFalse(entity.is_on)
        self.assertEqual(seen, [])
        timer.assert_not_called()

    async def test_light_report_during_rpc_starts_bounded_contrary_candidate(self):
        entity, _coordinator, client, seen = self._entity()
        gate = asyncio.Event()

        async def pending(*_args: object):
            await gate.wait()

        client.set_property.side_effect = pending
        with patch(
            "custom_components.eufy_sdk.entity.async_call_later", return_value=Mock()
        ) as timer:
            task = asyncio.create_task(entity.write(True))
            await asyncio.sleep(0)
            self._report(entity, False)
            gate.set()
            await task
        self.assertEqual(entity._optimistic_owner, 1)
        self.assertEqual(seen, [False, True])
        contrary = next(
            call.args[2]
            for call in timer.call_args_list
            if call.args[1] == LIGHT_CONTRADICTION_SECS
        )
        contrary(None)
        self.assertEqual(seen, [False, True, False])
        self.assertIsNone(entity._optimistic_owner)

    async def test_non_light_reports_still_release_immediately(self):
        for prop in ("enabled", "lightPower"):
            with self.subTest(prop=prop):
                entity, _coordinator, _client, seen = self._entity(prop=prop)
                cancel = Mock()
                with patch(
                    "custom_components.eufy_sdk.entity.async_call_later",
                    return_value=cancel,
                ):
                    await entity.write(True)
                self._report(entity, False)
                self.assertEqual(seen, [True, False])
                self.assertIsNone(entity._optimistic_owner)
                self.assertIsNone(entity._post_write_owner)
                cancel.assert_called_once()

    async def test_removal_clears_hold_and_invalidates_queued_timer(self):
        entity, coordinator, _client, seen = self._entity()
        cancel = Mock()
        with patch(
            "custom_components.eufy_sdk.entity.async_call_later", return_value=cancel
        ) as timer:
            await entity.write(True)
        await entity.async_will_remove_from_hass()
        timer.call_args.args[2](None)
        self._report(entity, False)
        self.assertIsNone(entity._optimistic_owner)
        self.assertIsNone(entity._assumed_value)
        self.assertIsNone(entity._post_write_unsub)
        self.assertFalse(entity.is_on)
        self.assertEqual(seen, [True])
        self.assertEqual(cancel.call_count, 2)
        entity.hass.async_create_task.assert_not_called()
        coordinator.async_request_refresh.assert_not_awaited()

    async def test_overlapping_rpcs_keep_latest_successful_owner(self):
        entity, _coordinator, client, seen = self._entity()
        gate = asyncio.Event()

        async def pending(_sn, _prop, value):
            if value:
                await gate.wait()

        client.set_property.side_effect = pending
        cancel = Mock()
        with patch(
            "custom_components.eufy_sdk.entity.async_call_later", return_value=cancel
        ) as timer:
            older = asyncio.create_task(entity.write(True))
            await asyncio.sleep(0)
            await entity.write(False)
            self._report(entity, True)
            gate.set()
            await older
        self.assertEqual(entity._optimistic_owner, 2)
        self.assertEqual(entity._post_write_owner, 2)
        self.assertEqual(seen, [False])
        self.assertEqual(timer.call_count, 3)
        cancel.assert_not_called()

    async def test_repeated_writes_replace_timer_and_ignore_old_callback(self):
        entity, _coordinator, _client, seen = self._entity()
        first_cancel, second_cancel = Mock(), Mock()
        with patch(
            "custom_components.eufy_sdk.entity.async_call_later",
            side_effect=[Mock(), first_cancel, Mock(), second_cancel, Mock()],
        ) as timer:
            await entity.write(True)
            first_callback = timer.call_args.args[2]
            await entity.write(False)
            self._report(entity, True)
            first_callback(None)
        self.assertEqual(seen, [True, False])
        self.assertEqual(entity._optimistic_owner, 2)
        self.assertIs(entity._post_write_unsub, second_cancel)
        first_cancel.assert_called_once()
        second_cancel.assert_not_called()
        entity.hass.async_create_task.assert_not_called()

    async def test_failed_newer_write_preserves_prior_successful_hold(self):
        entity, _coordinator, client, seen = self._entity()
        cancel = Mock()
        with patch(
            "custom_components.eufy_sdk.entity.async_call_later", return_value=cancel
        ) as timer:
            await entity.write(True)
            client.set_property.side_effect = RuntimeError("synthetic rejection")
            with self.assertRaisesRegex(RuntimeError, "synthetic rejection"):
                await entity.write(False)
            self._report(entity, False)
        self.assertEqual(seen, [True])
        self.assertEqual(entity._optimistic_owner, 1)
        self.assertEqual(timer.call_count, 3)
        cancel.assert_not_called()

    async def test_refresh_failure_still_has_no_hard_hold_expiry(self):
        for raises in (False, True):
            with self.subTest(raises=raises):
                entity, coordinator, _client, seen = self._entity()
                with patch(
                    "custom_components.eufy_sdk.entity.async_call_later",
                    return_value=Mock(),
                ):
                    await entity.write(True)
                owner = entity._optimistic_owner
                if raises:
                    coordinator.async_request_refresh.side_effect = RuntimeError(
                        "synthetic refresh failure"
                    )
                    with self.assertRaisesRegex(
                        RuntimeError, "synthetic refresh failure"
                    ):
                        await entity._reconcile(owner)
                else:
                    coordinator.last_update_success = False
                    await entity._reconcile(owner)
                self._report(entity, False)
                self.assertEqual(entity._optimistic_owner, owner)
                self.assertEqual(seen, [True])

    async def test_external_change_without_hold_is_immediate(self):
        entity, _coordinator, _client, seen = self._entity()
        self._report(entity, True)
        self._receipt(entity, False)
        self.assertEqual(seen, [True, False])

    async def test_setup_callback_and_native_event_bus_preserve_hold(self):
        for target in (False, True):
            with self.subTest(target=target):
                entity, coordinator, client, seen = self._entity(initial=not target)
                # The local HA object starts no server or bridge transport.
                hass = HomeAssistant(ROOT)
                hass.config_entries = Mock(async_forward_entry_setups=AsyncMock())
                entry = Mock(
                    data={CONF_HOST: "bridge.invalid", CONF_PORT: 3000},
                    options={},
                    domain="eufy_sdk",
                )
                client.set_poll_ms = AsyncMock()
                client.get_properties = AsyncMock(return_value=[])
                with (
                    patch.object(integration, "async_reconcile_property_entities"),
                    patch.object(
                        integration,
                        "EufySdkDataUpdateCoordinator",
                        return_value=coordinator,
                    ),
                    patch.object(
                        integration, "EufySdkApiClient", return_value=client
                    ) as factory,
                    patch.object(
                        integration, "async_get_clientsession", return_value=Mock()
                    ),
                    patch.object(
                        integration, "async_get_loaded_integration", return_value=Mock()
                    ),
                ):
                    await integration.async_setup_entry(hass, entry)
                on_event = factory.call_args.kwargs["on_event"]
                entity.hass = hass
                await entity.async_added_to_hass()
                cancel = Mock()
                try:
                    with patch(
                        "custom_components.eufy_sdk.entity.async_call_later",
                        return_value=cancel,
                    ):
                        await entity.write(target)
                    reports = [
                        {
                            "event": "propertyChanged",
                            "property": "light",
                            "value": target,
                        },
                        {
                            "event": "propertiesObserved",
                            "source": "rtc",
                            "observedAtMs": 100000,
                            "properties": {"light": not target},
                        },
                        {
                            "event": "propertyChanged",
                            "property": "light",
                            "value": target,
                        },
                    ]
                    with patch(
                        "custom_components.eufy_sdk.property_sync.time",
                        return_value=100,
                    ):
                        for report in reports:
                            on_event({"deviceSn": "fixture-device", **report})
                            await hass.async_block_till_done()
                            self.assertIs(entity.is_on, target)
                    self.assertEqual(seen, [target])
                    self.assertIs(
                        entity.extra_state_attributes["device_observed_value"],
                        not target,
                    )
                    self.assertIs(entity._post_write_unsub, cancel)
                    cancel.assert_not_called()
                finally:
                    await entity.async_will_remove_from_hass()
                    await hass.async_block_till_done()


if __name__ == "__main__":
    unittest.main()
