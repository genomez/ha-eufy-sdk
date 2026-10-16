"""A preloaded battery camera raises a fixable repair; the fix turns preload off."""

# ruff: noqa: INP001, S101

from types import SimpleNamespace
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.components.camera.const import DATA_CAMERA_PREFS
from homeassistant.components.camera.prefs import CameraPreferences
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import device_registry as dr

from custom_components.eufy_sdk import preload, repairs

BATTERY_CAM = {
    "name": "Front door",
    "capabilities": ["battery"],
    "state": {"battery": 80},
}
# A mains model can carry the capability but reports no battery level.
MAINS_CAM = {"name": "Garden", "capabilities": ["battery"], "state": {}}


class PreloadCheckTests(IsolatedAsyncioTestCase):
    """Raise or clear the repair issue from the camera's preload setting."""

    async def _check(self, record: dict, *, preload_on: bool) -> tuple[Mock, Mock]:
        settings = SimpleNamespace(preload_stream=preload_on)
        with (
            patch.object(
                preload,
                "get_dynamic_camera_stream_settings",
                AsyncMock(return_value=settings),
            ),
            patch.object(preload.ir, "async_create_issue") as create,
            patch.object(preload.ir, "async_delete_issue") as delete,
        ):
            await preload.async_check_preload(Mock(), "camera.front", "SYNTHETIC_CAMERA", record)
        return create, delete

    async def test_a_preloaded_battery_camera_raises_a_fixable_issue(self) -> None:
        """The issue names the camera and carries the entity for the fix flow."""
        create, delete = await self._check(BATTERY_CAM, preload_on=True)
        create.assert_called_once()
        kwargs = create.call_args.kwargs
        assert create.call_args.args[2] == "preload_battery_camera_SYNTHETIC_CAMERA"
        assert kwargs["is_fixable"] is True
        assert kwargs["is_persistent"] is False
        assert kwargs["severity"] == ir.IssueSeverity.WARNING
        assert kwargs["translation_key"] == "preload_battery_camera"
        assert kwargs["data"] == {"entity_id": "camera.front", "camera": "Front door"}
        delete.assert_not_called()

    async def test_preload_off_clears_the_issue(self) -> None:
        """Turning preload off (e.g. by hand) removes a stale issue on next start."""
        create, delete = await self._check(BATTERY_CAM, preload_on=False)
        create.assert_not_called()
        delete.assert_called_once()

    async def test_a_mains_camera_is_never_flagged(self) -> None:
        """Preloading a mains camera costs no battery, so there is no issue."""
        create, _ = await self._check(MAINS_CAM, preload_on=True)
        create.assert_not_called()

    async def test_explicit_wired_zero_level_clears_issue(self) -> None:
        record = {**BATTERY_CAM, "power": "wired", "state": {"battery": 0}}
        create, delete = await self._check(record, preload_on=True)
        create.assert_not_called()
        delete.assert_called_once()

    async def test_explicit_battery_without_level_is_flagged(self) -> None:
        create, delete = await self._check({"power": "battery"}, preload_on=True)
        create.assert_called_once()
        delete.assert_not_called()

    async def test_no_record_clears_issue(self) -> None:
        create, delete = await self._check(None, preload_on=True)
        create.assert_not_called()
        delete.assert_called_once()


class PreloadFixFlowTests(IsolatedAsyncioTestCase):
    """The fix flow turns preload off for the camera the issue names."""

    async def test_confirming_turns_preload_off(self) -> None:
        """Submitting the confirm step updates the camera's preferences."""
        flow = await repairs.async_create_fix_flow(
            Mock(),
            "preload_battery_camera_SYNTHETIC_CAMERA",
            {"entity_id": "camera.front", "camera": "Front door"},
        )
        assert isinstance(flow, repairs.PreloadRepairFlow)
        prefs = Mock(async_update=AsyncMock())
        flow.hass = Mock(data={DATA_CAMERA_PREFS: prefs})
        flow.async_create_entry = Mock(return_value="done")
        assert await flow.async_step_confirm({}) == "done"
        prefs.async_update.assert_awaited_once_with(
            "camera.front", preload_stream=False
        )

    async def test_the_form_names_the_camera(self) -> None:
        """Before confirming, the form shows which camera it will change."""
        flow = repairs.PreloadRepairFlow("camera.front", "Front door")
        flow.async_show_form = Mock(side_effect=lambda **kw: kw)
        result = await flow.async_step_init()
        assert result["description_placeholders"] == {"camera": "Front door"}

    async def test_opening_or_abandoning_form_does_not_change_preferences(self) -> None:
        flow = repairs.PreloadRepairFlow("camera.front", "Front door")
        prefs = Mock(async_update=AsyncMock())
        flow.hass = Mock(data={DATA_CAMERA_PREFS: prefs})
        flow.async_show_form = Mock(return_value={"type": "form"})
        await flow.async_step_init()
        await flow.async_step_confirm()
        prefs.async_update.assert_not_awaited()

    async def test_unrelated_issue_uses_generic_confirmation(self) -> None:
        flow = await repairs.async_create_fix_flow(Mock(), "unrelated_issue", None)
        assert isinstance(flow, repairs.ConfirmRepairFlow)


class NativePreferencesTests(IsolatedAsyncioTestCase):
    """Exercise actual offline HA preferences and issue APIs with memory storage."""

    async def test_check_is_read_only_and_confirm_updates_real_preferences(self) -> None:
        with TemporaryDirectory() as config_dir:
            hass = HomeAssistant(config_dir)
            dr.async_setup(hass)
            await dr.async_load(hass, load_empty=True)
            await er.async_load(hass, load_empty=True)
            await ir.async_load(hass, load_empty=True)
            prefs = CameraPreferences(hass)
            prefs._preload_prefs = {"camera.front": {"preload_stream": True}}
            hass.data[DATA_CAMERA_PREFS] = prefs
            registry = ir.async_get(hass)
            with (
                patch.object(prefs._store, "async_save", new_callable=AsyncMock) as save,
                patch.object(ir, "async_get", return_value=registry),
            ):
                await preload.async_check_preload(hass, "camera.front", "SYNTHETIC_CAMERA", BATTERY_CAM)
                issue_id = "preload_battery_camera_SYNTHETIC_CAMERA"
                issue = registry.async_get_issue("eufy_sdk", issue_id)
                assert issue is not None and issue.is_fixable
                assert (await prefs.get_dynamic_stream_settings("camera.front")).preload_stream
                save.assert_not_awaited()
                flow = await repairs.async_create_fix_flow(hass, issue_id, issue.data)
                flow.hass = hass
                result = await flow.async_step_init()
                assert result["type"] == "form"
                save.assert_not_awaited()
                result = await flow.async_step_confirm({})
                assert result["type"] == "create_entry"
                assert not (await prefs.get_dynamic_stream_settings("camera.front")).preload_stream
                save.assert_awaited_once()
                await preload.async_check_preload(hass, "camera.front", "SYNTHETIC_CAMERA", BATTERY_CAM)
                assert registry.async_get_issue("eufy_sdk", issue_id) is None
