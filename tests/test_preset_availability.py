# ruff: noqa: ANN201, D100, D101, D102, INP001, PT009, PT027, SLF001, FBT001, FBT003
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.exceptions import HomeAssistantError

from custom_components.eufy_sdk import presets
from custom_components.eufy_sdk.button import async_setup_entry as setup_buttons
from custom_components.eufy_sdk.entity import EufySdkDeviceEntity
from custom_components.eufy_sdk.select import ATTR_SLOTS, EufySdkPresetSelect
from custom_components.eufy_sdk.select import async_setup_entry as setup_selects


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
    def test_bound_movement_requires_preview_on_the_ptz_surface(self):
        device = snapshot(True)
        device["capabilities"] = ["ptz"]
        self.assertFalse(presets.movement_supported(device))
        detail = device["decodedProperties"]["details"][0]
        detail["actions"] = [{"name": "preset.preview"}]
        self.assertTrue(presets.movement_supported(device))
        detail["actions"] = []
        detail["undescribedActions"] = ["preset.preview"]
        self.assertTrue(presets.movement_supported(device))
        detail["accessor"] = "camera"
        self.assertFalse(presets.movement_supported(device))

    def test_legacy_and_unbound_ptz_keep_preset_movement(self):
        self.assertTrue(presets.movement_supported({"capabilities": ["ptz"]}))
        device = snapshot(False)
        device["capabilities"] = ["ptz"]
        device["decodedProperties"]["bound"] = False
        self.assertTrue(presets.movement_supported(device))
        self.assertFalse(presets.movement_supported({}))

    def test_bound_manifest_without_ptz_details_has_no_preset_movement(self):
        self.assertFalse(
            presets.movement_supported(
                {"capabilities": ["ptz"], "decodedProperties": {"bound": True}}
            )
        )

    def test_fallback_numbers_are_not_occupied_evidence(self):
        slots = presets.fallback_slots()
        self.assertEqual(len(slots), 4)
        self.assertTrue(all(s.occupied is None for s in slots))
        self.assertTrue(all("unverified" in s.label for s in slots))
        self.assertEqual(presets.from_dicts(presets.as_dicts(slots)), [])

    def test_observed_slots_round_trip_and_legacy_choice_stays_unverified(self):
        slots = [presets.PresetSlot(2, True, True), presets.PresetSlot(4, False, False)]
        self.assertEqual(presets.from_dicts(presets.as_dicts(slots)), slots)
        legacy = presets.from_dicts([{"index": 5, "occupied": True, "default": False}])
        self.assertEqual(legacy, [])
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
    async def test_setup_keeps_directions_but_omits_rotation_only_presets(self):
        device = snapshot(False)
        device["capabilities"] = ["ptz"]
        device["decodedProperties"]["details"][0]["undescribedActions"] = [
            "left",
            "right",
            "up",
            "down",
        ]
        await self.check_setup(device, preset_count=0)

    async def test_setup_preserves_existing_bound_and_legacy_presets(self):
        for model in ("T8425", "T8170"):
            with self.subTest(model=model):
                device = snapshot(True)
                device["model"] = model
                device["capabilities"] = ["ptz"]
                device["decodedProperties"]["details"][0]["actions"] = [
                    {"name": "preset.preview"}
                ]
                await self.check_setup(device, preset_count=1)
        for device in (
            {"capabilities": ["ptz"]},
            {"capabilities": ["ptz"], "decodedProperties": {"bound": False}},
        ):
            await self.check_setup(device, preset_count=1)

    async def check_setup(self, device: dict, preset_count: int):
        runtime = SimpleNamespace(
            preset_slots={},
            selected_preset={},
            properties={},
        )
        entry = SimpleNamespace(runtime_data=runtime)
        runtime.coordinator = Mock(
            data={"synthetic": device}, config_entry=entry, solix_devices={}
        )
        buttons = []
        selects = []
        await setup_buttons(Mock(), entry, buttons.extend)
        await setup_selects(Mock(), entry, selects.extend)
        self.assertEqual(len(buttons), 4)
        self.assertEqual(
            [item._action for item in buttons], ["up", "down", "left", "right"]
        )
        self.assertEqual(len(selects), preset_count)

    def entity(self, supported: bool):
        runtime = SimpleNamespace(
            client=Mock(
                action=AsyncMock(),
                preset_slots=AsyncMock(
                    return_value=[
                        {"id": 2, "raw": {"enable": 1, "isdefault": 1}},
                        {"id": 4, "raw": {"enable": 0}},
                    ]
                ),
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
        self.assertEqual(entity.current_option, "5 · empty")

    async def test_supported_startup_reads_once_and_publishes_device_slots(self):
        entity, runtime = self.entity(True)
        with patch.object(EufySdkDeviceEntity, "async_added_to_hass", new=AsyncMock()):
            await entity.async_added_to_hass()
        runtime.client.preset_slots.assert_awaited_once_with("synthetic")
        self.assertEqual(entity.options, ["3 · default", "5 · empty"])

    async def test_supported_stream_refresh_and_failed_read_preserve_selection(self):
        entity, runtime = self.entity(True)
        saved = [presets.PresetSlot(2, True, True), presets.PresetSlot(4, True, False)]
        presets.remember(entity.coordinator.config_entry, "synthetic", saved)
        entity._apply(saved)
        await entity.async_select_option("5")
        runtime.client.preset_slots.side_effect = RuntimeError("synthetic failure")
        with patch.object(EufySdkDeviceEntity, "_handle_coordinator_update"):
            entity._handle_coordinator_update()
        pending = entity.hass.async_create_task.call_args.args[0]
        await pending
        runtime.client.preset_slots.assert_awaited_once()
        self.assertEqual(entity.current_option, "5")
        self.assertEqual(runtime.preset_slots["synthetic"], saved)

    async def test_legacy_fallback_returns_to_four_without_claiming_occupancy(self):
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
        self.assertEqual(runtime.selected_preset["synthetic"], 0)
        self.assertEqual(entity.current_option, "1 · default · unverified")
        runtime.client.preset_slots.assert_not_awaited()

    async def test_unknown_occupancy_does_not_disable_existing_preset_movement(self):
        entity, runtime = self.entity(False)
        runtime.client.action = AsyncMock()
        await entity.async_select_option(entity.options[3])
        runtime.client.action.assert_awaited_once_with("synthetic", "preset.preview", 3)

    async def test_named_camera_slots_preserve_arbitrary_returned_ids(self):
        entity, runtime = self.entity(True)
        runtime.client.action = AsyncMock()
        runtime.client.preset_slots.return_value = [
            {"id": 9, "raw": {"name": "right", "enable": 1}},
            {"id": 2, "raw": {"name": "left", "enable": 1}},
            {"id": 6, "raw": {"name": "mid", "enable": 1}},
        ]
        await entity._reread()
        self.assertEqual(entity.options, ["3", "7", "10"])
        await entity.async_select_option("7")
        runtime.client.action.assert_awaited_once_with("synthetic", "preset.preview", 6)

    async def test_confirmed_empty_slot_still_refuses_movement(self):
        entity, runtime = self.entity(True)
        runtime.client.action = AsyncMock()
        entity._apply([presets.PresetSlot(2, False, False)])
        with self.assertRaises(HomeAssistantError):
            await entity.async_select_option(entity.options[0])
        runtime.client.action.assert_not_awaited()
