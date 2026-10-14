# ruff: noqa: ANN201, D100, D101, D102, INP001, PT009, SLF001
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from custom_components.eufy_sdk.sensor import EufySdkLastPersonSensor


# This display name is constructed for offline routing tests only.
SYNTHETIC_PERSON_NAME = "SYNTHETIC_PERSON_ALPHA"


class PersonNameRoutingTests(unittest.TestCase):
    def entity(self):
        entity = object.__new__(EufySdkLastPersonSensor)
        entity._sn = "T8000P0000000001"
        entity.async_write_ha_state = Mock()
        return entity

    def event(self, **fields: object):
        return SimpleNamespace(
            data={"deviceSn": "T8000P0000000001", "event": "personDetected", **fields}
        )

    def test_name_unknown_and_stranger_remain_distinct(self):
        entity = self.entity()
        entity._handle_event(
            self.event(person_name=SYNTHETIC_PERSON_NAME, recognized=True)
        )
        self.assertEqual(entity._attr_native_value, SYNTHETIC_PERSON_NAME)
        self.assertTrue(entity._attr_extra_state_attributes["recognized"])
        entity._handle_event(self.event(person_id=7, recognized=False))
        self.assertEqual(entity._attr_native_value, "Unknown")
        self.assertFalse(entity._attr_extra_state_attributes["recognized"])
        entity._handle_event(
            self.event(
                event="strangerDetected",
                person_name=SYNTHETIC_PERSON_NAME,
                recognized=True,
            )
        )
        self.assertEqual(entity._attr_native_value, "Stranger")
        self.assertFalse(entity._attr_extra_state_attributes["recognized"])

    def test_other_device_and_unrelated_event_do_not_change_state(self):
        entity = self.entity()
        entity._handle_event(
            self.event(
                deviceSn="T8000P0000000002",
                person_name=SYNTHETIC_PERSON_NAME,
                recognized=True,
            )
        )
        entity._handle_event(
            self.event(event="motion", person_name=SYNTHETIC_PERSON_NAME, recognized=True)
        )
        entity.async_write_ha_state.assert_not_called()

    def test_a_name_alone_or_a_flag_alone_is_not_a_verified_name(self):
        for fields in ({"person_name": SYNTHETIC_PERSON_NAME}, {"recognized": True}):
            entity = self.entity()
            entity._handle_event(self.event(**fields))
            self.assertEqual(entity._attr_native_value, "Unknown")

    def test_delayed_name_can_follow_an_unnamed_detection(self):
        entity = self.entity()
        entity._handle_event(self.event(recognized=False))
        self.assertEqual(entity._attr_native_value, "Unknown")
        entity._handle_event(
            self.event(person_name=SYNTHETIC_PERSON_NAME, person_id=7, recognized=True)
        )
        self.assertEqual(entity._attr_native_value, SYNTHETIC_PERSON_NAME)
        self.assertTrue(entity._attr_extra_state_attributes["recognized"])

    def test_repeated_name_does_not_name_a_later_unknown_event(self):
        entity = self.entity()
        for _ in range(3):
            entity._handle_event(
                self.event(person_name=SYNTHETIC_PERSON_NAME, recognized=True)
            )
        entity._handle_event(self.event(recognized=False))
        self.assertEqual(entity._attr_native_value, "Unknown")
        self.assertFalse(entity._attr_extra_state_attributes["recognized"])
