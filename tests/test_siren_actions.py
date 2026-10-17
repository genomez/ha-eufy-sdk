"""Siren entities require callable SDK actions and device-reported state."""

# Modified for genomez S1 experimental source preview, 2026-10-17.
# ruff: noqa: ANN001, ANN201, D102, INP001, PT009, PT027

import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from homeassistant.components.siren import SirenEntityFeature
from homeassistant.exceptions import HomeAssistantError

from custom_components.eufy_sdk.siren import (
    DEFAULT_DURATION_SECS,
    EufySdkSiren,
    async_setup_entry,
    has_siren_actions,
)


def record(codec="camera", actions=("trigger", "stop"), undescribed=()):
    """Construct a synthetic bound siren descriptor without device access."""
    return {
        "codec": codec,
        "capabilities": ["siren"],
        "state": {},
        "decodedProperties": {
            "bound": True,
            "details": [
                {
                    "capability": "siren",
                    "accessor": "siren",
                    "actions": [{"name": name} for name in actions],
                    "undescribedActions": list(undescribed),
                }
            ],
        },
    }


def make_entity(device):
    """Bind an entity to an in-memory coordinator and mocked command client."""
    client = SimpleNamespace(action=AsyncMock())
    coordinator = Mock(data={"synthetic": device}, last_update_success=True)
    coordinator.config_entry.runtime_data.client = client
    entity = EufySdkSiren(coordinator, "synthetic")
    entity.hass = Mock()
    entity.async_write_ha_state = Mock()
    return entity, coordinator, client


class SirenActionTests(unittest.IsolatedAsyncioTestCase):
    """Exercise action gates, command compatibility and reported state only."""

    async def test_malformed_manifests_fail_closed_without_rpc(self):
        invalid = [None, [], "siren", False]
        for value in (None, "siren", {"siren": True}):
            candidate = record()
            candidate["capabilities"] = value
            invalid.append(candidate)
        for field, values in (
            (
                "actions",
                (
                    None,
                    {},
                    "trigger",
                    [{"name": "trigger"}, {"name": "stop"}, None],
                    [{"name": "trigger"}, {"name": "stop"}, {"name": 1}],
                ),
            ),
            ("undescribedActions", (None, {}, "stop", [None], ["stop", 1])),
            ("capability", (None, "camera")),
            ("accessor", (None, "camera")),
        ):
            for value in values:
                candidate = record()
                candidate["decodedProperties"]["details"][0][field] = value
                invalid.append(candidate)
        for value in (None, {}, "siren"):
            candidate = record()
            candidate["decodedProperties"]["details"] = value
            invalid.append(candidate)
        for value in (None, [], "manifest"):
            candidate = record()
            candidate["decodedProperties"] = value
            invalid.append(candidate)
        candidate = record()
        candidate["decodedProperties"]["details"].append(None)
        invalid.append(candidate)
        candidate = record()
        candidate["decodedProperties"]["details"] *= 2
        invalid.append(candidate)
        for value in invalid:
            with self.subTest(value=value):
                self.assertFalse(has_siren_actions(value))
        for value in invalid[4:]:
            entity, _coordinator, client = make_entity(value)
            self.assertFalse(entity.available)
            with self.assertRaises(HomeAssistantError):
                await entity.async_turn_on()
            with self.assertRaises(HomeAssistantError):
                await entity.async_turn_off()
            client.action.assert_not_awaited()

    async def test_only_reported_booleans_establish_state(self):
        entity, coordinator, _client = make_entity(record())
        for value in (None, 0, 1, "false", "true", [], {}):
            coordinator.data["synthetic"]["state"] = {"siren": value}
            self.assertIsNone(entity.is_on)
        for value in (None, [], "true"):
            coordinator.data["synthetic"]["state"] = value
            self.assertIsNone(entity.is_on)

    async def test_setup_requires_complete_bound_surface(self):
        valid = record()
        cases = [None, {}, {"capabilities": ["siren"]}]
        cases.extend(
            record(actions=actions) for actions in ((), ("trigger",), ("stop",))
        )
        for malformed in (False, None, 1):
            candidate = copy.deepcopy(valid)
            candidate["decodedProperties"]["bound"] = malformed
            cases.append(candidate)
        candidate = copy.deepcopy(valid)
        candidate["decodedProperties"]["details"][0]["accessor"] = "camera"
        cases.append(candidate)
        for candidate in cases:
            with self.subTest(candidate=candidate):
                self.assertFalse(has_siren_actions(candidate))
        coordinator = Mock(data={"unsupported": record(actions=())})
        entry = SimpleNamespace(runtime_data=SimpleNamespace(coordinator=coordinator))
        added = []
        await async_setup_entry(Mock(), entry, added.extend)
        self.assertEqual(added, [])
        coordinator.data = {"supported": record()}
        await async_setup_entry(Mock(), entry, added.extend)
        self.assertEqual([entity.unique_id for entity in added], ["supported_siren"])

    async def test_described_and_undescribed_methods_are_accepted(self):
        self.assertTrue(has_siren_actions(record()))
        self.assertTrue(
            has_siren_actions(record(actions=(), undescribed=("trigger", "stop")))
        )
        self.assertTrue(
            has_siren_actions(record(actions=("trigger",), undescribed=("stop",)))
        )
        self.assertTrue(has_siren_actions(record(codec="station")))
        self.assertTrue(
            has_siren_actions(record(codec="sensor", actions=("test", "stop")))
        )
        self.assertFalse(has_siren_actions(record(codec="sensor")))
        self.assertFalse(has_siren_actions(record(actions=("test", "stop"))))

    async def test_homebase_duration_and_stop_commands_keep_unknown_state(self):
        entity, _coordinator, client = make_entity(record(codec="station"))
        self.assertTrue(entity.available)
        self.assertIsNone(entity.is_on)
        self.assertTrue(entity.supported_features & SirenEntityFeature.DURATION)
        await entity.async_turn_on()
        client.action.assert_awaited_with("synthetic", "trigger", DEFAULT_DURATION_SECS)
        self.assertIsNone(entity.is_on)
        await entity.async_turn_on(duration=5)
        client.action.assert_awaited_with("synthetic", "trigger", 5)
        self.assertIsNone(entity.is_on)
        await entity.async_turn_off()
        client.action.assert_awaited_with("synthetic", "stop")
        self.assertIsNone(entity.is_on)
        entity.async_write_ha_state.assert_not_called()

    async def test_camera_ack_does_not_infer_state_or_arm_timeout(self):
        entity, _coordinator, client = make_entity(record())
        await entity.async_turn_on(duration=1)
        client.action.assert_awaited_with("synthetic", "trigger", 1)
        self.assertIsNone(entity.is_on)
        await entity.async_turn_off()
        self.assertIsNone(entity.is_on)
        self.assertFalse(hasattr(entity, "_expiry_unsub"))
        entity.async_write_ha_state.assert_not_called()

    async def test_reported_state_survives_command_acknowledgements(self):
        for codec, actions in (
            ("camera", ("trigger", "stop")),
            ("sensor", ("test", "stop")),
        ):
            for value in (False, True):
                with self.subTest(codec=codec, value=value):
                    device = record(codec=codec, actions=actions)
                    device["state"]["siren"] = value
                    entity, _coordinator, _client = make_entity(device)
                    await entity.async_turn_on(duration=1)
                    self.assertIs(entity.is_on, value)
                    await entity.async_turn_off()
                    self.assertIs(entity.is_on, value)

    async def test_standalone_does_not_gain_duration_or_timed_state(self):
        entity, _coordinator, client = make_entity(
            record(codec="sensor", actions=("test", "stop"))
        )
        self.assertFalse(entity.supported_features & SirenEntityFeature.DURATION)
        await entity.async_turn_on(duration=5)
        client.action.assert_awaited_with("synthetic", "test")
        self.assertIsNone(entity.is_on)
        await entity.async_turn_off()
        client.action.assert_awaited_with("synthetic", "stop")
        self.assertIsNone(entity.is_on)

    async def test_withdrawn_methods_disable_existing_entity_and_direct_calls(self):
        entity, coordinator, client = make_entity(record())
        self.assertTrue(entity.available)
        coordinator.data["synthetic"] = record(actions=())
        self.assertFalse(entity.available)
        with self.assertRaises(HomeAssistantError):
            await entity.async_turn_on(duration=1)
        with self.assertRaises(HomeAssistantError):
            await entity.async_turn_off()
        client.action.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
