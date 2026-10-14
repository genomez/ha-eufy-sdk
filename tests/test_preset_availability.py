# ruff: noqa: ANN201, D100, D101, D102, INP001, PT009, PT027, SLF001, FBT001, FBT003
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.exceptions import HomeAssistantError

from custom_components.eufy_sdk import presets
from custom_components.eufy_sdk.button import EufySdkGotoPresetButton
from custom_components.eufy_sdk.entity import EufySdkDeviceEntity
from custom_components.eufy_sdk.select import ATTR_SLOTS, EufySdkPresetSelect


def snapshot(supported: bool) -> dict:
    """Build a bound synthetic manifest with optional preset reads."""
    return {
        "streaming": True,
        "decodedProperties": {
            "bound": True,
            "details": [
                {
                    "accessor": "ptz",
                    "actions": [],
                    "undescribedActions": ["preset.list"] if supported else [],
                }
            ],
        },
    }


class SlotTests(unittest.TestCase):
    def test_fallback_numbers_are_not_occupied_evidence(self):
        slots = presets.fallback_slots()
        self.assertEqual(len(slots), 8)
        self.assertTrue(all(s.occupied is None for s in slots))
        self.assertTrue(all("unverified" in s.label for s in slots))
        self.assertEqual(presets.from_dicts(presets.as_dicts(slots)), slots)

    def test_observed_slots_round_trip_and_legacy_choice_stays_unverified(self):
        slots = [presets.PresetSlot(2, True, True), presets.PresetSlot(4, False, False)]
        self.assertEqual(presets.from_dicts(presets.as_dicts(slots)), slots)
        legacy = presets.from_dicts([{"index": 5, "occupied": True, "default": False}])
        self.assertEqual(legacy[0].index, 5)
        self.assertIsNone(legacy[0].occupied)
        empty = presets.from_dicts([{"index": 4, "occupied": False, "default": False}])
        self.assertIs(empty[0].occupied, False)

    def test_reply_without_occupancy_does_not_invent_it(self):
        self.assertIsNone(presets.parse_slots([{"id": 2, "raw": {}}])[0].occupied)

    def test_only_bound_list_is_readable(self):
        self.assertTrue(presets.list_supported(snapshot(True)))
        self.assertFalse(presets.list_supported(snapshot(False)))
        self.assertFalse(presets.list_supported({}))
        device = snapshot(True)
        device["decodedProperties"]["bound"] = False
        self.assertFalse(presets.list_supported(device))
        device = snapshot(False)
        device["decodedProperties"]["details"][0]["actions"] = [{"name": "preset.list"}]
        self.assertTrue(presets.list_supported(device))


class SelectTests(unittest.IsolatedAsyncioTestCase):
    def entity(self, supported: bool):
        runtime = SimpleNamespace(
            client=Mock(
                preset_slots=AsyncMock(
                    return_value=[
                        {"id": 2, "raw": {"enable": 1, "isdefault": 1}},
                        {"id": 4, "raw": {"enable": 0}},
                    ]
                )
            ),
            preset_slots={},
            selected_preset={},
        )
        coordinator = Mock(
            data={"synthetic": snapshot(supported)},
            config_entry=SimpleNamespace(runtime_data=runtime),
        )
        entity = EufySdkPresetSelect(coordinator, "synthetic")
        entity.hass = Mock()
        entity.async_write_ha_state = Mock()
        entity.async_get_last_state = AsyncMock(return_value=None)
        return entity, runtime

    async def test_startup_and_streaming_skip_unsupported_reads_keep_saved_choice(self):
        entity, runtime = self.entity(False)
        saved = [presets.PresetSlot(2, True, True), presets.PresetSlot(4, False, False)]
        entity.async_get_last_state.return_value = SimpleNamespace(
            state="4 · empty",
            attributes={ATTR_SLOTS: presets.as_dicts(saved)},
        )
        with patch.object(EufySdkDeviceEntity, "async_added_to_hass", new=AsyncMock()):
            await entity.async_added_to_hass()
        with patch.object(EufySdkDeviceEntity, "_handle_coordinator_update"):
            entity._handle_coordinator_update()
        runtime.client.preset_slots.assert_not_awaited()
        entity.hass.async_create_task.assert_not_called()
        self.assertEqual(runtime.preset_slots["synthetic"], saved)
        self.assertEqual(runtime.selected_preset["synthetic"], 4)
        self.assertEqual(entity.current_option, "4 · empty")

    async def test_supported_startup_reads_once_and_publishes_device_slots(self):
        entity, runtime = self.entity(True)
        with patch.object(EufySdkDeviceEntity, "async_added_to_hass", new=AsyncMock()):
            await entity.async_added_to_hass()
        runtime.client.preset_slots.assert_awaited_once_with("synthetic")
        self.assertEqual(entity.options, ["2 · HOME", "4 · empty"])

    async def test_supported_stream_refresh_and_failed_read_preserve_selection(self):
        entity, runtime = self.entity(True)
        saved = [presets.PresetSlot(2, True, True), presets.PresetSlot(4, True, False)]
        presets.remember(entity.coordinator.config_entry, "synthetic", saved)
        entity._apply(saved)
        await entity.async_select_option("4")
        runtime.client.preset_slots.side_effect = RuntimeError("synthetic failure")
        with patch.object(EufySdkDeviceEntity, "_handle_coordinator_update"):
            entity._handle_coordinator_update()
        pending = entity.hass.async_create_task.call_args.args[0]
        await pending
        runtime.client.preset_slots.assert_awaited_once()
        self.assertEqual(entity.current_option, "4")
        self.assertEqual(runtime.preset_slots["synthetic"], saved)

    async def test_legacy_fallback_choice_survives_without_claiming_occupancy(self):
        entity, runtime = self.entity(False)
        entity.async_get_last_state.return_value = SimpleNamespace(
            state="5",
            attributes={
                ATTR_SLOTS: [
                    {"index": i, "occupied": True, "default": False} for i in range(8)
                ]
            },
        )
        with patch.object(EufySdkDeviceEntity, "async_added_to_hass", new=AsyncMock()):
            await entity.async_added_to_hass()
        self.assertEqual(runtime.selected_preset["synthetic"], 5)
        self.assertEqual(entity.current_option, "5 · unverified")
        runtime.client.preset_slots.assert_not_awaited()

    async def test_unknown_occupancy_does_not_disable_existing_preset_movement(self):
        entity, runtime = self.entity(False)
        runtime.client.action = AsyncMock()
        runtime.selected_preset["synthetic"] = 5
        button = EufySdkGotoPresetButton(entity.coordinator, "synthetic")
        await button.async_press()
        runtime.client.action.assert_awaited_once_with("synthetic", "preset.preview", 5)

    async def test_confirmed_empty_slot_still_refuses_movement(self):
        entity, runtime = self.entity(True)
        runtime.client.action = AsyncMock()
        runtime.preset_slots["synthetic"] = [presets.PresetSlot(2, False, False)]
        button = EufySdkGotoPresetButton(entity.coordinator, "synthetic")
        with self.assertRaises(HomeAssistantError):
            await button.async_press()
        runtime.client.action.assert_not_awaited()
