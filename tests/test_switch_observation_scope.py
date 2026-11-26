"""Keep floodlight receipts off unrelated boolean switches."""

# ruff: noqa: ANN201, D102, FBT003, INP001, PT009, SLF001

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from homeassistant.core import CoreState
from homeassistant.helpers.entity import Entity

from custom_components.eufy_sdk.api import CommandReceipt
from custom_components.eufy_sdk.property_sync import apply_observation_event
from custom_components.eufy_sdk.switch import EufySdkSwitch, EufySdkWriteOnlySwitch


class SwitchObservationScopeTests(unittest.TestCase):
    """Exercise real switch listener and write paths with local observations."""

    def setUp(self):
        self.coordinator = SimpleNamespace(
            data={"camera": {"state": {"light": False, "speaker": False}}},
            property_observations=None,
            last_update_success=True,
            completed_snapshot_generation=0,
            record_state_event=Mock(),
        )
        self.entities = []
        self.writes = []
        writer = patch.object(
            Entity,
            "async_write_ha_state",
            autospec=True,
            side_effect=lambda entity: self.writes.append(
                (
                    entity._prop,
                    entity.available,
                    entity.is_on,
                    entity.extra_state_attributes,
                )
            ),
        )
        writer.start()
        self.addCleanup(writer.stop)
        self.coordinator.async_update_listeners = lambda: [
            entity._handle_coordinator_update() for entity in self.entities
        ]

    def entity(self, prop: str):
        self.coordinator.data["camera"]["state"].setdefault(prop, False)
        entity = EufySdkSwitch(
            self.coordinator, "camera", {"name": prop, "type": "bool", "writable": True}
        )
        entity.hass = Mock(data={}, state=CoreState.running)
        self.entities.append(entity)
        return entity

    def observe(self, at: int, **properties: bool):
        with patch("custom_components.eufy_sdk.property_sync.time", return_value=at):
            self.assertTrue(
                apply_observation_event(
                    self.coordinator,
                    {
                        "event": "propertiesObserved",
                        "deviceSn": "camera",
                        "source": "rtc",
                        "observedAtMs": at * 1000,
                        "properties": properties,
                    },
                )
            )

    def test_light_keeps_all_receipt_attributes_for_both_boolean_values(self):
        light = self.entity("light")
        for at, value in ((100, False), (123, True)):
            self.observe(at, light=value)
            self.assertEqual(
                light.extra_state_attributes,
                {
                    "device_observed_at": at,
                    "device_observed_value": value,
                    "device_observation_source": "rtc",
                },
            )
        self.assertEqual(len(self.writes), 2)

    def test_receipts_are_live_but_not_recorded(self):
        light = self.entity("light")
        light._command_profile = "fixture-profile"
        self.observe(100, light=True)
        receipts = {
            "device_observed_at",
            "device_observed_value",
            "device_observation_source",
        }
        live = set(light.extra_state_attributes)
        self.assertTrue(receipts <= live)
        unrecorded = light._Entity__combined_unrecorded_attributes
        # Exactly the receipts: any extra key would silently drop history.
        self.assertEqual(unrecorded, receipts)
        self.assertEqual(
            EufySdkWriteOnlySwitch._Entity__combined_unrecorded_attributes, receipts
        )
        self.assertEqual(live & unrecorded, receipts)
        for key in (
            "command_profile",
            "command_status",
            "command_effect",
            "command_send_attempted",
            "floodlight_safety_status",
            "off_stuck_incident",
        ):
            self.assertNotIn(key, unrecorded)

    def test_non_light_switches_omit_receipts_and_keep_command_attributes(self):
        expected = {
            "command_profile": "fixture-profile",
            "command_status": "rejected",
            "command_effect": "unconfirmed",
            "command_send_attempted": True,
            "command_rejection_code": 7,
            "command_response_latency_ms": 12.5,
            "command_superseded": True,
        }
        for prop in ("speaker", "statusLed", "motionDetection", "lightPower"):
            with self.subTest(prop=prop):
                entity = self.entity(prop)
                entity._command_profile = "fixture-profile"
                entity._command_receipt = CommandReceipt(
                    "rejected",
                    True,
                    "unconfirmed",
                    superseded=True,
                    rejection_code=7,
                    response_latency_ms=12.5,
                )
                self.observe(100, **{prop: False})
                self.assertEqual(entity.extra_state_attributes, expected)
