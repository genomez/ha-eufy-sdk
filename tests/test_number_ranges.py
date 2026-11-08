"""Numeric manifests are device domains; older bridges retain the same fallbacks."""

# ruff: noqa: ANN003, ANN201, D101, D102, INP001, PT009, PT027

import unittest
from unittest.mock import AsyncMock, Mock

from homeassistant.components.number import NumberMode
from homeassistant.exceptions import HomeAssistantError

from custom_components.eufy_sdk.manifest_reads import normalize_properties
from custom_components.eufy_sdk.number import EufySdkNumber


def number(**metadata):
    """Construct a number without device, account, transport, or runtime access."""
    coordinator = Mock(data={"synthetic": {"model": "T8170", "codec": "camera"}})
    coordinator.config_entry.entry_id = "synthetic-entry"
    entity = EufySdkNumber(
        coordinator,
        "synthetic",
        {
            "name": "recordDuration",
            "type": "number",
            "writable": True,
            "kind": "seconds",
            **metadata,
        },
    )
    entity.write = AsyncMock()
    return entity


class NumberRangeTests(unittest.TestCase):
    def test_properties_select_their_own_domain(self):
        for name, kind, low, high in (
            ("brightness", "percent", 1, 100),
            ("speakerVolume", "percent", 0, 100),
            ("recordDuration", "seconds", 10, 120),
            ("rotationSpeed", "scalar", 1, 5),
            ("alarmVolume", "scalar", 1, 26),
        ):
            with self.subTest(name=name):
                entity = number(name=name, kind=kind, min=low, max=high, step=1)
                self.assertEqual(
                    (
                        entity.native_min_value,
                        entity.native_max_value,
                        entity.native_step,
                    ),
                    (low, high, 1),
                )
                self.assertEqual(
                    entity.mode,
                    NumberMode.SLIDER if kind == "percent" else NumberMode.BOX,
                )

    def test_model_resolved_metadata_wins_over_device_name_or_generic_kind(self):
        for low, high in ((5, 120), (10, 120), (2, 60), (5, 60)):
            entity = number(min=low, max=high, step=1)
            self.assertEqual(
                (entity.native_min_value, entity.native_max_value), (low, high)
            )

    def test_older_bridge_fallbacks_are_unchanged(self):
        for kind, expected in (
            ("percent", (0, 100)),
            ("seconds", (0, 86400)),
            ("degrees", (0, 360)),
            ("scalar", (0, 65535)),
            (None, (0, 65535)),
        ):
            with self.subTest(kind=kind):
                entity = number(kind=kind)
                self.assertEqual(
                    (entity.native_min_value, entity.native_max_value), expected
                )
                self.assertEqual(entity.native_step, 1)

    def test_partial_metadata_keeps_the_other_fallback_bound(self):
        entity = number(min=5)
        self.assertEqual(
            (entity.native_min_value, entity.native_max_value, entity.native_step),
            (5, 86400, 1),
        )
        entity = number(max=120)
        self.assertEqual((entity.native_min_value, entity.native_max_value), (0, 120))

    def test_bad_metadata_is_not_treated_as_a_valid_range(self):
        for metadata in (
            {"min": True},
            {"max": float("nan")},
            {"max": float("inf")},
            {"min": "5"},
            {"min": 10**1000},
            {"min": 121, "max": 120},
            {"step": 0},
            {"step": -1},
            {"step": True},
            {"step": None},
            {"step": float("nan")},
        ):
            with self.subTest(metadata=metadata):
                entity = number(**metadata)
                self.assertEqual(
                    (
                        entity.native_min_value,
                        entity.native_max_value,
                        entity.native_step,
                    ),
                    (0, 86400, 1),
                )

    def test_invalid_step_preserves_valid_bounds(self):
        entity = number(min=10, max=120, step=0)
        self.assertEqual(
            (entity.native_min_value, entity.native_max_value, entity.native_step),
            (10, 120, 1),
        )

    def test_single_value_domain_does_not_expand_to_fallback(self):
        entity = number(min=5, max=5, step=1)
        self.assertEqual((entity.native_min_value, entity.native_max_value), (5, 5))

    def test_fractional_step_survives_manifest_normalization(self):
        spec = {
            "name": "fractional",
            "type": "number",
            "writable": True,
            "kind": "scalar",
            "min": 0.25,
            "max": 2,
            "step": 0.25,
        }
        reply = {
            "properties": [spec],
            "decodedProperties": {
                "details": [
                    {
                        "accessor": "synthetic",
                        "reads": [
                            {
                                "accessor": "fractional",
                                "property": "fractional",
                                "type": "number",
                                "kind": "scalar",
                                "writable": True,
                            }
                        ],
                    }
                ]
            },
        }
        normalized = normalize_properties(reply)[0]
        entity = number(**normalized)
        self.assertEqual(
            (entity.native_min_value, entity.native_max_value, entity.native_step),
            (0.25, 2, 0.25),
        )

    def test_fallback_diagnostic_names_only_the_property(self):
        with self.assertLogs(
            "custom_components.eufy_sdk.number", level="DEBUG"
        ) as logs:
            number()
        self.assertIn("recordDuration", logs.output[0])
        self.assertNotIn("synthetic", logs.output[0])


class NumberWriteTests(unittest.IsolatedAsyncioTestCase):
    async def test_out_of_range_nonfinite_and_off_step_do_not_write(self):
        entity = number(min=10, max=120, step=1)
        for value in (
            9,
            121,
            10.5,
            float("nan"),
            float("inf"),
            -float("inf"),
            True,
            "10",
            10**1000,
        ):
            with self.subTest(value=value), self.assertRaises(HomeAssistantError):
                await entity.async_set_native_value(value)
        entity.write.assert_not_awaited()

    async def test_endpoints_are_sent_as_integers(self):
        entity = number(min=10, max=120, step=1)
        for value in (10.0, 120.0):
            await entity.async_set_native_value(value)
            self.assertIs(type(entity.write.await_args.args[0]), int)
            self.assertEqual(entity.write.await_args.args, (int(value),))

    async def test_fractional_increment_uses_minimum_as_origin(self):
        entity = number(kind="scalar", min=0.1, max=0.6, step=0.1)
        await entity.async_set_native_value(0.1 + 0.1 + 0.1)
        self.assertAlmostEqual(entity.write.await_args.args[0], 0.3)
        entity.write.reset_mock()
        with self.assertRaises(HomeAssistantError):
            await entity.async_set_native_value(0.35)
        entity.write.assert_not_awaited()

    async def test_fallback_range_still_rejects_values_above_existing_maximum(self):
        entity = number()
        with self.assertRaises(HomeAssistantError):
            await entity.async_set_native_value(86401)
        entity.write.assert_not_awaited()
