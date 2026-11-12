"""Keep internal bridge delivery while limiting recorder-visible automation events."""

# ruff: noqa: ANN201, D102, INP001, PT009
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.core import Event, HomeAssistant, callback

import custom_components.eufy_sdk as integration
from custom_components.eufy_sdk.bridge_events import (
    async_dispatch_bridge_event,
    async_listen_bridge_event,
)
from custom_components.eufy_sdk.const import EVENT_TYPE
from custom_components.eufy_sdk.device_trigger import TRIGGER_MAP
from custom_components.eufy_sdk.pushmap import PUBLIC_BUS_EVENTS, THUMBNAIL_EVENTS

ROOT = str(Path(__file__).resolve().parents[1])

PUBLIC_EVENTS = (
    "motion",
    "personDetected",
    "strangerDetected",
    "petDetection",
    "dogDetected",
    "vehicleDetected",
    "soundDetected",
    "cryingDetected",
    "doorbellPress",
    "packageDelivered",
    "packageTaken",
    "packageStranded",
    "streamState",
)


class RecorderEventRoutingTests(unittest.IsolatedAsyncioTestCase):
    """Exercise the real setup callback, dispatcher and HA bus without transport."""

    async def asyncSetUp(self):
        self.hass = HomeAssistant(ROOT)
        self.hass.config_entries = Mock(async_forward_entry_setups=AsyncMock())
        self.coordinator = Mock(
            data={"synthetic": {"state": {"light": False}}},
            property_observations=None,
            async_config_entry_first_refresh=AsyncMock(),
            async_request_refresh=AsyncMock(),
        )
        client = Mock(
            set_poll_ms=AsyncMock(), get_properties=AsyncMock(return_value=[])
        )
        entry = Mock(
            data={"host": "bridge.invalid", "port": 3000},
            options={"recordings_enabled": False},
            domain="eufy_sdk",
        )
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
        ):
            await integration.async_setup_entry(self.hass, entry)
        self.on_event = factory.call_args.kwargs["on_event"]
        self.public, self.internal = [], []

        @callback
        def public(event: Event) -> None:
            self.public.append(event.data)

        @callback
        def internal(event: Event) -> None:
            self.internal.append(event.data)

        self.addCleanup(self.hass.bus.async_listen(EVENT_TYPE, public))
        self.addCleanup(async_listen_bridge_event(self.hass, internal))

    async def test_public_events_reach_internal_and_public_consumers_once(self):
        self.assertEqual(PUBLIC_BUS_EVENTS, frozenset(PUBLIC_EVENTS))
        self.assertTrue(
            {event for event, _ in TRIGGER_MAP.values()} <= PUBLIC_BUS_EVENTS
        )
        self.assertTrue(THUMBNAIL_EVENTS <= PUBLIC_BUS_EVENTS)
        payloads = [{"event": name, "deviceSn": "synthetic"} for name in PUBLIC_EVENTS]
        for payload in payloads:
            self.on_event(payload)
        await self.hass.async_block_till_done()
        self.assertEqual(self.public, payloads)
        self.assertEqual(self.internal, payloads)

    async def test_telemetry_retains_internal_delivery_and_state_updates_only(self):
        payloads = [
            {"event": name, "deviceSn": "synthetic"}
            for name in (
                "propertyEvidenceChanged",
                "cameraHealth",
                "commandHealth",
                "guardRuntime",
                "guardConfirmation",
                "deviceMetadata",
                "eventImageUpdated",
                "lateContraryEffect",
                "solixReading",
                "batteryLevel",
                "batteryAlert",
                "ptzNotify",
                "smartLightState",
                "hello",
                "ready",
                "bridgeDisconnected",
                "futureTelemetry",
            )
        ]
        payloads.extend(
            [
                {
                    "event": "propertyChanged",
                    "deviceSn": "synthetic",
                    "property": "light",
                    "value": True,
                },
                {"event": "contactState", "deviceSn": "synthetic", "open": True},
                {"event": "armingModeChanged", "deviceSn": "synthetic", "mode": 0},
                {
                    "event": "propertiesObserved",
                    "deviceSn": "synthetic",
                    "source": "rtc",
                    "observedAtMs": 100000,
                    "properties": {"light": False},
                },
            ]
        )
        with patch("custom_components.eufy_sdk.property_sync.time", return_value=100):
            for payload in payloads:
                self.on_event(payload)
        await self.hass.async_block_till_done()
        self.assertEqual(self.public, [])
        self.assertEqual(self.internal, payloads)
        self.assertFalse(self.coordinator.data["synthetic"]["state"]["light"])
        self.assertTrue(self.coordinator.data["synthetic"]["state"]["contact"])
        self.assertEqual(
            self.coordinator.property_observations["synthetic"]["light"]["value"], False
        )
        self.coordinator.apply_camera_health_event.assert_called_once_with(payloads[1])
        self.coordinator.async_request_refresh.assert_awaited_once()

    async def test_private_filter_and_unsubscribe_do_not_change_public_bus(self):
        received = []

        @callback
        def receive(event: Event) -> None:
            received.append(event.data)

        unsubscribe = async_listen_bridge_event(
            self.hass,
            receive,
            event_filter=lambda data: data.get("property") == "light",
        )
        report = {"event": "propertyChanged", "property": "light", "value": False}
        async_dispatch_bridge_event(self.hass, {**report, "property": "battery"})
        async_dispatch_bridge_event(self.hass, report)
        await self.hass.async_block_till_done()
        unsubscribe()
        async_dispatch_bridge_event(self.hass, report)
        await self.hass.async_block_till_done()
        self.assertEqual(received, [report])
        self.assertEqual(self.public, [])
