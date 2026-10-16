# ruff: noqa: ANN201, D100, D101, D102, INP001, PT009, PT027, SLF001

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.eufy_sdk.alarm_sync import apply_alarm_event, clear_alarm
from custom_components.eufy_sdk.api import (
    EufySdkApiClient,
    EufySdkApiClientCommunicationError,
)
from custom_components.eufy_sdk.arming_sync import apply_arming_mode_event
from custom_components.eufy_sdk.coordinator import EufySdkDataUpdateCoordinator
from custom_components.eufy_sdk.property_sync import apply_property_event
from custom_components.eufy_sdk.schedule_logic import current_mode_for
from custom_components.eufy_sdk.sensor import EufySdkPropertySensor


def coordinator_for(client: object) -> EufySdkDataUpdateCoordinator:
    """Build a coordinator with only offline dependencies."""
    coordinator = object.__new__(EufySdkDataUpdateCoordinator)
    coordinator.config_entry = SimpleNamespace(
        runtime_data=SimpleNamespace(client=client)
    )
    coordinator.hass = Mock()
    coordinator.data = {
        "station": {"state": {"armingMode": 1, "currentMode": 1, "light": False}}
    }
    coordinator.async_update_listeners = Mock()
    return coordinator


class TypedSensorTests(unittest.TestCase):
    def value(self, spec: dict, value: object):
        entity = SimpleNamespace(_spec=spec, prop_value=value)
        return EufySdkPropertySensor.native_value.fget(entity)

    def test_numeric_property_rejects_structured_strings_and_nonfinite_values(self):
        for value in (
            '{"synthetic": true}',
            "12",
            {},
            [],
            True,
            float("nan"),
            float("inf"),
        ):
            self.assertIsNone(self.value({"type": "number"}, value))
        self.assertEqual(self.value({"type": "number"}, 0), 0)
        self.assertEqual(self.value({"type": "number"}, 1.5), 1.5)

    def test_declared_strings_booleans_and_known_enums_survive(self):
        self.assertEqual(self.value({"type": "string"}, "4.2.4.2"), "4.2.4.2")
        self.assertEqual(self.value({"type": "string"}, ""), "")
        false_value = False
        self.assertIs(self.value({"type": "bool"}, false_value), false_value)
        self.assertEqual(
            self.value({"type": "number", "enumValues": {"0": "off"}}, 0), "off"
        )
        # Unlabelled codes stay visible as their raw value.
        self.assertEqual(
            self.value({"type": "number", "enumValues": {"0": "off"}}, 9), 9
        )
        self.assertIsNone(self.value({"type": "string", "raw": True}, "synthetic-wire"))

    def test_geofence_without_report_is_unknown(self):
        self.assertEqual(current_mode_for({"armingMode": 47}, None), (None, "unknown"))


class SnapshotOrderTests(unittest.IsolatedAsyncioTestCase):
    async def test_events_during_snapshot_survive_even_when_value_matches_old_state(
        self,
    ):
        client = Mock(connected=True)
        client.auth_status = AsyncMock(return_value={"state": "ok"})
        client.list_solix_devices = AsyncMock(return_value=[])
        coordinator = coordinator_for(client)

        async def list_devices() -> list[dict]:
            apply_arming_mode_event(
                coordinator,
                {
                    "event": "armingModeChanged",
                    "deviceSn": "station",
                    "arming": 2,
                    "mode": 3,
                },
            )
            apply_property_event(
                coordinator,
                {
                    "event": "propertyChanged",
                    "deviceSn": "station",
                    "property": "light",
                    "value": False,
                },
            )
            apply_property_event(
                coordinator,
                {
                    "event": "propertyChanged",
                    "deviceSn": "station",
                    "property": "cameraInfo",
                    "value": None,
                },
            )
            return [
                {
                    "sn": "station",
                    "state": {
                        "armingMode": 1,
                        "currentMode": 1,
                        "light": True,
                        "cameraInfo": 9,
                    },
                }
            ]

        client.list_devices = AsyncMock(side_effect=list_devices)
        result = await coordinator._async_update_data()
        self.assertEqual(
            result["station"]["state"],
            {"armingMode": 2, "currentMode": 3, "light": False, "cameraInfo": None},
        )
        self.assertIsNone(coordinator._pending_state_events)
        client.list_devices = AsyncMock(
            return_value=[{"sn": "station", "state": {"armingMode": 0}}]
        )
        self.assertEqual(
            (await coordinator._async_update_data())["station"]["state"],
            {"armingMode": 0},
        )

    async def test_failure_drops_journal_and_schedules_only_one_quick_retry(self):
        client = Mock(connected=True)
        client.auth_status = AsyncMock(return_value={"state": "pending"})
        coordinator = coordinator_for(client)
        cancel = Mock()
        with patch(
            "custom_components.eufy_sdk.coordinator.async_call_later",
            return_value=cancel,
        ) as later:
            for _ in range(2):
                with self.assertRaises(UpdateFailed):
                    await coordinator._async_update_data()
            later.assert_called_once()
            self.assertIsNone(coordinator._pending_state_events)
            coordinator._clear_fast_retry()
            cancel.assert_called_once()
            with self.assertRaises(UpdateFailed):
                await coordinator._async_update_data()
            later.assert_called_once()

    async def test_alarm_lifecycle_survives_omitted_snapshot_until_stop_or_clear(self):
        client = Mock(connected=True)
        client.auth_status = AsyncMock(return_value={"state": "ok"})
        client.list_devices = AsyncMock(
            side_effect=lambda: [{"sn": "station", "state": {"armingMode": 1}}]
        )
        client.list_solix_devices = AsyncMock(return_value=[])
        coordinator = coordinator_for(client)
        apply_alarm_event(
            coordinator, {"event": "alarm", "deviceSn": "station", "type": 4}
        )
        coordinator.data = await coordinator._async_update_data()
        self.assertTrue(coordinator.data["station"]["state"]["alarmTriggered"])
        apply_alarm_event(
            coordinator, {"event": "alarm", "deviceSn": "station", "type": 16}
        )
        coordinator.data = await coordinator._async_update_data()
        self.assertFalse(coordinator.data["station"]["state"]["alarmTriggered"])
        apply_alarm_event(
            coordinator, {"event": "alarm", "deviceSn": "station", "type": 4}
        )
        clear_alarm(coordinator, "station")
        coordinator.data = await coordinator._async_update_data()
        self.assertFalse(coordinator.data["station"]["state"]["alarmTriggered"])
        apply_alarm_event(
            coordinator, {"event": "alarm", "deviceSn": "station", "type": 4}
        )
        client.list_devices = AsyncMock(
            return_value=[{"sn": "station", "state": {"alarmTriggered": False}}]
        )
        self.assertFalse(
            (await coordinator._async_update_data())["station"]["state"][
                "alarmTriggered"
            ]
        )
        client.list_devices = AsyncMock(return_value=[])
        self.assertEqual(await coordinator._async_update_data(), {})

    async def test_retry_fires_once_and_failed_retry_does_not_rearm(self):
        client = Mock(connected=True)
        client.auth_status = AsyncMock(return_value={"state": "pending"})
        coordinator = coordinator_for(client)
        coordinator.async_request_refresh = AsyncMock()
        tasks = []
        coordinator.hass.async_create_task.side_effect = lambda coro: tasks.append(
            asyncio.create_task(coro)
        )
        with patch("custom_components.eufy_sdk.coordinator.async_call_later") as later:
            coordinator._schedule_fast_retry()
            coordinator._fast_retry_fire(None)
            await asyncio.gather(*tasks)
            coordinator.async_request_refresh.assert_awaited_once()
            with self.assertRaises(UpdateFailed):
                await coordinator._async_update_data()
            later.assert_called_once()

    async def test_auth_challenge_cancels_retry_without_reset_or_login(self):
        client = Mock(connected=True)
        client.auth_status = AsyncMock(return_value={"state": "require_2fa"})
        coordinator = coordinator_for(client)
        cancel = coordinator._fast_retry_cancel = Mock()
        with self.assertRaises(ConfigEntryAuthFailed):
            await coordinator._async_update_data()
        cancel.assert_called_once()
        client.reset_connection.assert_not_called()
        client.retrigger_auth.assert_not_called()

    async def test_good_poll_cancels_timer_and_reopens_one_retry_budget(self):
        client = Mock(connected=True)
        client.auth_status = AsyncMock(return_value={"state": "ok"})
        client.list_devices = AsyncMock(return_value=[])
        client.list_solix_devices = AsyncMock(return_value=[])
        coordinator = coordinator_for(client)
        cancel = coordinator._fast_retry_cancel = Mock()
        coordinator._fast_retry_used = True
        await coordinator._async_update_data()
        cancel.assert_called_once()
        self.assertFalse(coordinator._fast_retry_used)

    async def test_transport_failure_resets_socket_with_fixed_error(self):
        client = Mock(connected=True)
        client.auth_status = AsyncMock(
            side_effect=EufySdkApiClientCommunicationError("synthetic-private")
        )
        client.reset_connection = AsyncMock()
        coordinator = coordinator_for(client)
        with (
            patch("custom_components.eufy_sdk.coordinator.async_call_later"),
            self.assertRaisesRegex(UpdateFailed, "^bridge_update_failed$"),
        ):
            await coordinator._async_update_data()
        client.reset_connection.assert_awaited_once()
        client.retrigger_auth.assert_not_called()


class SocketResetTests(unittest.IsolatedAsyncioTestCase):
    async def test_reset_retires_reader_and_pending_before_reconnect(self):
        client = EufySdkApiClient("synthetic.invalid", 3000, Mock())
        ws = Mock(closed=False, close=AsyncMock())
        client._ws = ws
        pending = asyncio.get_running_loop().create_future()
        client._pending[1] = pending
        finished = asyncio.Event()

        async def reader() -> None:
            try:
                await asyncio.Future()
            finally:
                finished.set()

        client._recv_task = asyncio.create_task(reader())
        await asyncio.sleep(0)
        client._schedule_reconnect = Mock()
        await client.reset_connection()
        self.assertTrue(finished.is_set())
        self.assertFalse(client.connected)
        self.assertEqual(client._pending, {})
        with self.assertRaises(EufySdkApiClientCommunicationError):
            await pending
        ws.close.assert_awaited_once()
        client._schedule_reconnect.assert_called_once()

    async def test_cancelled_rpc_removes_pending_without_replaying(self):
        client = EufySdkApiClient("synthetic.invalid", 3000, Mock())
        client._ws = Mock(closed=False, send_json=AsyncMock())
        task = asyncio.create_task(client.rpc("devices.list"))
        await asyncio.sleep(0)
        self.assertEqual(len(client._pending), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(client._pending, {})
        client._ws.send_json.assert_awaited_once()
