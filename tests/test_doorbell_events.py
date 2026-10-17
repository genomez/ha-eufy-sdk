# ruff: noqa: ANN001, ANN201, D100, D101, D102, INP001, PT009, SLF001
# Added 2026-10-17: standard ring and legacy doorbell event regressions.

import unittest
from unittest.mock import Mock

from homeassistant.components.event import DoorbellEventType, EventDeviceClass

from custom_components.eufy_sdk.event import (
    EufySdkDetectionEvent,
    EufySdkDoorbellEvent,
    async_setup_entry,
)
from custom_components.eufy_sdk.pushmap import DETECTION_EVENTS, DOORBELL_EVENT_TYPE


def make_entity(entity_class=EufySdkDoorbellEvent):
    """Capture each published event type through the native HA event attributes."""
    coordinator = Mock(data={"doorbell": {"name": "Front door"}})
    entity = entity_class(coordinator, "doorbell")
    writes = []
    entity.async_write_ha_state = Mock(
        side_effect=lambda: writes.append(dict(entity.state_attributes))
    )
    return entity, writes


class DoorbellEventTests(unittest.TestCase):
    def test_doorbell_advertises_ring_and_keeps_pressed(self):
        entity, _ = make_entity()
        self.assertEqual(entity.device_class, EventDeviceClass.DOORBELL)
        self.assertEqual(entity.event_types, [DOORBELL_EVENT_TYPE, "ring"])
        self.assertIn(DoorbellEventType.RING, entity.event_types)
        self.assertEqual(entity.unique_id, "doorbell_doorbell_event")

    def test_press_writes_both_events_with_ring_as_final_type(self):
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
                    "event_type": "pressed",
                    "thumbnailUrl": "http://bridge.invalid/thumbnail",
                },
                {
                    "event_type": "ring",
                    "thumbnailUrl": "http://bridge.invalid/thumbnail",
                },
            ],
        )
        self.assertEqual(entity.state_attributes["event_type"], "ring")
        self.assertIsNotNone(entity.state)

    def test_repeated_presses_publish_both_types_each_time(self):
        entity, writes = make_entity()
        event = Mock(data={"deviceSn": "doorbell", "event": "doorbellPress"})
        for _ in range(2):
            entity._handle_event(event)
        self.assertEqual(
            [attrs["event_type"] for attrs in writes],
            ["pressed", "ring", "pressed", "ring"],
        )

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

    def test_empty_thumbnail_is_omitted_for_both_events(self):
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
        self.assertEqual(writes, [{"event_type": "pressed"}, {"event_type": "ring"}])

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
        entry.runtime_data.coordinator = coordinator
        add_entities = Mock()
        await async_setup_entry(Mock(), entry, add_entities)
        entities = add_entities.call_args.args[0]
        doorbells = [e for e in entities if isinstance(e, EufySdkDoorbellEvent)]
        self.assertEqual(len(doorbells), 1)
        self.assertEqual(doorbells[0]._sn, "doorbell")
        self.assertIn("ring", doorbells[0].event_types)


if __name__ == "__main__":
    unittest.main()
