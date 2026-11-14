"""Offline late-name routing, occurrence guards, ledger bounds and isolation."""

# ruff: noqa: ANN001, ANN201, D102, INP001, PT009, SLF001

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.core import Event, HomeAssistant, callback

import custom_components.eufy_sdk as integration
from custom_components.eufy_sdk.binary_sensor import EufyPushBinarySensor
from custom_components.eufy_sdk.bridge_events import async_listen_bridge_event
from custom_components.eufy_sdk.const import DOMAIN, EVENT_TYPE
from custom_components.eufy_sdk.entity import EufySdkDeviceEntity
from custom_components.eufy_sdk.event import EufySdkDetectionEvent
from custom_components.eufy_sdk.image import EufySdkEventImage
from custom_components.eufy_sdk.person_identification import (
    IDENTIFICATION_TTL_SECONDS,
    MAX_IDENTIFICATIONS,
    PERSON_IDENTIFIED_EVENT,
    PersonIdentifications,
    detected_at_for,
)
from custom_components.eufy_sdk.pushmap import (
    DETECTION_EVENTS,
    MOTION_EVENTS,
    PUBLIC_BUS_EVENTS,
    PUSH_BINARY_SENSORS,
    THUMBNAIL_EVENTS,
)
from custom_components.eufy_sdk.sensor import EufySdkLastPersonSensor

SERIAL = "synthetic-camera"
ROOT = str(Path(__file__).resolve().parents[1])
CREATED = 1_800_000_000
DETECTED = "2027-01-15T08:00:00+00:00"
IDENTIFIED = "2027-01-15T08:02:00+00:00"


def detection(**fields: object):
    """Build an unnamed occurrence with synthetic serial and numeric person id."""
    return {
        "event": "personDetected",
        "deviceSn": SERIAL,
        "create_time": CREATED,
        "trigger_time": CREATED,
        "person_id": 7,
        **fields,
    }


def correction(**fields: object):
    """Build the bridge contract with synthetic face names only."""
    return {
        **detection(),
        "event": "personIdentified",
        "person_name": "Synthetic A, Synthetic B",
        "person_names": ["Synthetic A", "Synthetic B"],
        "recognized": True,
        "detected_at_ms": CREATED * 1000,
        "identified_at_ms": (CREATED + 120) * 1000,
        **fields,
    }


class LastPersonIdentificationTests(unittest.TestCase):
    """Exercise the actual sensor independently of the central event publisher."""

    def setUp(self):
        coordinator = SimpleNamespace(data={}, inventory_evidence=None)
        self.entity = EufySdkLastPersonSensor(coordinator, SERIAL)
        self.entity.async_write_ha_state = Mock()

    def apply(self, data):
        self.entity._handle_event(Event(EVENT_TYPE, data))

    def test_accepts_only_current_human_and_preserves_person_id_and_detection(self):
        self.apply(detection())
        self.assertEqual(self.entity._person_occurrence, (CREATED, CREATED))
        self.assertEqual(self.entity.extra_state_attributes["detected_at"], DETECTED)
        self.entity.async_write_ha_state.reset_mock()
        self.apply(correction(person_id=999, detected_at_ms=(CREATED + 5) * 1000))
        self.assertEqual(self.entity.native_value, "Synthetic A, Synthetic B")
        self.assertEqual(
            self.entity.extra_state_attributes,
            {
                "inventory_quality": "unknown",
                "recognized": True,
                "person_id": 7,
                "event": "personIdentified",
                "late_identified": True,
                "identified_at": IDENTIFIED,
                "detected_at": DETECTED,
            },
        )
        self.apply(correction())
        self.entity.async_write_ha_state.assert_called_once_with()

    def test_newer_detection_blocks_older_correction(self):
        self.apply(detection())
        self.apply(detection(create_time=CREATED + 1))
        self.entity.async_write_ha_state.reset_mock()
        self.apply(correction())
        self.assertEqual(self.entity.native_value, "Human")
        self.entity.async_write_ha_state.assert_not_called()

    def test_already_named_including_literal_human_is_not_replaced(self):
        for name in ("Synthetic original", "Human"):
            with self.subTest(name=name):
                self.apply(detection(person_name=name, recognized=True))
                self.entity.async_write_ha_state.reset_mock()
                self.apply(correction())
                self.assertEqual(self.entity.native_value, name)
                self.entity.async_write_ha_state.assert_not_called()

    def test_stranger_records_identity_but_is_not_named(self):
        self.apply(detection(event="strangerDetected"))
        self.assertEqual(self.entity._person_occurrence, (CREATED, CREATED))
        self.assertEqual(self.entity.extra_state_attributes["detected_at"], DETECTED)
        self.entity.async_write_ha_state.reset_mock()
        self.apply(correction())
        self.assertEqual(self.entity.native_value, "Stranger")
        self.entity.async_write_ha_state.assert_not_called()

    def test_mismatched_occurrence_or_device_is_ignored(self):
        self.apply(detection())
        self.entity.async_write_ha_state.reset_mock()
        for fields in (
            {"create_time": CREATED + 1},
            {"trigger_time": CREATED + 1},
            {"trigger_time": None},
            {"deviceSn": "another-camera"},
            {"create_time": str(CREATED)},
        ):
            self.apply(correction(**fields))
        self.entity.async_write_ha_state.assert_not_called()

    def test_missing_trigger_time_on_both_sides_matches(self):
        original, identified = detection(), correction()
        original.pop("trigger_time")
        identified.pop("trigger_time")
        self.apply(original)
        self.apply(identified)
        self.assertTrue(self.entity.extra_state_attributes["late_identified"])

    def test_restored_human_without_occurrence_is_not_named(self):
        self.entity._attr_native_value = "Human"
        self.entity._person_attributes = {"recognized": False}
        self.apply(correction())
        self.entity.async_write_ha_state.assert_not_called()

    def test_detection_without_identity_cannot_match_empty_identity(self):
        self.apply(detection(create_time=None, trigger_time=None))
        self.entity.async_write_ha_state.reset_mock()
        self.apply(correction(create_time=None, trigger_time=None))
        self.entity.async_write_ha_state.assert_not_called()

    def test_bad_name_or_missing_device_is_ignored(self):
        self.apply(detection())
        self.entity.async_write_ha_state.reset_mock()
        for name in (None, 7, [], {}, "", "  "):
            self.apply(correction(person_name=name))
        payload = correction()
        payload.pop("deviceSn")
        self.apply(payload)
        self.entity.async_write_ha_state.assert_not_called()

    def test_detection_timestamps_are_optional_and_safe(self):
        self.assertEqual(detected_at_for({"detected_at_ms": CREATED * 1000}), DETECTED)
        self.assertEqual(detected_at_for({"create_time": str(CREATED)}), DETECTED)
        self.assertEqual(detected_at_for({"create_time": CREATED * 1000}), DETECTED)
        for value in (None, [], {}, True, "bad", float("inf"), 10**1000):
            self.assertIsNone(detected_at_for({"create_time": value}))


class IdentificationRoutingTests(unittest.IsolatedAsyncioTestCase):
    """Use the real setup callback, sensor subscription, dispatcher and HA bus."""

    async def asyncSetUp(self):
        self.hass = HomeAssistant(ROOT)
        self.hass.config_entries = Mock(async_forward_entry_setups=AsyncMock())
        self.coordinator = Mock(
            data={SERIAL: {}},
            inventory_evidence=None,
            async_config_entry_first_refresh=AsyncMock(),
            async_request_refresh=AsyncMock(),
        )
        self.recordings = Mock(scope="synthetic-scope")
        client = Mock(
            set_poll_ms=AsyncMock(),
            get_properties=AsyncMock(return_value=[]),
            recordings_capability={"scopeId": "synthetic-scope"},
        )
        entry = Mock(data={"host": "bridge.invalid", "port": 3000}, options={})
        entry.domain = DOMAIN
        with (
            patch.object(integration, "async_reconcile_property_entities"),
            patch.object(
                integration,
                "EufySdkDataUpdateCoordinator",
                return_value=self.coordinator,
            ),
            patch.object(
                integration, "EufySdkApiClient", return_value=client
            ) as factory,
            patch.object(integration, "async_get_clientsession", return_value=Mock()),
            patch.object(
                integration, "async_get_loaded_integration", return_value=Mock()
            ),
            patch.object(integration, "RecordingRuntime", return_value=self.recordings),
        ):
            await integration.async_setup_entry(self.hass, entry)
        self.on_event = factory.call_args.kwargs["on_event"]
        self.entry = entry
        self.entity = EufySdkLastPersonSensor(self.coordinator, SERIAL)
        self.entity.hass = self.hass
        self.entity.async_write_ha_state = Mock()
        self.entity.async_on_remove = Mock(side_effect=self.addCleanup)
        with patch.object(EufySdkDeviceEntity, "async_added_to_hass", AsyncMock()):
            await self.entity.async_added_to_hass()
        self.public, self.identified, self.internal = [], [], []

        @callback
        def public(event: Event) -> None:
            self.public.append(event.data)

        @callback
        def identified(event: Event) -> None:
            self.identified.append(event.data)

        @callback
        def internal(event: Event) -> None:
            self.internal.append(event.data)

        self.addCleanup(self.hass.bus.async_listen(EVENT_TYPE, public))
        self.addCleanup(self.hass.bus.async_listen(PERSON_IDENTIFIED_EVENT, identified))
        self.addCleanup(async_listen_bridge_event(self.hass, internal))
        self.registry = Mock()
        self.registry.async_get_device.return_value = SimpleNamespace(id="ha-camera-id")
        patcher = patch(
            "custom_components.eufy_sdk.person_identification.dr.async_get",
            return_value=self.registry,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    async def drain(self):
        await self.hass.async_block_till_done()

    async def test_duplicate_writes_and_publishes_once_with_acceptance(self):
        self.on_event(detection())
        self.entity.async_write_ha_state.reset_mock()
        self.on_event(correction())
        self.on_event(correction())
        self.on_event(correction(person_name="Synthetic changed"))
        await self.drain()
        self.entity.async_write_ha_state.assert_called_once_with()
        self.assertEqual(
            self.identified,
            [
                {
                    "device_id": "ha-camera-id",
                    "deviceSn": SERIAL,
                    "person_name": "Synthetic A, Synthetic B",
                    "person_names": ["Synthetic A", "Synthetic B"],
                    "detected_at": DETECTED,
                    "identified_at": IDENTIFIED,
                    "current_last_person": True,
                }
            ],
        )
        self.registry.async_get_device.assert_called_once_with(
            identifiers={(DOMAIN, SERIAL)}
        )
        self.assertEqual(self.public, [detection()])
        self.assertEqual(self.internal, [detection()])

    async def test_ignored_old_correction_still_publishes_once(self):
        self.on_event(detection())
        self.on_event(detection(create_time=CREATED + 1))
        self.entity.async_write_ha_state.reset_mock()
        self.on_event(correction())
        self.on_event(correction())
        await self.drain()
        self.entity.async_write_ha_state.assert_not_called()
        self.assertEqual(len(self.identified), 1)
        self.assertFalse(self.identified[0]["current_last_person"])

    async def test_other_sensor_rejections_still_publish_with_false(self):
        originals = (
            detection(person_name="Synthetic original", recognized=True),
            detection(event="strangerDetected"),
            detection(trigger_time=CREATED + 1),
        )
        for index, original in enumerate(originals):
            created = CREATED + index
            self.on_event({**original, "create_time": created})
            self.entity.async_write_ha_state.reset_mock()
            self.on_event(correction(create_time=created))
            self.entity.async_write_ha_state.assert_not_called()
        await self.drain()
        self.assertEqual(len(self.identified), len(originals))
        self.assertTrue(
            all(not event["current_last_person"] for event in self.identified)
        )

    async def test_sensor_removal_retires_acceptance_subscription(self):
        self.on_event(detection())
        self.entity.async_write_ha_state.reset_mock()
        for remove in self.entity.async_on_remove.call_args_list:
            remove.args[0]()
        self.on_event(correction())
        await self.drain()
        self.entity.async_write_ha_state.assert_not_called()
        self.assertFalse(self.identified[0]["current_last_person"])

    async def test_absent_sensor_or_registry_device_still_publishes(self):
        self.registry.async_get_device.return_value = None
        self.on_event(correction(deviceSn="absent-camera"))
        await self.drain()
        self.assertIsNone(self.identified[0]["device_id"])
        self.assertEqual(self.identified[0]["deviceSn"], "absent-camera")
        self.assertFalse(self.identified[0]["current_last_person"])

    async def test_malformed_messages_are_ignored_before_dispatch(self):
        payload = correction()
        payload.pop("deviceSn")
        invalid = [payload]
        invalid.extend(
            correction(person_name=name) for name in (None, 1, [], {}, "", " ")
        )
        invalid.extend(
            correction(**fields)
            for fields in (
                {"deviceSn": []},
                {"deviceSn": " "},
                {"create_time": []},
                {"create_time": None},
                {"trigger_time": {}},
            )
        )
        for payload in invalid:
            self.on_event(payload)
        await self.drain()
        self.assertEqual(self.identified, [])
        self.assertEqual(self.internal, [])
        self.entity.async_write_ha_state.assert_not_called()

    async def test_no_detection_or_image_recording_side_effects(self):
        motion = EufyPushBinarySensor(
            self.coordinator, SERIAL, MOTION_EVENTS, ("motion", "Motion", None)
        )
        person = EufyPushBinarySensor(
            self.coordinator,
            SERIAL,
            frozenset({"personDetected"}),
            ("person", "Person", None),
        )
        event_entity = EufySdkDetectionEvent(self.coordinator, SERIAL)
        image = object.__new__(EufySdkEventImage)
        image._sn = SERIAL
        image.hass = self.hass
        image._refresh = AsyncMock()
        image._event_time = DETECTED
        image._image = b"retained image"
        for entity in (motion, person, event_entity, image):
            entity.async_write_ha_state = Mock()
            self.addCleanup(async_listen_bridge_event(self.hass, entity._handle_event))
        event_entity._trigger_event = Mock()
        for mapping in (
            DETECTION_EVENTS,
            MOTION_EVENTS,
            PUBLIC_BUS_EVENTS,
            PUSH_BINARY_SENSORS,
            THUMBNAIL_EVENTS,
        ):
            self.assertNotIn("personIdentified", mapping)
        self.on_event(correction())
        await self.drain()
        # Consumers remain safe even if a future caller dispatches it directly.
        for entity in (motion, person, event_entity, image):
            entity._handle_event(Event(EVENT_TYPE, correction()))
            entity.async_write_ha_state.assert_not_called()
        self.assertFalse(motion.is_on)
        self.assertFalse(person.is_on)
        event_entity._trigger_event.assert_not_called()
        image._refresh.assert_not_awaited()
        self.assertEqual(image._image, b"retained image")
        self.assertEqual(image._event_time, DETECTED)
        self.recordings.changed.assert_not_called()
        self.recordings.reset.assert_not_called()
        self.coordinator.async_request_refresh.assert_not_awaited()
        self.coordinator.request_state_refresh.assert_not_called()
        self.coordinator.async_update_listeners.assert_not_called()
        self.assertEqual(self.public, [])
        self.assertEqual(self.internal, [])


class IdentificationLedgerTests(unittest.TestCase):
    """Check bounded deduplication with a controlled monotonic clock."""

    def setUp(self):
        self.hass = Mock()
        self.router = PersonIdentifications(self.hass)
        self.listener = Mock(return_value=False)
        self.router.listen(SERIAL, self.listener)
        patcher = patch(
            "custom_components.eufy_sdk.person_identification.dr.async_get",
            return_value=Mock(),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_cap_rejects_new_occurrences_and_does_not_evict_or_extend_duplicates(self):
        with patch(
            "custom_components.eufy_sdk.person_identification.monotonic", return_value=0
        ):
            for index in range(MAX_IDENTIFICATIONS):
                self.router.apply(correction(create_time=CREATED + index))
        with patch(
            "custom_components.eufy_sdk.person_identification.monotonic",
            return_value=IDENTIFICATION_TTL_SECONDS - 1,
        ):
            self.router.apply(correction())
            self.router.apply(correction(create_time=CREATED + MAX_IDENTIFICATIONS))
            self.router.apply(correction())
        self.assertEqual(len(self.router._seen), MAX_IDENTIFICATIONS)
        self.assertEqual(self.hass.bus.async_fire.call_count, MAX_IDENTIFICATIONS)
        self.assertEqual(self.listener.call_count, MAX_IDENTIFICATIONS)
        with patch(
            "custom_components.eufy_sdk.person_identification.monotonic",
            return_value=IDENTIFICATION_TTL_SECONDS,
        ):
            self.router.apply(correction())
        self.assertEqual(len(self.router._seen), 1)
        self.assertEqual(self.hass.bus.async_fire.call_count, MAX_IDENTIFICATIONS + 1)

    def test_key_includes_device_and_both_occurrence_times(self):
        for fields in ({}, {"deviceSn": "other"}, {"trigger_time": CREATED + 1}):
            self.router.apply(correction(**fields))
            self.router.apply(correction(**fields))
        self.assertEqual(self.hass.bus.async_fire.call_count, 3)

    def test_missing_trigger_is_deduped_and_bad_optional_fields_are_safe(self):
        payload = correction(person_names=[7], identified_at_ms={})
        payload.pop("trigger_time")
        self.router.apply(payload)
        self.router.apply(payload)
        self.hass.bus.async_fire.assert_called_once()
        data = self.hass.bus.async_fire.call_args.args[1]
        self.assertEqual(data["person_names"], [payload["person_name"]])
        self.assertIsNone(data["identified_at"])

    def test_unsubscribe_and_close_release_active_sensor(self):
        remove = self.router.listen(SERIAL, self.listener)
        replacement = Mock(return_value=True)
        self.router.listen(SERIAL, replacement)
        remove()
        self.router.apply(correction())
        replacement.assert_called_once()
        self.listener.assert_not_called()
        self.router.close()
        self.assertEqual(self.router._seen, {})
        self.assertEqual(self.router._listeners, {})
