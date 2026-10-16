# ruff: noqa: ANN201, D100, D101, D102, INP001, PT009

import unittest
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import custom_components.eufy_sdk as integration
from custom_components.eufy_sdk.alarm_sync import apply_alarm_event, clear_alarm
from custom_components.eufy_sdk.arming_sync import apply_arming_mode_event
from custom_components.eufy_sdk.const import CONF_HOST, CONF_PORT, EVENT_TYPE


class EarlyEventHelperTests(unittest.TestCase):
    def test_alarm_and_arming_helpers_skip_unloaded_or_empty_data(self):
        for data in (None, {}):
            with self.subTest(data=data):
                coordinator = Mock(data=data)
                self.assertIsNone(
                    apply_alarm_event(
                        coordinator,
                        {"event": "alarm", "deviceSn": "homebase", "type": 4},
                    )
                )
                self.assertFalse(clear_alarm(coordinator, "homebase"))
                self.assertFalse(
                    apply_arming_mode_event(
                        coordinator,
                        {
                            "event": "armingModeChanged",
                            "deviceSn": "homebase",
                            "arming": 2,
                            "mode": 0,
                        },
                    )
                )
                coordinator.async_update_listeners.assert_not_called()
                coordinator.record_state_event.assert_not_called()
                self.assertIs(coordinator.data, data)


class SetupEventGuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hass = Mock()
        self.hass.config_entries.async_forward_entry_setups = AsyncMock()
        self.entry = Mock()
        self.entry.data = {CONF_HOST: "bridge", CONF_PORT: 3000}
        self.entry.options = {}
        self.entry.domain = "eufy_sdk"
        self.entry.entry_id = "test-entry"
        self.entry.add_update_listener = Mock(return_value=lambda: None)
        self.coordinator = Mock(data=None, property_observations=None)
        self.coordinator.async_request_refresh = AsyncMock()
        self.callbacks = []
        self.early_events = (
            {"event": "armingModeChanged", "deviceSn": "homebase"},
            {
                "event": "armingModeChanged",
                "deviceSn": "homebase",
                "arming": 2,
                "mode": 0,
            },
            {"event": "contactState", "deviceSn": "sensor", "open": True},
            {
                "event": "propertyChanged",
                "deviceSn": "camera",
                "property": "battery",
            },
            {
                "event": "propertyChanged",
                "deviceSn": "camera",
                "property": "battery",
                "value": 0,
            },
            {"event": "alarm", "deviceSn": "homebase", "type": 4},
            {
                "event": "propertiesObserved",
                "deviceSn": "camera",
                "source": "rtc",
                "observedAtMs": 100000,
                "properties": {"light": False},
            },
        )

        async def first_refresh():
            # Exercise the actual callback while setup has not loaded its cache.
            for event in self.early_events:
                self.callbacks[0](event)
            self.coordinator.data = {
                "homebase": {"state": {"armingMode": 1}},
                "camera": {"state": {"light": True}},
            }

        self.coordinator.async_config_entry_first_refresh = AsyncMock(
            side_effect=first_refresh
        )
        client = Mock()
        client.set_poll_ms = AsyncMock()
        client.get_properties = AsyncMock(return_value=[])

        def make_client(**kwargs: Any) -> Mock:
            self.callbacks.append(kwargs["on_event"])
            return client

        with (
            patch.object(integration, "async_reconcile_property_entities"),
            patch.object(
                integration, "EufySdkDataUpdateCoordinator", return_value=self.coordinator
            ),
            patch.object(integration, "EufySdkApiClient", side_effect=make_client),
            patch.object(integration, "async_get_clientsession", return_value=Mock()),
            patch.object(
                integration, "async_get_loaded_integration", return_value=Mock()
            ),
            patch.object(integration, "async_call_later") as timer,
        ):
            await integration.async_setup_entry(self.hass, self.entry)
        self.timer = timer
        self.on_event = self.callbacks[0]

    async def test_early_events_still_reach_bus_without_mutation_or_refresh(self):
        self.assertEqual(self.hass.bus.async_fire.call_count, len(self.early_events))
        for event in self.early_events:
            self.hass.bus.async_fire.assert_any_call(EVENT_TYPE, event)
        self.entry.async_create_background_task.assert_not_called()
        self.hass.async_create_task.assert_not_called()
        self.coordinator.async_request_refresh.assert_not_called()
        self.coordinator.async_update_listeners.assert_not_called()
        self.coordinator.record_state_event.assert_not_called()
        self.assertIsNone(self.coordinator.property_observations)
        self.timer.assert_not_called()

    async def test_valued_modes_after_refresh_keep_selected_and_reported_modes(self):
        self.on_event(
            {
                "event": "armingModeChanged",
                "deviceSn": "homebase",
                "arming": 2,
                "mode": 0,
            }
        )
        self.assertEqual(
            self.coordinator.data["homebase"]["state"],
            {"armingMode": 2, "currentMode": 0},
        )
        self.coordinator.record_state_event.assert_called_once_with(
            "homebase", {"armingMode": 2, "currentMode": 0}
        )
        self.coordinator.async_update_listeners.assert_called_once_with()
        self.entry.async_create_background_task.assert_not_called()

    async def test_observation_after_refresh_retains_receipt_and_state_update(self):
        with patch("custom_components.eufy_sdk.property_sync.time", return_value=100):
            self.on_event(self.early_events[-1])
        self.assertEqual(
            self.coordinator.property_observations,
            {"camera": {"light": {"value": False, "observed_at": 100}}},
        )
        self.assertFalse(self.coordinator.data["camera"]["state"]["light"])
        self.coordinator.record_state_event.assert_called_once_with(
            "camera", {"light": False}
        )
        self.coordinator.async_update_listeners.assert_called_once_with()
        self.entry.async_create_background_task.assert_not_called()


if __name__ == "__main__":
    unittest.main()
