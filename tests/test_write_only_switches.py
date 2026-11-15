"""Write-only switch state belongs to successful HA writes, including on restart."""

# ruff: noqa: ANN201, D102, FBT003, INP001, PT009, PT027, SLF001

import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.core import HomeAssistant, State
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.json import json_dumps
from homeassistant.helpers.restore_state import (
    DATA_RESTORE_STATE,
    RestoreEntity,
    RestoreStateData,
    StoredState,
)
from homeassistant.util.json import json_loads

from custom_components.eufy_sdk.switch import (
    EufySdkSwitch,
    EufySdkWriteOnlySwitch,
    async_setup_entry,
)

SN = "synthetic-camera"
ENTITY_ID = "switch.synthetic_camera_image_flipped"
SPEC = {"name": "imageFlipped", "type": "bool", "writable": True, "writeOnly": True}
OFFLINE_DIR = str(Path(__file__).resolve().parents[1] / ".offline")


def coordinator():
    """Provide only local data, listeners and a synthetic command client."""
    return SimpleNamespace(
        data={SN: {"state": {"imageFlipped": False}}},
        config_entry=SimpleNamespace(
            runtime_data=SimpleNamespace(
                client=SimpleNamespace(set_property=AsyncMock())
            )
        ),
        property_observations=None,
        last_update_success=True,
        snapshot_generation=0,
        completed_snapshot_generation=0,
        async_add_listener=Mock(return_value=Mock()),
        async_request_refresh=AsyncMock(),
    )


class WriteOnlySwitchTests(unittest.IsolatedAsyncioTestCase):
    """Use actual switch/write/restore methods with synthetic collaborators."""

    def entity(self):
        entity = EufySdkWriteOnlySwitch(coordinator(), SN, dict(SPEC))
        entity.hass = Mock(data={})
        entity.entity_id = ENTITY_ID
        entity.async_write_ha_state = Mock()
        entity.async_on_remove = Mock()
        return entity

    async def test_setup_selects_restore_only_for_write_only_non_light_switches(self):
        coord = coordinator()
        entry = SimpleNamespace(
            runtime_data=SimpleNamespace(
                coordinator=coord,
                properties={
                    SN: [
                        dict(SPEC),
                        {**SPEC, "name": "enabled", "writeOnly": False},
                        {**SPEC, "name": "light"},
                    ]
                },
            )
        )
        add = Mock()
        await async_setup_entry(Mock(), entry, add)
        entities = {entity._prop: entity for entity in add.call_args.args[0]}
        self.assertIsInstance(entities["imageFlipped"], RestoreEntity)
        self.assertIs(type(entities["enabled"]), EufySdkSwitch)
        self.assertIs(type(entities["light"]), EufySdkSwitch)
        self.assertFalse(entities["enabled"].assumed_state)
        self.assertFalse(entities["light"].assumed_state)

    async def test_unknown_before_write_ignores_cached_values_and_reports(self):
        entity = self.entity()
        self.assertTrue(entity.assumed_state)
        for value in (False, True, None):
            entity.coordinator.data[SN]["state"]["imageFlipped"] = value
            entity._handle_coordinator_update()
            entity._handle_property_changed(
                SimpleNamespace(
                    data={
                        "event": "propertyChanged",
                        "deviceSn": SN,
                        "property": "imageFlipped",
                        "value": value,
                    }
                )
            )
            self.assertIsNone(entity.is_on)
            self.assertNotIn("last_written_value", entity.extra_state_attributes)

    async def test_successful_writes_survive_reports_without_post_write_refresh(self):
        entity = self.entity()
        with patch("custom_components.eufy_sdk.entity.async_call_later") as timer:
            await entity.async_turn_on()
            self.assertIs(entity.is_on, True)
            entity.client.set_property.assert_awaited_once_with(
                SN, "imageFlipped", True
            )
            entity.coordinator.data[SN]["state"]["imageFlipped"] = False
            entity._handle_coordinator_update()
            entity._handle_property_changed(
                SimpleNamespace(
                    data={
                        "event": "propertyChanged",
                        "deviceSn": SN,
                        "property": "imageFlipped",
                        "value": False,
                    }
                )
            )
            self.assertIs(entity.is_on, True)
            await entity.async_turn_off()
            self.assertIs(entity.is_on, False)
            self.assertIs(entity.extra_state_attributes["last_written_value"], False)
        timer.assert_not_called()
        entity.coordinator.async_request_refresh.assert_not_awaited()

    async def test_failed_write_preserves_previous_value(self):
        entity = self.entity()
        entity.client.set_property.side_effect = RuntimeError("synthetic failure")
        with self.assertRaises(RuntimeError):
            await entity.async_turn_on()
        self.assertIsNone(entity.is_on)
        entity.client.set_property.side_effect = None
        await entity.async_turn_on()
        entity.client.set_property.side_effect = RuntimeError("synthetic failure")
        with self.assertRaises(RuntimeError):
            await entity.async_turn_off()
        self.assertIs(entity.is_on, True)

    async def test_superseded_success_is_kept_when_newer_write_fails(self):
        entity = self.entity()
        release = asyncio.Event()
        calls = []

        async def set_property(_sn, _prop, value):
            calls.append(value)
            if value is True:
                await release.wait()
                return
            raise RuntimeError("synthetic failure")

        entity.client.set_property.side_effect = set_property
        first = asyncio.ensure_future(entity.async_turn_on())
        await asyncio.sleep(0)
        with self.assertRaises(RuntimeError):
            await entity.async_turn_off()
        release.set()
        await first
        self.assertEqual(calls, [True, False])
        self.assertIs(entity.is_on, True)

    async def test_older_success_never_overrides_a_newer_success(self):
        entity = self.entity()
        release = asyncio.Event()

        async def set_property(_sn, _prop, value):
            if value is True:
                await release.wait()

        entity.client.set_property.side_effect = set_property
        first = asyncio.ensure_future(entity.async_turn_on())
        await asyncio.sleep(0)
        await entity.async_turn_off()
        release.set()
        await first
        self.assertIs(entity.is_on, False)

    async def test_restore_survives_a_save_made_while_unavailable(self):
        entity = self.entity()
        hass = HomeAssistant(OFFLINE_DIR)
        entity.hass = hass
        data = hass.data[DATA_RESTORE_STATE] = RestoreStateData(hass)
        with patch.object(Entity, "async_internal_added_to_hass", AsyncMock()):
            await entity.async_internal_added_to_hass()
        await entity.async_turn_on()
        # HA saves an unavailable entity without its attributes.
        hass.states.async_set(ENTITY_ID, "unavailable", {})
        with patch.object(Entity, "async_internal_will_remove_from_hass", AsyncMock()):
            await entity.async_internal_will_remove_from_hass()
        stored = StoredState.from_dict(
            json_loads(json_dumps(data.last_states[ENTITY_ID].as_dict()))
        )
        self.assertNotIn("last_written_value", stored.state.attributes)

        restarted = self.entity()
        restarted.hass = HomeAssistant(OFFLINE_DIR)
        restored_data = RestoreStateData(restarted.hass)
        restored_data.last_states[ENTITY_ID] = stored
        restarted.hass.data[DATA_RESTORE_STATE] = restored_data
        with patch("custom_components.eufy_sdk.entity.async_listen_bridge_event"):
            await restarted.async_added_to_hass()
        self.assertIs(restarted.is_on, True)

    async def test_restore_round_trip_keeps_both_on_and_off_after_removal(self):
        for value in (True, False):
            with self.subTest(value=value):
                entity = self.entity()
                hass = HomeAssistant(OFFLINE_DIR)
                entity.hass = hass
                data = hass.data[DATA_RESTORE_STATE] = RestoreStateData(hass)
                with patch.object(Entity, "async_internal_added_to_hass", AsyncMock()):
                    await entity.async_internal_added_to_hass()
                await (entity.async_turn_on() if value else entity.async_turn_off())
                # Model the state HA publishes before unloading, using the actual
                # RestoreEntity removal path and persisted JSON representation.
                hass.states.async_set(
                    ENTITY_ID, entity.state, entity.extra_state_attributes
                )
                await entity.async_will_remove_from_hass()
                with patch.object(
                    Entity, "async_internal_will_remove_from_hass", AsyncMock()
                ):
                    await entity.async_internal_will_remove_from_hass()
                stored = StoredState.from_dict(
                    json_loads(json_dumps(data.last_states[ENTITY_ID].as_dict()))
                )

                restarted = self.entity()
                restarted.hass = HomeAssistant(OFFLINE_DIR)
                restored_data = RestoreStateData(restarted.hass)
                restored_data.last_states[ENTITY_ID] = stored
                restarted.hass.data[DATA_RESTORE_STATE] = restored_data
                restarted.coordinator.data[SN]["state"]["imageFlipped"] = not value
                with patch(
                    "custom_components.eufy_sdk.entity.async_listen_bridge_event"
                ):
                    await restarted.async_added_to_hass()
                self.assertIs(restarted.is_on, value)
                self.assertTrue(restarted.assumed_state)
                restarted.client.set_property.assert_not_awaited()

    async def test_legacy_or_unknown_state_does_not_invent_a_written_value(self):
        for state in (
            None,
            State(ENTITY_ID, "off"),
            State(ENTITY_ID, "unknown"),
            State(ENTITY_ID, "off", {"last_written_value": "false"}),
        ):
            entity = self.entity()
            entity.async_get_last_state = AsyncMock(return_value=state)
            entity.async_get_last_extra_data = AsyncMock(return_value=None)
            with patch("custom_components.eufy_sdk.entity.async_listen_bridge_event"):
                await entity.async_added_to_hass()
            self.assertIsNone(entity.is_on)

    async def test_non_write_only_switch_keeps_report_and_refresh_behavior(self):
        entity = EufySdkSwitch(coordinator(), SN, {**SPEC, "writeOnly": False})
        entity.hass = Mock(data={})
        entity.async_write_ha_state = Mock()
        self.assertIs(entity.is_on, False)
        self.assertFalse(entity.assumed_state)
        cancel = Mock()
        with patch(
            "custom_components.eufy_sdk.entity.async_call_later", return_value=cancel
        ) as timer:
            await entity.async_turn_on()
        self.assertIs(entity.is_on, True)
        timer.assert_called_once()
        entity._handle_property_changed(
            SimpleNamespace(
                data={
                    "event": "propertyChanged",
                    "deviceSn": SN,
                    "property": "imageFlipped",
                    "value": False,
                }
            )
        )
        self.assertIs(entity.is_on, False)
        cancel.assert_called_once()
