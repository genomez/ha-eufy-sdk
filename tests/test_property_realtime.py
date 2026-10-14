# ruff: noqa: ANN201, D100, D101, D102, INP001, PT009, S101

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import Mock

_PATH = Path(__file__).parents[1] / "custom_components/eufy_sdk/property_sync.py"
_SPEC = importlib.util.spec_from_file_location("property_sync", _PATH)
_MODULE = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_MODULE)


class PropertyRealtimeTests(unittest.TestCase):
    def test_explicit_scalar_values_update_only_the_announced_property(self):
        for value in (62, 0, False, "", 1.5, None):
            with self.subTest(value=value):
                coordinator = Mock(
                    data={"camera": {"state": {"battery": 20, "motion": True}}}
                )
                self.assertTrue(
                    _MODULE.apply_property_event(
                        coordinator,
                        {
                            "event": "propertyChanged",
                            "deviceSn": "camera",
                            "property": "battery",
                            "value": value,
                        },
                    )
                )
                self.assertEqual(coordinator.data["camera"]["state"]["battery"], value)
                self.assertIs(
                    type(coordinator.data["camera"]["state"]["battery"]), type(value)
                )
                self.assertTrue(coordinator.data["camera"]["state"]["motion"])
                coordinator.async_update_listeners.assert_called_once_with()

    def test_newly_observed_property_does_not_require_a_previous_value(self):
        coordinator = Mock(data={"camera": {}})
        self.assertTrue(
            _MODULE.apply_property_event(
                coordinator,
                {
                    "event": "propertyChanged",
                    "sn": "camera",
                    "property": "battery",
                    "value": 0,
                },
            )
        )
        self.assertEqual(coordinator.data, {"camera": {"state": {"battery": 0}}})

    def test_missing_or_malformed_events_do_not_mutate_the_snapshot(self):
        event = {
            "event": "propertyChanged",
            "deviceSn": "camera",
            "property": "battery",
            "value": 62,
        }
        invalid = [
            {k: v for k, v in event.items() if k != "value"},
            {**event, "event": "other"},
            {**event, "deviceSn": "unknown"},
            {**event, "deviceSn": []},
            {**event, "property": []},
            {**event, "property": ""},
            *(
                {**event, "value": value}
                for value in ({}, [], float("nan"), float("inf"))
            ),
        ]
        for value in invalid:
            coordinator = Mock(data={"camera": {"state": {"battery": 20}}})
            self.assertFalse(_MODULE.apply_property_event(coordinator, value))
            self.assertEqual(coordinator.data, {"camera": {"state": {"battery": 20}}})
            coordinator.async_update_listeners.assert_not_called()
        coordinator = Mock(data=None)
        self.assertFalse(_MODULE.apply_property_event(coordinator, event))
        coordinator.async_update_listeners.assert_not_called()


if __name__ == "__main__":
    unittest.main()
