"""The RTSP recording select keeps its existing identity and wire behavior."""

# ruff: noqa: ANN201, D101, D102, INP001, PT009

import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from custom_components.eufy_sdk.select import EufySdkSelect


class RecordingModeNameTests(unittest.IsolatedAsyncioTestCase):
    def select(self, name: str = "recordingMode"):
        spec = {
            "name": name,
            "type": "enum",
            "writable": True,
            "enumValues": {"0": "Mode A", "1": "Mode B"},
        }
        original = deepcopy(spec)
        coordinator = SimpleNamespace(data={"synthetic-camera": {"state": {name: 1}}})
        entity = EufySdkSelect(coordinator, "synthetic-camera", spec)
        self.assertEqual(spec, original)
        return entity

    def test_display_name_changes_without_changing_unique_id_or_translation_key(self):
        entity = self.select()
        self.assertEqual(entity.name, "RTSP recording mode")
        self.assertEqual(entity.unique_id, "synthetic-camera_recordingMode")
        self.assertIsNone(entity.translation_key)
        self.assertEqual(entity.options, ["Mode A", "Mode B"])
        self.assertEqual(entity.current_option, "Mode B")

    async def test_selection_keeps_original_raw_values(self):
        entity = self.select()
        with patch.object(entity, "write", new=AsyncMock()) as write:
            await entity.async_select_option("Mode A")
        write.assert_awaited_once_with(0)

    def test_unrelated_select_keeps_generated_name(self):
        entity = self.select("otherMode")
        self.assertEqual(entity.name, "Other Mode")
        self.assertEqual(entity.unique_id, "synthetic-camera_otherMode")
