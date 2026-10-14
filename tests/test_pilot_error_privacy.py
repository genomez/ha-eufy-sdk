# ruff: noqa: ANN201, D100, D101, D102, INP001, PT009, PT027, SLF001

import json
import traceback
import unittest
from typing import Any
from unittest.mock import AsyncMock, Mock

from homeassistant.exceptions import HomeAssistantError

from custom_components.eufy_sdk.api import (
    EufySdkApiClient,
    EufySdkApiClientCommunicationError,
    EufySdkApiClientError,
)
from custom_components.eufy_sdk.diagnostics import async_get_config_entry_diagnostics


class PilotErrorPrivacyTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_error_codes_remain_expected_ha_errors_without_raw_text(self):
        for code, expected in [
            ("command_connect_timeout", "command_connect_timeout"),
            ("command_expired_after_send", "command_expired_after_send"),
            ("synthetic-private-marker", "bridge_request_rejected"),
            ({"unexpected": "synthetic-private-marker"}, "bridge_request_rejected"),
        ]:
            client = EufySdkApiClient("bridge", 3000, Mock())
            client._ws = Mock(closed=False)

            async def reply(
                payload: dict[str, Any],
                code: Any = code,
                client: EufySdkApiClient = client,
            ) -> None:
                client._pending[payload["id"]].set_result(
                    {"ok": False, "code": code, "error": "synthetic-private-marker"}
                )

            client._ws.send_json = AsyncMock(side_effect=reply)
            with self.assertRaises(HomeAssistantError) as caught:
                await client.rpc("device.set", sn="synthetic-device", value=False)
            self.assertIsInstance(caught.exception, EufySdkApiClientError)
            self.assertEqual(str(caught.exception), expected)
            client._ws.send_json.assert_awaited_once()
            self.assertEqual(client._pending, {})

    async def test_connect_failure_suppresses_endpoint_and_raw_cause(self):
        session = Mock()
        session.ws_connect = AsyncMock(side_effect=OSError("synthetic-private-marker"))
        client = EufySdkApiClient("synthetic-endpoint", 3000, session)
        try:
            await client.connect()
        except EufySdkApiClientCommunicationError as error:
            self.assertEqual(str(error), "bridge_unreachable")
            output = "".join(traceback.format_exception(error))
            self.assertNotIn("synthetic-private-marker", output)
            self.assertNotIn("synthetic-endpoint", output)
        else:
            self.fail("connection failure must remain a failure")

    async def test_rejected_rpc_does_not_export_raw_reply(self):
        client = EufySdkApiClient("bridge", 3000, Mock())
        client._ws = Mock(closed=False)

        async def reply(payload: dict[str, Any]) -> None:
            client._pending[payload["id"]].set_result(
                {"ok": False, "error": "synthetic-private-marker"}
            )

        client._ws.send_json = AsyncMock(side_effect=reply)
        with self.assertRaisesRegex(EufySdkApiClientError, "^bridge_request_rejected$"):
            await client.rpc("device.set", sn="synthetic-device", value=False)

    async def test_successful_rpc_keeps_normal_state_values(self):
        client = EufySdkApiClient("bridge", 3000, Mock())
        client._ws = Mock(closed=False)
        expected = {"ok": True, "state": {"enabled": False, "level": 0}}

        async def reply(payload: dict[str, Any]) -> None:
            client._pending[payload["id"]].set_result(expected)

        client._ws.send_json = AsyncMock(side_effect=reply)
        self.assertEqual(await client.rpc("device.read"), expected)

    async def test_send_failure_is_typed_and_removes_pending_request(self):
        client = EufySdkApiClient("bridge", 3000, Mock())
        client._ws = Mock(closed=False)
        client._ws.send_json = AsyncMock(
            side_effect=OSError("synthetic-private-marker")
        )
        with self.assertRaisesRegex(
            EufySdkApiClientCommunicationError, "^bridge_send_failed$"
        ):
            await client.rpc("device.set", sn="synthetic-device", value=False)
        self.assertEqual(client._pending, {})

    async def test_diagnostics_keep_status_and_counts_without_untrusted_text(self):
        private = "synthetic-private-marker"
        client = Mock(connected=True)
        client.auth_status = AsyncMock(
            return_value={"state": private, "schemaVersion": private}
        )
        coordinator = Mock(
            data={
                private: {"state": {"stream_url": private}, "capabilities": [private]}
            },
            solix_devices={},
            last_update_success=False,
        )
        entry = Mock()
        entry.runtime_data = Mock(
            client=client, coordinator=coordinator, properties={private: [{}]}
        )
        entry.as_dict.return_value = {"title": private}
        result = await async_get_config_entry_diagnostics(Mock(), entry)
        self.assertNotIn(private, json.dumps(result))
        self.assertEqual(
            result["connection"],
            {
                "bridge_connected": True,
                "last_update_success": False,
                "auth_state": "unknown",
            },
        )
        self.assertEqual(
            result["summary"],
            {"device_count": 1, "solix_device_count": 0, "property_spec_count": 1},
        )
