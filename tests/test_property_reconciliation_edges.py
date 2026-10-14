"""Bounded regression cases for property reconciliation and lifecycle ownership.

These tests use explicit listener/timer mocks; native HA event dispatch is a
separate integration boundary and is not established by these fixtures.
"""

# ruff: noqa: ANN201, ANN202, ANN001, D102, INP001, PT009, SLF001

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from custom_components.eufy_sdk.entity import EufySdkPropertyEntity
from custom_components.eufy_sdk.property_sync import (
    apply_observation_event,
    apply_property_event,
)


class ReconciliationEdgeTests(unittest.IsolatedAsyncioTestCase):
    """Exercise actual entity methods using generic framework collaborators."""

    def _entity(self, *, write_only=False):
        client = SimpleNamespace(set_property=AsyncMock())
        coordinator = SimpleNamespace(
            data={"fixture-device": {"state": {"enabled": False}}},
            config_entry=SimpleNamespace(runtime_data=SimpleNamespace(client=client)),
            async_request_refresh=AsyncMock(),
            last_update_success=True,
            snapshot_generation=0,
            completed_snapshot_generation=0,
            property_observations=None,
            record_state_event=Mock(),
            async_update_listeners=Mock(),
            async_add_listener=Mock(return_value=Mock()),
        )
        entity = EufySdkPropertyEntity(
            coordinator,
            "fixture-device",
            {
                "name": "enabled",
                "type": "bool",
                "writable": True,
                "writeOnly": write_only,
            },
        )
        entity.hass = Mock()
        entity.async_write_ha_state = Mock()
        entity.async_on_remove = Mock()
        return entity, coordinator, client

    @staticmethod
    def _event(**overrides):
        return {
            "event": "propertyChanged",
            "deviceSn": "fixture-device",
            "property": "enabled",
            "value": False,
            **overrides,
        }

    async def test_invalid_reports_cannot_release_successful_optimistic_hold(self):
        entity, coordinator, _client = self._entity()
        cancel = Mock()
        with patch(
            "custom_components.eufy_sdk.entity.async_call_later", return_value=cancel
        ):
            await entity.write(True)
        owner = entity._optimistic_owner
        report_generation = entity._report_generation
        invalid = [
            self._event(value={}),
            self._event(value=[]),
            self._event(value=float("nan")),
            self._event(value=float("inf")),
            self._event(value=float("-inf")),
            self._event(value=object()),
            self._event(deviceSn="other-device"),
            self._event(deviceSn=[]),
            self._event(property="other-property"),
            self._event(property=[]),
            self._event(event="other-event"),
            {key: value for key, value in self._event().items() if key != "value"},
        ]
        for report in invalid:
            with self.subTest(report=repr(report)):
                self.assertFalse(entity._property_event_filter(report))
                # Also call the handler directly: it must defend independently.
                entity._handle_property_changed(SimpleNamespace(data=report))
                self.assertTrue(entity.prop_value)
                self.assertEqual(entity._optimistic_owner, owner)
                self.assertEqual(entity._report_generation, report_generation)
        self.assertFalse(coordinator.data["fixture-device"]["state"]["enabled"])
        cancel.assert_not_called()

    async def test_valid_scalar_reports_release_hold_after_cache_application(self):
        for value in (False, 0, "", 1.5, None):
            with self.subTest(value=value):
                entity, coordinator, _client = self._entity()
                cancel = Mock()
                with patch(
                    "custom_components.eufy_sdk.entity.async_call_later",
                    return_value=cancel,
                ):
                    await entity.write(True)
                report = self._event(value=value)
                self.assertTrue(apply_property_event(coordinator, report))
                self.assertTrue(entity._property_event_filter(report))
                entity._handle_property_changed(SimpleNamespace(data=report))
                self.assertIsNone(entity._optimistic_owner)
                self.assertIsNone(entity._assumed_value)
                self.assertEqual(entity.prop_value, value)
                self.assertIs(type(entity.prop_value), type(value))
                cancel.assert_called_once()

    async def test_older_reconcile_finishing_after_new_write_preserves_new_hold(self):
        entity, coordinator, _client = self._entity()
        gate = asyncio.Event()

        async def refresh():
            await gate.wait()
            coordinator.snapshot_generation += 1
            coordinator.completed_snapshot_generation = coordinator.snapshot_generation

        coordinator.async_request_refresh.side_effect = refresh
        with patch(
            "custom_components.eufy_sdk.entity.async_call_later", return_value=Mock()
        ):
            await entity.write(True)
            old_owner = entity._optimistic_owner
            reconcile = asyncio.create_task(entity._reconcile(old_owner))
            await asyncio.sleep(0)
            await entity.write(False)
            new_owner = entity._optimistic_owner
            gate.set()
            await reconcile
        self.assertNotEqual(old_owner, new_owner)
        self.assertEqual(entity._optimistic_owner, new_owner)
        self.assertIs(entity._assumed_value, False)

    async def test_raised_refresh_failure_preserves_optimistic_hold(self):
        entity, coordinator, _client = self._entity()
        with patch(
            "custom_components.eufy_sdk.entity.async_call_later", return_value=Mock()
        ):
            await entity.write(True)
        owner = entity._optimistic_owner
        coordinator.async_request_refresh.side_effect = RuntimeError(
            "fixture-refresh-failure"
        )
        with self.assertRaisesRegex(RuntimeError, "fixture-refresh-failure"):
            await entity._reconcile(owner)
        self.assertEqual(entity._optimistic_owner, owner)
        self.assertTrue(entity.prop_value)

    async def test_coordinator_reported_refresh_failure_preserves_optimistic_hold(self):
        entity, coordinator, _client = self._entity()
        with patch(
            "custom_components.eufy_sdk.entity.async_call_later", return_value=Mock()
        ):
            await entity.write(True)
        owner = entity._optimistic_owner
        coordinator.last_update_success = False
        await entity._reconcile(owner)
        self.assertEqual(entity._optimistic_owner, owner)
        self.assertTrue(entity.prop_value)

    async def test_pending_rpc_cannot_reacquire_hold_after_remove_and_readd(self):
        entity, _coordinator, client = self._entity()
        gate = asyncio.Event()

        async def pending(*_args):
            await gate.wait()

        client.set_property.side_effect = pending
        with patch(
            "custom_components.eufy_sdk.entity.async_call_later", return_value=Mock()
        ) as timer:
            task = asyncio.create_task(entity.write(True))
            await asyncio.sleep(0)
            await entity.async_will_remove_from_hass()
            await entity.async_added_to_hass()
            gate.set()
            await task
            self.assertIsNone(entity._optimistic_owner)
            self.assertIsNone(entity._assumed_value)
            timer.assert_not_called()
            client.set_property.side_effect = None
            await entity.write(True)
            self.assertTrue(entity.prop_value)
            timer.assert_called_once()

    async def test_listener_cleanup_and_stale_callback_after_readd(self):
        entity, coordinator, _client = self._entity()
        first_cancel, second_cancel = Mock(), Mock()
        entity.hass.bus.async_listen.side_effect = [first_cancel, second_cancel]
        await entity.async_added_to_hass()
        first_listener = entity.hass.bus.async_listen.call_args.args[1]
        first_filter = entity.hass.bus.async_listen.call_args.kwargs["event_filter"]
        self.assertTrue(first_filter(self._event()))
        removal = entity.async_on_remove.call_args.args[0]
        await entity.async_will_remove_from_hass()
        removal()  # HA's cleanup may run as well as the explicit removal method.
        first_cancel.assert_called_once()
        entity.async_write_ha_state.reset_mock()
        entity._handle_coordinator_update()
        entity.async_write_ha_state.assert_not_called()
        await entity.async_added_to_hass()
        second_listener = entity.hass.bus.async_listen.call_args.args[1]
        with patch(
            "custom_components.eufy_sdk.entity.async_call_later", return_value=Mock()
        ):
            await entity.write(True)
        first_listener(SimpleNamespace(data=self._event()))
        self.assertTrue(entity.prop_value)
        apply_property_event(coordinator, self._event())
        second_listener(SimpleNamespace(data=self._event()))
        self.assertFalse(entity.prop_value)
        await entity.async_will_remove_from_hass()
        second_cancel.assert_called_once()

    async def test_observation_retains_receipt_without_releasing_intended_hold(self):
        entity, coordinator, _client = self._entity()
        cancel = Mock()
        with patch(
            "custom_components.eufy_sdk.entity.async_call_later", return_value=cancel
        ):
            await entity.write(True)
        owner = entity._optimistic_owner
        report = {
            "event": "propertiesObserved",
            "deviceSn": "fixture-device",
            "source": "rtc",
            "observedAtMs": 100000,
            "properties": {"enabled": False},
        }
        with patch("custom_components.eufy_sdk.property_sync.time", return_value=100):
            self.assertTrue(apply_observation_event(coordinator, report))
        self.assertFalse(entity._property_event_filter(report))
        entity._handle_property_changed(SimpleNamespace(data=report))
        self.assertEqual(
            coordinator.property_observations["fixture-device"]["enabled"],
            {"value": False, "observed_at": 100},
        )
        self.assertFalse(coordinator.data["fixture-device"]["state"]["enabled"])
        self.assertEqual(entity._optimistic_owner, owner)
        self.assertTrue(entity.prop_value)
        cancel.assert_not_called()

    async def test_write_only_success_has_no_refresh_timer(self):
        entity, _coordinator, _client = self._entity(write_only=True)
        with patch("custom_components.eufy_sdk.entity.async_call_later") as timer:
            await entity.write(True)
            self.assertTrue(entity.prop_value)
            self.assertIsNotNone(entity._optimistic_owner)
            timer.assert_not_called()

    async def test_queued_refresh_return_does_not_release_until_new_snapshot(self):
        entity, coordinator, _client = self._entity()
        with patch(
            "custom_components.eufy_sdk.entity.async_call_later", return_value=Mock()
        ):
            await entity.write(True)
        owner = entity._optimistic_owner
        # A debounced HA request can return before the actual read begins.
        await entity._reconcile(owner)
        self.assertTrue(entity.prop_value)
        self.assertEqual(entity._optimistic_owner, owner)
        # Cache/listener notifications alone must not imply a completed read.
        entity._handle_coordinator_update()
        self.assertEqual(entity._optimistic_owner, owner)
        coordinator.snapshot_generation = 1
        coordinator.completed_snapshot_generation = 1
        entity._handle_coordinator_update()
        self.assertIsNone(entity._optimistic_owner)
        self.assertFalse(entity.prop_value)

    async def test_snapshot_started_before_reconcile_cannot_release_hold(self):
        entity, coordinator, _client = self._entity()
        coordinator.snapshot_generation = 5
        coordinator.completed_snapshot_generation = 4
        with patch(
            "custom_components.eufy_sdk.entity.async_call_later", return_value=Mock()
        ):
            await entity.write(True)
        owner = entity._optimistic_owner
        await entity._reconcile(owner)
        coordinator.completed_snapshot_generation = 5
        entity._handle_coordinator_update()
        self.assertEqual(entity._optimistic_owner, owner)
        self.assertTrue(entity.prop_value)
        coordinator.snapshot_generation = 6
        coordinator.completed_snapshot_generation = 6
        entity._handle_coordinator_update()
        self.assertIsNone(entity._optimistic_owner)
        self.assertFalse(entity.prop_value)

    async def test_queued_old_reconcile_cannot_release_new_write_on_notification(self):
        entity, coordinator, _client = self._entity()
        with patch(
            "custom_components.eufy_sdk.entity.async_call_later", return_value=Mock()
        ):
            await entity.write(True)
            await entity._reconcile(entity._optimistic_owner)
            await entity.write(False)
        owner = entity._optimistic_owner
        coordinator.snapshot_generation = 1
        coordinator.completed_snapshot_generation = 1
        entity._handle_coordinator_update()
        self.assertEqual(entity._optimistic_owner, owner)
        self.assertIs(entity._assumed_value, False)

    async def test_deferred_failed_completion_and_receipt_cannot_release_hold(self):
        entity, coordinator, _client = self._entity()
        with patch(
            "custom_components.eufy_sdk.entity.async_call_later", return_value=Mock()
        ):
            await entity.write(True)
        owner = entity._optimistic_owner
        await entity._reconcile(owner)
        coordinator.snapshot_generation = 1
        coordinator.last_update_success = False
        entity._handle_coordinator_update()
        self.assertEqual(entity._optimistic_owner, owner)
        coordinator.last_update_success = True
        report = {
            "event": "propertiesObserved",
            "deviceSn": "fixture-device",
            "source": "rtc",
            "observedAtMs": 100000,
            "properties": {"enabled": False},
        }
        with patch("custom_components.eufy_sdk.property_sync.time", return_value=100):
            self.assertTrue(apply_observation_event(coordinator, report))
        entity._handle_coordinator_update()
        self.assertEqual(entity._optimistic_owner, owner)
        self.assertTrue(entity.prop_value)


if __name__ == "__main__":
    unittest.main()
