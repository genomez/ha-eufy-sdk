# ruff: noqa: ANN201, D100, D101, D102, INP001, PT009

import unittest
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import custom_components.eufy_sdk as integration
from custom_components.eufy_sdk.const import (
    CONF_HOST,
    CONF_PORT,
    MAX_DOORBELL_EVENT_IDS,
)
from custom_components.eufy_sdk.event import EufySdkDoorbellEvent
from custom_components.eufy_sdk.switch import EufySdkSwitch


class SetupEventTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        hass = Mock()
        hass.bus.async_fire = Mock()
        hass.config_entries.async_forward_entry_setups = AsyncMock()

        entry = Mock()
        entry.data = {CONF_HOST: "bridge", CONF_PORT: 3000}
        entry.options = {}
        entry.domain = "eufy_sdk"
        entry.entry_id = "test-entry"
        entry.add_update_listener = Mock(return_value=lambda: None)
        entry.async_on_unload = Mock()
        scheduled = []
        entry.async_create_background_task = Mock(
            side_effect=lambda _hass, coroutine, _name: scheduled.append(coroutine)
        )

        coordinator = Mock(data={"homebase": {"state": {"armingMode": 1}}})
        coordinator.async_config_entry_first_refresh = AsyncMock()
        coordinator.async_request_refresh = AsyncMock()

        client = Mock()
        client.set_poll_ms = AsyncMock()
        client.get_properties = AsyncMock(return_value=[])
        callbacks = []

        def make_client(**kwargs: Any) -> Mock:
            callbacks.append(kwargs["on_event"])
            return client

        with (
            patch.object(integration, "async_reconcile_property_entities"),
            patch.object(
                integration, "EufySdkDataUpdateCoordinator", return_value=coordinator
            ),
            patch.object(integration, "EufySdkApiClient", side_effect=make_client),
            patch.object(integration, "async_get_clientsession", return_value=Mock()),
            patch.object(
                integration, "async_get_loaded_integration", return_value=Mock()
            ),
        ):
            await integration.async_setup_entry(hass, entry)
            self.assertEqual(len(callbacks), 1)

        self.on_event = callbacks[0]
        self.bus = hass.bus
        self.coordinator = coordinator
        self.scheduled = scheduled

    async def test_doorbell_duplicate_identity_reaches_all_ha_consumers_once(self):
        for legacy_pressed in (False, True):
            with self.subTest(legacy_pressed=legacy_pressed):
                entity = EufySdkDoorbellEvent(
                    self.coordinator,
                    "synthetic-doorbell",
                    legacy_pressed=legacy_pressed,
                )
                writes = []
                entity.async_write_ha_state = Mock(
                    side_effect=lambda writes=writes, entity=entity: writes.append(
                        dict(entity.state_attributes)
                    )
                )
                self.bus.async_fire.side_effect = lambda _type, data, entity=entity: (
                    entity._handle_event(Mock(data=data))  # noqa: SLF001
                )
                for identity in ("synthetic-a", "synthetic-b", "synthetic-a"):
                    self.on_event(
                        {
                            "event": "doorbellPress",
                            "deviceSn": "synthetic-doorbell",
                            "eventId": f"{legacy_pressed}-{identity}",
                        }
                    )
                expected = "pressed" if legacy_pressed else "ring"
                self.assertEqual(writes, [{"event_type": expected}] * 2)
        self.assertEqual(self.bus.async_fire.call_count, 4)

    async def test_doorbell_identity_is_device_scoped_and_only_filters_presses(self):
        for serial, event in (
            ("synthetic-a", "doorbellPress"),
            ("synthetic-b", "doorbellPress"),
            ("synthetic-a", "motion"),
            ("synthetic-a", "motion"),
        ):
            self.on_event(
                {"event": event, "deviceSn": serial, "eventId": "synthetic-id"}
            )
        self.assertEqual(self.bus.async_fire.call_count, 4)

    async def test_doorbell_without_valid_identity_keeps_real_repeats(self):
        for identity in (None, "", " ", 42):
            for _ in range(2):
                self.on_event(
                    {
                        "event": "doorbellPress",
                        "deviceSn": "synthetic-doorbell",
                        "eventId": identity,
                    }
                )
        self.assertEqual(self.bus.async_fire.call_count, 8)

    async def test_doorbell_identity_cache_is_bounded_without_timer(self):
        for index in range(MAX_DOORBELL_EVENT_IDS + 1):
            self.on_event(
                {
                    "event": "doorbellPress",
                    "deviceSn": "synthetic-doorbell",
                    "eventId": str(index),
                }
            )
        self.on_event(
            {"event": "doorbellPress", "deviceSn": "synthetic-doorbell", "eventId": "0"}
        )
        self.assertEqual(self.bus.async_fire.call_count, MAX_DOORBELL_EVENT_IDS + 2)
        self.assertEqual(self.scheduled, [])

    async def test_observation_callback_reaches_switch_attributes(self):

        self.coordinator.data = {"synthetic": {"state": {"light": False}}}
        self.coordinator.property_observations = None
        entity = EufySdkSwitch(self.coordinator, "synthetic", {"name": "light"})
        with patch("custom_components.eufy_sdk.property_sync.time", return_value=100):
            self.on_event(
                {
                    "event": "propertiesObserved",
                    "deviceSn": "synthetic",
                    "source": "rtc",
                    "observedAtMs": 100000,
                    "properties": {"light": False},
                }
            )
        self.assertEqual(
            entity.extra_state_attributes,
            {
                "device_observed_at": 100,
                "device_observed_value": False,
                "device_observation_source": "rtc",
            },
        )
        self.assertFalse(entity.is_on)
        entity._assumed_value = True  # noqa: SLF001
        self.assertTrue(entity.is_on)
        self.assertFalse(entity.extra_state_attributes["device_observed_value"])
        self.assertEqual(self.scheduled, [])

    async def test_valueless_arming_event_refreshes_known_device_only(self):
        callbacks = [self.on_event]
        scheduled = self.scheduled
        coordinator = self.coordinator
        callbacks[0]({"event": "armingModeChanged", "deviceSn": "homebase"})
        self.assertEqual(len(scheduled), 1)
        await scheduled.pop()
        coordinator.async_request_refresh.assert_awaited_once_with()

        callbacks[0]({"event": "armingModeChanged", "deviceSn": "unknown"})
        self.assertEqual(scheduled, [])

    async def test_valued_property_event_updates_cache_without_refresh(self):
        self.on_event(
            {
                "event": "propertyChanged",
                "deviceSn": "homebase",
                "property": "battery",
                "value": 0,
            }
        )
        self.assertEqual(self.coordinator.data["homebase"]["state"]["battery"], 0)
        self.coordinator.async_update_listeners.assert_called_once_with()
        self.assertEqual(self.scheduled, [])
        self.coordinator.async_request_refresh.assert_not_awaited()

    async def test_valueless_property_event_requests_debounced_refresh(self):
        self.on_event(
            {
                "event": "propertyChanged",
                "deviceSn": "homebase",
                "property": "battery",
            }
        )
        self.assertEqual(len(self.scheduled), 1)
        await self.scheduled.pop()
        self.coordinator.async_request_refresh.assert_awaited_once_with()
        self.coordinator.async_update_listeners.assert_not_called()
        self.assertNotIn("battery", self.coordinator.data["homebase"]["state"])
        for changed in ({"deviceSn": "unknown"}, {"property": ""}, {"property": []}):
            self.on_event(
                {
                    "event": "propertyChanged",
                    "deviceSn": "homebase",
                    "property": "battery",
                    **changed,
                }
            )
        self.assertEqual(self.scheduled, [])


if __name__ == "__main__":
    unittest.main()
