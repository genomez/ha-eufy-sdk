# ruff: noqa: ANN201, D100, D101, D102, INP001, PT009

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from custom_components.eufy_sdk import camera as camera_platform
from custom_components.eufy_sdk.camera import EufySdkCamera, EufySdkS1Camera
from custom_components.eufy_sdk.native_webrtc import NativeWebRTCMixin
from custom_components.eufy_sdk.preload import on_battery_power

BATTERY = "SYNTHETIC_BATTERY"
MAINS = "SYNTHETIC_WIRED"
LEGACY_BATTERY = "SYNTHETIC_LEGACY_BATTERY"
LEGACY_MAINS = "SYNTHETIC_LEGACY_WIRED"


def _camera(sn: str) -> EufySdkCamera:
    coordinator = Mock()
    coordinator.data = {
        BATTERY: {
            "capabilities": ["camera", "battery"],
            "state": {"battery": 80},
            "power": "battery",
            "stream": "/s",
        },
        # A mains-only model that still reports the battery capability and a level.
        MAINS: {
            "capabilities": ["camera", "battery"],
            "state": {"battery": 0},
            "power": "wired",
            "stream": "/s",
        },
        # A bridge without the `power` field.
        LEGACY_BATTERY: {
            "capabilities": ["camera", "battery"],
            "state": {"battery": 80},
            "stream": "/s",
        },
        LEGACY_MAINS: {"capabilities": ["camera", "battery"], "stream": "/s"},
    }
    return EufySdkCamera(coordinator, sn, "synthetic-host", 3000)


class BatteryCameraStreamTests(unittest.IsolatedAsyncioTestCase):
    async def _starts_worker(self, sn: str) -> bool:
        with patch.object(Camera, "async_create_stream", AsyncMock()) as base:
            await _camera(sn).async_create_stream()
        return base.called

    async def test_a_battery_camera_never_starts_the_ha_stream_worker(self):
        # ha-eufy-sdk#83: the worker retries forever and each retry wakes the camera.
        with patch.object(Camera, "async_create_stream", AsyncMock()) as base:
            self.assertIsNone(await _camera(BATTERY).async_create_stream())
        base.assert_not_called()

    async def test_a_mains_camera_keeps_the_stream_worker(self):
        cam = _camera(MAINS)
        with patch.object(Camera, "async_create_stream", AsyncMock(return_value="s")):
            self.assertEqual(await cam.async_create_stream(), "s")

    async def test_without_the_power_field_the_capability_and_level_decide(self):
        self.assertFalse(await self._starts_worker(LEGACY_BATTERY))
        self.assertTrue(await self._starts_worker(LEGACY_MAINS))

    async def test_every_camera_keeps_the_rtsp_restream(self):
        for sn in (BATTERY, MAINS):
            self.assertEqual(
                await _camera(sn).stream_source(), f"rtsp://synthetic-host:8554/{sn}"
            )
            self.assertEqual(_camera(sn).supported_features, CameraEntityFeature.STREAM)

    async def test_explicit_battery_without_level_never_starts_worker(self):
        cam = _camera(BATTERY)
        cam.coordinator.data[BATTERY] = {"power": "battery"}
        with patch.object(Camera, "async_create_stream", AsyncMock()) as base:
            self.assertIsNone(await cam.async_create_stream())
        base.assert_not_called()

    async def test_legacy_zero_level_never_starts_worker(self):
        cam = _camera(LEGACY_BATTERY)
        cam.coordinator.data[LEGACY_BATTERY]["state"]["battery"] = 0
        with patch.object(Camera, "async_create_stream", AsyncMock()) as base:
            self.assertIsNone(await cam.async_create_stream())
        base.assert_not_called()

    async def test_unset_or_missing_record_delegates_without_crashing(self):
        cam = _camera(BATTERY)
        for data in (None, {}):
            cam.coordinator.data = data
            with patch.object(Camera, "async_create_stream", AsyncMock(return_value="stream")) as base:
                self.assertEqual(await cam.async_create_stream(), "stream")
            base.assert_awaited_once_with()

    async def test_s1_selection_retains_native_webrtc_and_battery_protection(self):
        for relay, expected in ((True, EufySdkS1Camera), (False, EufySdkCamera)):
            with self.subTest(relay=relay):
                coord = _camera(BATTERY).coordinator
                coord.data[BATTERY]["stationSn"] = "SYNTHETIC_STATION"
                coord.data["SYNTHETIC_STATION"] = {"model": "T9000"}
                coord.device_serial_counts = {BATTERY: 1, "SYNTHETIC_STATION": 1}
                entry = SimpleNamespace(
                    data={"host": "synthetic-host", "port": 3000},
                    runtime_data=SimpleNamespace(
                        coordinator=coord,
                        client=Mock(webrtc_relay_enabled=relay),
                    ),
                    async_on_unload=Mock(),
                )
                entities = []
                await camera_platform.async_setup_entry(Mock(), entry, lambda items: entities.extend(items))
                self.assertEqual(len(entities), 4)
                cam = next(entity for entity in entities if entity._sn == BATTERY)
                self.assertIs(type(cam), expected)
                self.assertEqual(cam._supports_native_async_webrtc, relay)
                if relay:
                    self.assertIsInstance(cam, NativeWebRTCMixin)
                with patch.object(Camera, "async_create_stream", AsyncMock()) as base:
                    self.assertIsNone(await cam.async_create_stream())
                base.assert_not_called()

    async def test_preload_check_runs_after_superclass_registration(self):
        cam = _camera(BATTERY)
        cam.hass = Mock()
        cam.entity_id = "camera.synthetic"
        order = []

        async def added():
            order.append("registered")

        async def checked(*args):
            self.assertEqual(order, ["registered"])
            self.assertEqual(args, (cam.hass, cam.entity_id, BATTERY, cam.coordinator.data[BATTERY]))

        with (
            patch.object(CoordinatorEntity, "async_added_to_hass", side_effect=added) as base,
            patch.object(camera_platform, "async_check_preload", side_effect=checked) as check,
        ):
            await cam.async_added_to_hass()
        base.assert_awaited_once_with()
        check.assert_awaited_once()


class BatteryPredicateTests(unittest.TestCase):
    def test_metadata_precedence_and_legacy_presence(self):
        cases = (
            ({"power": "battery"}, True),
            ({"power": "wired", "capabilities": ["battery"], "state": {"battery": 0}}, False),
            ({"capabilities": ["battery"], "state": {"battery": 80}}, True),
            ({"capabilities": ["battery"], "state": {"battery": 0}}, True),
            ({"capabilities": ["battery"], "state": {"battery": None}}, False),
            ({"capabilities": ["battery"]}, False),
            ({"state": {"battery": 80}}, False),
            ({"power": "unknown", "capabilities": ["battery"], "state": {"battery": 0}}, True),
            ({"capabilities": ["battery"], "state": {"battery": 50, "powerSource": "solar"}}, True),
            (None, False),
        )
        for record, expected in cases:
            with self.subTest(record=record):
                self.assertIs(on_battery_power(record), expected)


if __name__ == "__main__":
    unittest.main()
