"""Generic enum sensors preserve unrecognized scalar codes."""

# ruff: noqa: ANN201, D101, D102, INP001, PT009

import unittest
from unittest.mock import Mock

from custom_components.eufy_sdk.sensor import EufySdkPropertySensor


def sensor_for(value: object, *, raw: bool = False) -> EufySdkPropertySensor:
    """Build a property sensor from a synthetic device snapshot."""
    coordinator = Mock(data={"synthetic": {"state": {"powerSource": value}}})
    return EufySdkPropertySensor(
        coordinator,
        "synthetic",
        {
            "name": "powerSource",
            "type": "enum",
            "enumValues": {"0": "Battery", "1": "Mains"},
            "raw": raw,
        },
    )


class SensorEnumValueTests(unittest.TestCase):
    def test_labeled_values_are_unchanged(self):
        for value, expected in ((0, "Battery"), (1, "Mains"), ("1", "Mains")):
            with self.subTest(value=value):
                self.assertEqual(sensor_for(value).native_value, expected)

    def test_unlabeled_integer_code_is_preserved(self):
        sensor = sensor_for(2)
        self.assertEqual(sensor.native_value, 2)
        self.assertIs(type(sensor.native_value), int)
        self.assertIsNone(sensor.device_class)
        self.assertIsNone(sensor.options)

    def test_unlabeled_string_code_is_preserved(self):
        sensor = sensor_for("future")
        self.assertEqual(sensor.native_value, "future")
        self.assertIsNone(sensor.device_class)
        self.assertIsNone(sensor.options)

    def test_invalid_types_remain_unknown(self):
        for value in (None, True, 2.0, [], {"code": 2}):
            with self.subTest(value=value):
                self.assertIsNone(sensor_for(value).native_value)

    def test_raw_specs_remain_unknown(self):
        for value in (0, 2, "future"):
            with self.subTest(value=value):
                self.assertIsNone(sensor_for(value, raw=True).native_value)
