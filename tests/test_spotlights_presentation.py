"""Night-vision labels preserve wire values and connectivity has English labels."""

# ruff: noqa: ANN201, D101, D102, INP001, PT009

import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from custom_components.eufy_sdk.select import EufySdkSelect


def select_for(
    value: object,
    *,
    name: str = "nightVision",
    values: dict | None = None,
) -> EufySdkSelect:
    """Build a select with synthetic state and a legacy enum label."""
    coordinator = Mock(data={"synthetic": {"state": {name: value}}})
    return EufySdkSelect(
        coordinator,
        "synthetic",
        {
            "name": name,
            "type": "enum",
            "writable": True,
            "enumValues": (
                {"0": "Off", "1": "Infrared", "2": "Full Color"}
                if values is None
                else values
            ),
        },
    )


class SpotlightsPresentationTests(unittest.IsolatedAsyncioTestCase):
    def test_three_mode_order_and_stable_identity(self):
        for values in (
            {"0": "Off", "1": "Infrared", "2": "Full Color"},
            {2: "Full Color", 0: "Off", 1: "Infrared"},
        ):
            with self.subTest(values=values):
                select = select_for(2, values=values)
                self.assertEqual(select.options, ["Infrared", "Spotlights", "Off"])
                self.assertEqual(select.unique_id, "synthetic_nightVision")
                self.assertEqual(select.name, "Night Vision")
                self.assertNotIn("Spotlights", values.values())

    async def test_all_options_roundtrip_with_integer_wire_values(self):
        select = select_for(None)
        with patch.object(select, "write", new_callable=AsyncMock) as write:
            for label, raw in (("Infrared", 1), ("Spotlights", 2), ("Off", 0)):
                with self.subTest(label=label):
                    await select.async_select_option(label)
                    write.assert_awaited_with(raw)
                    self.assertIs(type(write.await_args.args[0]), int)
                    for restored in (raw, str(raw)):
                        select.coordinator.data["synthetic"]["state"]["nightVision"] = (
                            restored
                        )
                        self.assertEqual(select.current_option, label)
                    self.assertEqual(select.unique_id, "synthetic_nightVision")

    def test_missing_and_unrecognized_raw_values_are_unknown(self):
        for raw in (None, 99, "future"):
            with self.subTest(raw=raw):
                self.assertIsNone(select_for(raw).current_option)

    async def test_old_label_is_not_a_hidden_write_alias(self):
        select = select_for(2)
        with patch.object(select, "write", new_callable=AsyncMock) as write:
            await select.async_select_option("Full Color")
            write.assert_not_awaited()
        self.assertEqual(select.current_option, "Spotlights")

    async def test_other_numeric_enum_keeps_labels_order_identity_and_wire(self):
        select = select_for(2, name="recordingMode")
        self.assertEqual(select.options, ["Off", "Infrared", "Full Color"])
        self.assertEqual(select.current_option, "Full Color")
        self.assertEqual(select.unique_id, "synthetic_recordingMode")
        with patch.object(select, "write", new_callable=AsyncMock) as write:
            await select.async_select_option("Full Color")
            write.assert_awaited_once_with(2)
            self.assertIs(type(write.await_args.args[0]), int)

    async def test_other_string_enum_preserves_wire_values(self):
        select = select_for(
            "color", name="pictureStyle", values={"color": "Full Color", "off": "Off"}
        )
        self.assertEqual(select.options, ["Full Color", "Off"])
        self.assertEqual(select.current_option, "Full Color")
        with patch.object(select, "write", new_callable=AsyncMock) as write:
            await select.async_select_option("Full Color")
            write.assert_awaited_once_with("color")

    def test_nonmatching_night_vision_modes_keep_their_original_order(self):
        for values, expected in (
            ({"0": "Off", "1": "Infrared"}, ["Off", "Infrared"]),
            (
                {"0": "Off", "1": "Infrared", "2": "Full Color", "3": "Auto"},
                ["Off", "Infrared", "Spotlights", "Auto"],
            ),
        ):
            with self.subTest(values=values):
                self.assertEqual(select_for(0, values=values).options, expected)


class ConnectivityTranslationTests(unittest.TestCase):
    def test_source_and_english_cover_binary_and_every_enum_state(self):
        root = Path(__file__).resolve().parents[1] / "custom_components" / "eufy_sdk"
        for relative in ("strings.json", "translations/en.json"):
            with self.subTest(path=relative):
                entities = json.loads((root / relative).read_text(encoding="utf-8"))[
                    "entity"
                ]
                self.assertEqual(
                    entities["binary_sensor"]["connectivity"]["name"], "Connectivity"
                )
                self.assertEqual(
                    entities["sensor"]["connectivity_status"],
                    {
                        "name": "Connectivity status",
                        "state": {
                            "online": "Online",
                            "suspect": "Suspect",
                            "offline": "Offline",
                            "unknown": "Unknown",
                        },
                    },
                )
