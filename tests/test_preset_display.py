# ruff: noqa: ANN201, D100, D101, D102, INP001, PT009, PT027, SLF001
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.exceptions import HomeAssistantError

from custom_components.eufy_sdk import presets
from custom_components.eufy_sdk.button import async_setup_entry as setup_buttons
from custom_components.eufy_sdk.entity import EufySdkDeviceEntity
from custom_components.eufy_sdk.select import ATTR_SLOTS, EufySdkPresetSelect
from custom_components.eufy_sdk.select import async_setup_entry as setup_selects


def make_entity():
    """Build a synthetic preset-capable camera without external I/O."""
    runtime = SimpleNamespace(
        client=Mock(action=AsyncMock(), preset_slots=AsyncMock(return_value=[])),
        preset_slots={},
        selected_preset={},
        properties={},
    )
    device = {
        "capabilities": ["ptz"],
        "decodedProperties": {
            "bound": True,
            "details": [
                {
                    "accessor": "ptz",
                    "undescribedActions": [
                        "left",
                        "right",
                        "up",
                        "down",
                        "preset.preview",
                        "preset.list",
                    ],
                }
            ],
        },
    }
    coordinator = Mock(
        data={"synthetic": device},
        solix_devices={},
        config_entry=SimpleNamespace(runtime_data=runtime),
    )
    runtime.coordinator = coordinator
    entity = EufySdkPresetSelect(coordinator, "synthetic")
    entity.hass = Mock()
    entity.async_write_ha_state = Mock()
    entity.async_get_last_state = AsyncMock(return_value=None)
    return entity, runtime


class DisplayTests(unittest.TestCase):
    def test_fallback_is_four_app_numbered_slots_with_default_first(self):
        slots = presets.fallback_slots()
        self.assertEqual([slot.index for slot in slots], [0, 1, 2, 3])
        self.assertEqual(
            [slot.label for slot in slots],
            ["1", "2", "3", "4"],
        )
        self.assertTrue(all(slot.occupied is None for slot in slots))

    def test_reported_slots_without_occupancy_are_confirmed_choices(self):
        slots = presets.parse_slots([{"id": 3}, {"id": 0}])
        self.assertEqual([slot.label for slot in slots], ["1", "4"])
        self.assertTrue(all(slot.occupied is None for slot in slots))
        self.assertEqual(presets.from_dicts(presets.as_dicts(slots)), slots)

    def test_old_synthetic_eight_slots_are_discarded_on_restore(self):
        legacy = [{"index": i, "occupied": True, "default": False} for i in range(8)]
        self.assertEqual(presets.from_dicts(legacy), [])

    def test_confirmed_occupied_legacy_slots_survive_above_fallback_range(self):
        restored = presets.from_dicts(
            [
                {"index": 6, "occupied": True, "observed": True},
                {"index": 7, "occupied": None, "observed": False},
            ]
        )
        self.assertEqual([slot.index for slot in restored], [6])
        self.assertEqual(restored[0].label, "7")

    def test_go_translation_is_removed_in_every_language(self):
        root = (
            Path(__file__).resolve().parents[1]
            / "custom_components/eufy_sdk/translations"
        )
        for path in root.glob("*.json"):
            with self.subTest(language=path.stem):
                data = json.loads(path.read_text(encoding="utf-8"))
                self.assertNotIn("preset_goto", data["entity"]["button"])


class InteractionTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_app_option_moves_to_unchanged_zero_based_wire_index(self):
        entity, runtime = make_entity()
        for index, label in enumerate(entity.options):
            await entity.async_select_option(label)
            self.assertEqual(
                runtime.client.action.await_args.args,
                ("synthetic", "preset.preview", index),
            )
        self.assertEqual(runtime.client.action.await_count, 4)

    async def test_movement_failure_keeps_previous_choice(self):
        entity, runtime = make_entity()
        previous = entity.current_option
        runtime.client.action.side_effect = HomeAssistantError("synthetic failure")
        with self.assertRaises(HomeAssistantError):
            await entity.async_select_option(entity.options[2])
        self.assertEqual(entity.current_option, previous)
        entity.async_write_ha_state.assert_not_called()

    async def test_unknown_option_is_rejected_without_moving(self):
        entity, runtime = make_entity()
        previous = entity.current_option
        with self.assertRaises(HomeAssistantError):
            await entity.async_select_option("5 · unverified")
        runtime.client.action.assert_not_awaited()
        self.assertEqual(entity.current_option, previous)

    async def test_confirmed_empty_option_is_rejected_without_moving(self):
        entity, runtime = make_entity()
        entity._apply(presets.parse_slots([{"id": 0, "raw": {"enable": 0}}]))
        with self.assertRaises(HomeAssistantError):
            await entity.async_select_option(entity.options[0])
        runtime.client.action.assert_not_awaited()

    async def test_options_omit_empty_slots_but_attributes_keep_all_details(self):
        entity, runtime = make_entity()
        slots = presets.parse_slots(
            [
                {"id": 0, "raw": {"enable": 0, "isdefault": 1}},
                {"id": 2, "raw": {"enable": 1}},
                {"id": 6},
            ]
        )
        entity._apply(slots)
        self.assertEqual(entity.options, ["3", "7"])
        self.assertEqual(entity.current_option, "3")
        self.assertEqual(
            entity.extra_state_attributes[ATTR_SLOTS], presets.as_dicts(slots)
        )
        self.assertEqual(
            entity.extra_state_attributes[ATTR_SLOTS][0],
            {
                "index": 0,
                "occupied": False,
                "default": True,
                "observed": True,
                "reported": True,
            },
        )
        with self.assertRaises(HomeAssistantError):
            await entity.async_select_option("1")
        runtime.client.action.assert_not_awaited()

    async def test_all_empty_slots_keep_their_numbers_but_refuse_movement(self):
        entity, runtime = make_entity()
        entity._apply(
            presets.parse_slots(
                [{"id": 2, "raw": {"enable": 0}}, {"id": 6, "raw": {"enable": 0}}]
            )
        )
        self.assertEqual(entity.options, ["3", "7"])
        self.assertEqual(entity.current_option, "3")
        for option in entity.options:
            with self.assertRaises(HomeAssistantError):
                await entity.async_select_option(option)
        runtime.client.action.assert_not_awaited()

    async def test_arbitrary_reported_indices_are_not_renumbered_on_wire(self):
        entity, runtime = make_entity()
        entity._apply(presets.parse_slots([{"id": 9}, {"id": 2}, {"id": 6}]))
        self.assertEqual(entity.options, ["3", "7", "10"])
        await entity.async_select_option("7")
        runtime.client.action.assert_awaited_once_with("synthetic", "preset.preview", 6)

    async def test_fallback_ack_does_not_claim_confirmed_occupancy(self):
        entity, runtime = make_entity()
        await entity.async_select_option(entity.options[1])
        runtime.client.action.assert_awaited_once()
        self.assertEqual(entity.current_option, "2")
        self.assertIsNone(entity.extra_state_attributes[ATTR_SLOTS][1]["occupied"])

    async def test_refresh_relabels_selection_without_a_move(self):
        entity, runtime = make_entity()
        await entity.async_select_option(entity.options[2])
        runtime.client.action.reset_mock()
        runtime.client.preset_slots.return_value = [{"id": 0}, {"id": 2}]
        await entity._reread()
        self.assertEqual(entity.options, ["1", "3"])
        self.assertEqual(entity.current_option, "3")
        runtime.client.action.assert_not_awaited()

    async def test_empty_reply_replaces_stale_slots_with_four_fallbacks(self):
        entity, runtime = make_entity()
        presets.remember(
            entity.coordinator.config_entry,
            "synthetic",
            presets.parse_slots([{"id": 7}]),
        )
        await entity._reread()
        self.assertEqual(len(entity.options), 4)
        self.assertEqual([slot.index for slot in entity._slots], [0, 1, 2, 3])
        runtime.client.action.assert_not_awaited()

    async def test_restore_labeled_choice_keeps_displayed_number_without_moving(self):
        entity, runtime = make_entity()
        entity.device["decodedProperties"]["details"][0]["undescribedActions"] = [
            "preset.preview"
        ]
        entity.async_get_last_state.return_value = SimpleNamespace(
            state="2 · unverified",
            attributes={ATTR_SLOTS: presets.as_dicts(presets.fallback_slots())},
        )
        with patch.object(EufySdkDeviceEntity, "async_added_to_hass", new=AsyncMock()):
            await entity.async_added_to_hass()
        self.assertEqual(entity.current_option, "2")
        self.assertEqual(runtime.selected_preset["synthetic"], 1)
        runtime.client.action.assert_not_awaited()

    async def test_new_choice_restores_using_explicit_index_without_moving(self):
        entity, _runtime = make_entity()
        await entity.async_select_option(entity.options[2])
        state = SimpleNamespace(
            state=entity.current_option, attributes=entity.extra_state_attributes
        )
        restored, restored_runtime = make_entity()
        restored.device["decodedProperties"]["details"][0]["undescribedActions"] = [
            "preset.preview"
        ]
        restored.async_get_last_state.return_value = state
        with patch.object(EufySdkDeviceEntity, "async_added_to_hass", new=AsyncMock()):
            await restored.async_added_to_hass()
        self.assertEqual(restored.current_option, "3")
        self.assertEqual(restored_runtime.selected_preset["synthetic"], 2)
        restored_runtime.client.action.assert_not_awaited()

    async def test_old_labeled_states_restore_only_valid_numbered_options(self):
        for state, index, expected in [
            ("3 · default · unverified", 2, "3"),
            ("7 · default", 6, "7"),
            ("7 · default", None, "7"),
            ("1 · default · empty", 0, "3"),
            ("1 · default · empty", None, "3"),
            ("0 · HOME", None, "3"),
            ("99 · unverified", 98, "3"),
            ("unknown", None, "3"),
        ]:
            with self.subTest(state=state):
                entity, runtime = make_entity()
                entity.device["decodedProperties"]["details"][0][
                    "undescribedActions"
                ] = ["preset.preview"]
                entity.async_get_last_state.return_value = SimpleNamespace(
                    state=state,
                    attributes={
                        "selected_index": index,
                        ATTR_SLOTS: presets.as_dicts(
                            presets.parse_slots(
                                [
                                    {"id": 0, "raw": {"enable": 0}},
                                    {"id": 2, "raw": {"enable": 1}},
                                    {"id": 6, "raw": {"enable": 1}},
                                ]
                            )
                        ),
                    },
                )
                with patch.object(
                    EufySdkDeviceEntity, "async_added_to_hass", new=AsyncMock()
                ):
                    await entity.async_added_to_hass()
                self.assertEqual(entity.current_option, expected)
                self.assertIn(entity.current_option, entity.options)
                self.assertEqual(
                    runtime.selected_preset["synthetic"], int(expected) - 1
                )
                runtime.client.action.assert_not_awaited()

    async def test_ptz_setup_preserves_87_104_88_directions_without_go_buttons(self):
        for device_type, model, has_presets in [
            (87, "T8426", False),
            (104, "T8416", True),
            (88, "T8171", True),
        ]:
            with self.subTest(device_type=device_type):
                entity, runtime = make_entity()
                entity.device.update(deviceType=device_type, model=model)
                if not has_presets:
                    entity.device["decodedProperties"]["details"][0][
                        "undescribedActions"
                    ] = [
                        "left",
                        "right",
                        "up",
                        "down",
                    ]
                buttons, selects = [], []
                await setup_buttons(
                    Mock(), entity.coordinator.config_entry, buttons.extend
                )
                await setup_selects(
                    Mock(), entity.coordinator.config_entry, selects.extend
                )
                self.assertEqual(
                    [button._action for button in buttons],
                    ["up", "down", "left", "right"],
                )
                self.assertEqual(len(selects), int(has_presets))
                for button in buttons:
                    await button.async_press()
                self.assertEqual(
                    [call.args for call in runtime.client.action.await_args_list],
                    [
                        ("synthetic", direction)
                        for direction in ["up", "down", "left", "right"]
                    ],
                )
