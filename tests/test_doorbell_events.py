# ruff: noqa: ANN001, ANN201, D100, D101, D102, INP001, PT009, SLF001
# Added 2026-10-17: standard ring and legacy doorbell event regressions.

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from homeassistant.components.event import DoorbellEventType, EventDeviceClass

from custom_components.eufy_sdk.config_flow import EufySdkOptionsFlow
from custom_components.eufy_sdk.const import (
    CONF_LEGACY_DOORBELL_PRESSED,
    CONF_POLL_INTERVAL,
    CONF_SOC_REFRESH,
)
from custom_components.eufy_sdk.event import (
    EufySdkDetectionEvent,
    EufySdkDoorbellEvent,
    async_setup_entry,
)
from custom_components.eufy_sdk.pushmap import DETECTION_EVENTS, DOORBELL_EVENT_TYPE


def make_entity(entity_class=EufySdkDoorbellEvent, **options: bool):
    """Capture each published event type through the native HA event attributes."""
    coordinator = Mock(data={"doorbell": {}})
    entity = entity_class(coordinator, "doorbell", **options)
    writes = []
    entity.async_write_ha_state = Mock(
        side_effect=lambda: writes.append(dict(entity.state_attributes))
    )
    return entity, writes


class DoorbellEventTests(unittest.TestCase):
    def test_doorbell_advertises_only_ring_by_default(self):
        entity, _ = make_entity()
        self.assertEqual(entity.device_class, EventDeviceClass.DOORBELL)
        self.assertEqual(entity.event_types, ["ring"])
        self.assertIn(DoorbellEventType.RING, entity.event_types)
        self.assertEqual(entity.unique_id, "doorbell_doorbell_event")

    def test_press_writes_one_standard_event(self):
        entity, writes = make_entity()
        entity._handle_event(
            Mock(
                data={
                    "deviceSn": "doorbell",
                    "event": "doorbellPress",
                    "thumbnailUrl": "http://bridge.invalid/thumbnail",
                    "other": "ignored",
                }
            )
        )
        self.assertEqual(
            writes,
            [
                {
                    "event_type": "ring",
                    "thumbnailUrl": "http://bridge.invalid/thumbnail",
                },
            ],
        )
        self.assertEqual(entity.state_attributes["event_type"], "ring")
        self.assertIsNotNone(entity.state)

    def test_repeated_presses_without_identity_each_publish_once(self):
        entity, writes = make_entity()
        event = Mock(data={"deviceSn": "doorbell", "event": "doorbellPress"})
        for _ in range(2):
            entity._handle_event(event)
        self.assertEqual(
            [attrs["event_type"] for attrs in writes],
            ["ring", "ring"],
        )

    def test_legacy_option_emits_only_pressed(self):
        entity, writes = make_entity(legacy_pressed=True)
        self.assertEqual(entity.event_types, [DOORBELL_EVENT_TYPE])
        entity._handle_event(
            Mock(data={"deviceSn": "doorbell", "event": "doorbellPress"})
        )
        self.assertEqual(writes, [{"event_type": "pressed"}])

    def test_invalid_identities_do_not_collapse_real_presses(self):
        entity, writes = make_entity()
        for identity in (None, "", " ", 42):
            entity._handle_event(
                Mock(
                    data={
                        "deviceSn": "doorbell",
                        "event": "doorbellPress",
                        "eventId": identity,
                    }
                )
            )
        self.assertEqual(len(writes), 4)

    def test_unrelated_or_missing_events_do_not_fire(self):
        entity, writes = make_entity()
        for data in (
            {"deviceSn": "different", "event": "doorbellPress"},
            {"deviceSn": "doorbell", "event": "motion"},
            {"deviceSn": "doorbell"},
        ):
            entity._handle_event(Mock(data=data))
        self.assertEqual(writes, [])
        self.assertIsNone(entity.state)

    def test_empty_thumbnail_is_omitted(self):
        entity, writes = make_entity()
        entity._handle_event(
            Mock(
                data={
                    "deviceSn": "doorbell",
                    "event": "doorbellPress",
                    "thumbnailUrl": "",
                }
            )
        )
        self.assertEqual(writes, [{"event_type": "ring"}])

    def test_detection_types_and_single_fire_are_unchanged(self):
        entity, writes = make_entity(EufySdkDetectionEvent)
        self.assertEqual(entity.event_types, sorted(set(DETECTION_EVENTS.values())))
        entity._handle_event(Mock(data={"deviceSn": "doorbell", "event": "motion"}))
        entity._handle_event(
            Mock(data={"deviceSn": "doorbell", "event": "doorbellPress"})
        )
        self.assertEqual(writes, [{"event_type": "motion"}])


class DoorbellSetupTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_doorbell_capability_gets_doorbell_class(self):
        coordinator = Mock(
            data={
                "doorbell": {"capabilities": ["doorbell"]},
                "camera": {"capabilities": ["motion"]},
                "other": {"capabilities": []},
            }
        )
        entry = Mock()
        entry.options = {}
        entry.runtime_data.coordinator = coordinator
        add_entities = Mock()
        await async_setup_entry(Mock(), entry, add_entities)
        entities = add_entities.call_args.args[0]
        doorbells = [e for e in entities if isinstance(e, EufySdkDoorbellEvent)]
        self.assertEqual(len(doorbells), 1)
        self.assertEqual(doorbells[0]._sn, "doorbell")
        self.assertIn("ring", doorbells[0].event_types)

    async def test_setup_applies_legacy_option(self):
        coordinator = Mock(data={"doorbell": {"capabilities": ["doorbell"]}})
        entry = SimpleNamespace(
            options={CONF_LEGACY_DOORBELL_PRESSED: True},
            runtime_data=SimpleNamespace(coordinator=coordinator),
        )
        entities = []
        await async_setup_entry(Mock(), entry, entities.extend)
        self.assertEqual(entities[0].event_types, ["pressed"])


class DoorbellOptionsTests(unittest.IsolatedAsyncioTestCase):
    async def test_options_flow_defaults_saves_and_restores_legacy_selection(self):
        for options, expected in (
            ({}, False),
            ({CONF_LEGACY_DOORBELL_PRESSED: True}, True),
        ):
            with self.subTest(options=options):
                flow = EufySdkOptionsFlow()
                flow.handler = "synthetic-entry"
                flow.hass = Mock()
                flow.hass.config_entries.async_get_known_entry.return_value = (
                    SimpleNamespace(options=options)
                )
                result = await flow.async_step_init()
                values = result["data_schema"]({})
                self.assertIs(values[CONF_LEGACY_DOORBELL_PRESSED], expected)
                self.assertEqual(values[CONF_POLL_INTERVAL], 10)
                self.assertEqual(values[CONF_SOC_REFRESH], 60)
                saved = await flow.async_step_init(values)
                self.assertEqual(saved["data"], values)

    async def test_every_translation_explains_the_single_legacy_trigger(self):
        directory = (
            Path(__file__).parents[1] / "custom_components/eufy_sdk/translations"
        )
        for path in directory.glob("*.json"):
            with self.subTest(language=path.stem):
                step = json.loads(path.read_text(encoding="utf-8"))["options"]["step"][
                    "init"
                ]
                self.assertTrue(step["data"][CONF_LEGACY_DOORBELL_PRESSED])
                description = step["data_description"][CONF_LEGACY_DOORBELL_PRESSED]
                self.assertIn("pressed", description)
                self.assertIn("ring", description)


if __name__ == "__main__":
    unittest.main()
