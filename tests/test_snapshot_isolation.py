# ruff: noqa: D100, D102, INP001, PT009

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from custom_components.eufy_sdk.data import EufySdkData
from custom_components.eufy_sdk.select import EufySdkSnapshotPolicySelect
from test_snapshot_policy import _CameraWithHass, _coordinator, _SnapshotSession


class SnapshotIsolationTests(unittest.IsolatedAsyncioTestCase):
    """Exercise entry ownership, restore, and negative I/O guarantees."""

    async def test_actual_runtime_defaults_are_independent_per_entry(self) -> None:
        first = EufySdkData(Mock(), Mock(), Mock())
        second = EufySdkData(Mock(), Mock(), Mock())
        first.snapshot_policy["camera-a"] = "stored"
        self.assertEqual(second.snapshot_policy, {})
        self.assertIsNot(first.snapshot_policy, second.snapshot_policy)

    async def test_same_serial_in_two_entries_uses_own_policy(self) -> None:
        first = _coordinator({"camera-a": {"stream": "camera-a"}})
        second = _coordinator({"camera-a": {"stream": "camera-a"}})
        for coordinator, option in ((first, "stored"), (second, "live")):
            entity = EufySdkSnapshotPolicySelect(coordinator, "camera-a")
            entity.async_write_ha_state = Mock()
            await entity.async_select_option(option)
        session = _SnapshotSession()
        with patch(
            "custom_components.eufy_sdk.camera.async_get_clientsession",
            return_value=session,
        ):
            camera_a = _CameraWithHass(first, "camera-a", "bridge-a", 3000)
            await camera_a.async_camera_image()
            camera_b = _CameraWithHass(second, "camera-a", "bridge-b", 3001)
            await camera_b.async_camera_image()
        self.assertEqual(
            session.urls,
            [
                "http://bridge-a:3000/snapshot/camera-a?mode=stored",
                "http://bridge-b:3001/snapshot/camera-a?mode=live",
            ],
        )

    async def test_new_runtime_restores_choice_without_device_or_http_io(self) -> None:
        old = _coordinator({"camera-a": {"stream": "camera-a"}})
        chosen = EufySdkSnapshotPolicySelect(old, "camera-a")
        chosen.async_write_ha_state = Mock()
        await chosen.async_select_option("stored")
        restarted = _coordinator({"camera-a": {"stream": "camera-a"}})
        restored = EufySdkSnapshotPolicySelect(restarted, "camera-a")
        restored.async_get_last_state = AsyncMock(
            return_value=SimpleNamespace(state=chosen.current_option)
        )
        restored.async_on_remove = Mock()
        restarted.async_add_listener = Mock(return_value=lambda: None)
        with patch(
            "custom_components.eufy_sdk.camera.async_get_clientsession",
            side_effect=AssertionError("restore must not request a snapshot"),
        ):
            await restored.async_added_to_hass()
        self.assertEqual(restored.current_option, "stored")
        self.assertEqual(
            restarted.config_entry.runtime_data.snapshot_policy, {"camera-a": "stored"}
        )
        self.assertEqual(restarted.config_entry.runtime_data.client.mock_calls, [])
        self.assertEqual(old.config_entry.runtime_data.client.mock_calls, [])

    async def test_every_selection_is_local_including_invalid_option(self) -> None:
        coordinator = _coordinator({"camera-a": {"stream": "camera-a"}})
        selector = EufySdkSnapshotPolicySelect(coordinator, "camera-a")
        selector.async_write_ha_state = Mock()
        with patch(
            "custom_components.eufy_sdk.camera.async_get_clientsession",
            side_effect=AssertionError("selection must not capture"),
        ):
            for option in ("auto", "stored", "live", "default", "invalid"):
                await selector.async_select_option(option)
        self.assertEqual(selector.current_option, "default")
        self.assertEqual(coordinator.config_entry.runtime_data.client.mock_calls, [])

    async def test_camera_identity_and_availability_are_preserved(self) -> None:
        coordinator = _coordinator({"camera-a": {"stream": "camera-a"}})
        coordinator.last_update_success = True
        camera = _CameraWithHass(coordinator, "camera-a", "bridge", 3000)
        self.assertEqual(camera.unique_id, "camera-a_camera")
        self.assertEqual(camera.device_info["identifiers"], {("eufy_sdk", "camera-a")})
        self.assertEqual(await camera.stream_source(), "rtsp://bridge:8554/camera-a")
        self.assertTrue(camera.available)
        del coordinator.data["camera-a"]
        self.assertFalse(camera.available)

    async def test_camera_error_responses_keep_existing_none_result(self) -> None:
        coordinator = _coordinator({"camera-a": {"stream": "camera-a"}})
        coordinator.config_entry.runtime_data.snapshot_policy["camera-a"] = "stored"
        camera = _CameraWithHass(coordinator, "camera-a", "bridge", 3000)
        for error in (TimeoutError(), OSError()):
            session = Mock()
            session.get.side_effect = error
            with patch(
                "custom_components.eufy_sdk.camera.async_get_clientsession",
                return_value=session,
            ):
                self.assertIsNone(await camera.async_camera_image())
            session.get.assert_called_once_with(
                "http://bridge:3000/snapshot/camera-a?mode=stored", timeout=20
            )
