"""Verified timer choices and preserved duration-number registry identities."""

# ruff: noqa: ANN201, SLF001, D101, D102, INP001, PT009
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from custom_components.eufy_sdk.audit_visibility import hidden_default
from custom_components.eufy_sdk.number import EufySdkNumber
from custom_components.eufy_sdk.select import (
    EufyMotionLightTimerSelect,
    async_setup_entry,
)

SPEC = {
    "name": "motionLightDuration",
    "type": "number",
    "kind": "seconds",
    "writable": True,
    "min": 30,
    "max": 900,
    "step": 1,
}


class TimerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.coordinator = Mock(
            data={
                "camera": {
                    "model": "T8425",
                    "stationSn": "station",
                    "state": {"motionLightDuration": 180},
                },
                "station": {"model": "T9000"},
            },
            solix_devices={},
        )
        self.entity = EufyMotionLightTimerSelect(self.coordinator, "camera", SPEC)
        self.entity.write = AsyncMock()

    async def test_fixed_choices_write_raw_seconds_through_duration_property(self):
        choices = [
            (30, "30 s"),
            (60, "1 min"),
            (180, "3 min"),
            (300, "5 min"),
            (900, "15 min"),
        ]
        self.assertEqual(self.entity.options, [label for _, label in choices])
        self.assertEqual(self.entity.current_option, "3 min")
        self.assertEqual(self.entity.unique_id, "camera_motionLightTimer")
        self.assertEqual(self.entity._prop, "motionLightDuration")
        self.assertTrue(self.entity.entity_registry_visible_default)
        number = EufySdkNumber(self.coordinator, "camera", SPEC)
        self.assertEqual(number.unique_id, "camera_motionLightDuration")
        self.assertFalse(number.entity_registry_visible_default)
        for raw, label in choices:
            self.coordinator.data["camera"]["state"]["motionLightDuration"] = raw
            self.assertEqual(self.entity.current_option, label)
            await self.entity.async_select_option(label)
            self.entity.write.assert_awaited_with(raw)

    async def test_unknown_report_is_unknown_and_never_written(self):
        for raw in (45, 1200, None):
            self.coordinator.data["camera"]["state"]["motionLightDuration"] = raw
            self.assertIsNone(self.entity.current_option)
        await self.entity.async_select_option("45 s")
        self.entity.write.assert_not_awaited()

    async def test_setup_gates_models_parent_evidence_and_writability(self):
        for model, parent, writable, expected in (
            ("T8425", "T9000", True, 1),
            ("T8426", "T9000", True, 1),
            ("T8416", "T9000", True, 0),
            ("T8425", "T8030", True, 0),
            ("T8425", None, True, 0),
            ("T8425", "T9000", False, 0),
        ):
            with self.subTest(model=model, parent=parent, writable=writable):
                self.coordinator.data["camera"]["model"] = model
                self.coordinator.data["station"] = {"model": parent}
                entry = SimpleNamespace(
                    runtime_data=SimpleNamespace(
                        coordinator=self.coordinator,
                        properties={"camera": [{**SPEC, "writable": writable}]},
                    )
                )
                add = Mock()
                await async_setup_entry(Mock(), entry, add)
                timers = [
                    entity
                    for entity in add.call_args.args[0]
                    if isinstance(entity, EufyMotionLightTimerSelect)
                ]
                self.assertEqual(len(timers), expected)
                self.assertEqual(
                    hidden_default(self.coordinator, "camera", "motionLightDuration"),
                    model in {"T8425", "T8426"} and parent == "T9000",
                )
