# ruff: noqa: ANN201, ANN001, D101, D102, INP001, PT009
"""Device receipts remain independent of optimistic and cached state."""

import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location(
    "receipts",
    Path(__file__).parents[1] / "custom_components/eufy_sdk/property_sync.py",
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Receipts(unittest.TestCase):
    def setUp(self):
        self.c = SimpleNamespace(
            data={"synthetic": {"state": {"light": False}}},
            property_observations=None,
            record_state_event=Mock(),
            async_update_listeners=Mock(),
        )
        self.event = {
            "event": "propertiesObserved",
            "deviceSn": "synthetic",
            "source": "rtc",
            "observedAtMs": 100000,
            "properties": {"light": False},
        }

    @patch.object(module, "time", return_value=100)
    def test_same_value_receipt_and_snapshot_replacement(self, clock):
        self.assertEqual(clock.return_value, 100)
        self.assertTrue(module.apply_observation_event(self.c, self.event))
        self.assertEqual(
            self.c.property_observations["synthetic"]["light"],
            {"value": False, "observed_at": 100},
        )
        self.c.data = {"synthetic": {"state": {"light": False}}}
        self.assertEqual(
            self.c.property_observations["synthetic"]["light"]["observed_at"], 100
        )
        self.assertFalse(module.apply_observation_event(self.c, self.event))

    @patch.object(module, "time", return_value=100)
    def test_wrong_source_stale_future_unrelated_and_malformed(self, clock):
        self.assertEqual(clock.return_value, 100)
        for replacement in (
            {"source": "cloud"},
            {"observedAtMs": 39000},
            {"observedAtMs": 101000},
            {"deviceSn": "other"},
            {"properties": {"unknown": False}},
            {"properties": {"light": "false", "bad": {}}},
        ):
            event = {**self.event, **replacement}
            self.assertFalse(module.apply_observation_event(self.c, event))
        self.assertEqual(self.c.data["synthetic"]["state"]["light"], False)

    @patch.object(module, "time", return_value=100)
    def test_property_change_does_not_create_receipt(self, clock):
        self.assertEqual(clock.return_value, 100)
        module.apply_property_event(
            self.c,
            {
                "event": "propertyChanged",
                "deviceSn": "synthetic",
                "property": "light",
                "value": True,
            },
        )
        self.assertIsNone(self.c.property_observations)


if __name__ == "__main__":
    unittest.main()
