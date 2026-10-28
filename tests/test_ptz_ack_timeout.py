"""PTZ acknowledgement failures explain uncertain movement without retrying."""

# ruff: noqa: ANN201, D101, D102, INP001, PT009, PT027

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from homeassistant.exceptions import HomeAssistantError

from custom_components.eufy_sdk.api import (
    COMMAND_ERROR_CODES,
    EufySdkApiClientCommunicationError,
    EufySdkApiClientError,
)
from custom_components.eufy_sdk.button import EufySdkPtzButton
from custom_components.eufy_sdk.const import DOMAIN


class PtzAckTimeoutTests(unittest.IsolatedAsyncioTestCase):
    def button(self, action: str = "left"):
        client = SimpleNamespace(action=AsyncMock())
        coordinator = SimpleNamespace(
            data={"synthetic-camera": {}},
            config_entry=SimpleNamespace(runtime_data=SimpleNamespace(client=client)),
        )
        return EufySdkPtzButton(
            coordinator, "synthetic-camera", action, {"key": "pan_left", "icon": ""}
        ), client

    async def test_ack_timeout_is_translated_for_every_direction_without_retry(self):
        for action in ("left", "right", "up", "down"):
            with self.subTest(action=action):
                button, client = self.button(action)
                error = EufySdkApiClientError("command_ack_timeout")
                client.action.side_effect = error
                with self.assertRaises(HomeAssistantError) as caught:
                    await button.async_press()
                self.assertIs(type(caught.exception), HomeAssistantError)
                self.assertEqual(caught.exception.translation_domain, DOMAIN)
                self.assertEqual(caught.exception.translation_key, "ptz_ack_timeout")
                self.assertIs(caught.exception.__cause__, error)
                client.action.assert_awaited_once_with("synthetic-camera", action)

    async def test_other_errors_propagate_unchanged(self):
        errors = [
            EufySdkApiClientError(code)
            for code in sorted(COMMAND_ERROR_CODES - {"command_ack_timeout"})
        ]
        errors.append(EufySdkApiClientCommunicationError("bridge_request_timeout"))
        for error in errors:
            with self.subTest(code=str(error)):
                button, client = self.button()
                client.action.side_effect = error
                with self.assertRaises(type(error)) as caught:
                    await button.async_press()
                self.assertIs(caught.exception, error)
                client.action.assert_awaited_once_with("synthetic-camera", "left")

    async def test_success_sends_once(self):
        button, client = self.button()
        await button.async_press()
        client.action.assert_awaited_once_with("synthetic-camera", "left")

    def test_english_error_explains_possible_movement(self):
        root = Path(__file__).resolve().parents[1] / "custom_components/eufy_sdk"
        for path in (root / "strings.json", root / "translations/en.json"):
            with self.subTest(file=path.name):
                strings = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(
                    strings["exceptions"]["ptz_ack_timeout"]["message"],
                    "Pan sent, but the camera didn't confirm in time; "
                    "it may still have moved.",
                )
