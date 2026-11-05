# ruff: noqa: ANN001, ANN201, ANN202, ANN204, D100, D101, D102, INP001, PT009, SLF001
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
from fixtures import recordings_v1 as fixture
from homeassistant.config_entries import ConfigEntryState

import custom_components.eufy_sdk as integration
from custom_components.eufy_sdk.api import EufySdkApiClient
from custom_components.eufy_sdk.const import CONF_HOST, CONF_PORT
from custom_components.eufy_sdk.media_source import EufyRecordingMediaSource


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hass = Mock()
        self.hass.config_entries.async_forward_entry_setups = AsyncMock()
        self.entry = Mock()
        self.entry.data = {CONF_HOST: "bridge.invalid", CONF_PORT: 3000}
        self.entry.options = {}
        self.entry.domain = "eufy_sdk"
        self.entry.state = ConfigEntryState.LOADED
        self.hass.config_entries.async_entries.return_value = [self.entry]
        self.coordinator = Mock(data={})
        self.coordinator.async_config_entry_first_refresh = AsyncMock()
        self.coordinator.async_request_refresh = AsyncMock()

        def make_client(**kwargs: object):
            client = EufySdkApiClient(**kwargs)
            client.set_poll_ms = AsyncMock()
            client._closing = True
            self.client = client
            return client

        with (
            patch.object(integration, "async_reconcile_property_entities"),
            patch.object(
                integration,
                "EufySdkDataUpdateCoordinator",
                return_value=self.coordinator,
            ),
            patch.object(integration, "EufySdkApiClient", side_effect=make_client),
            patch.object(integration, "async_get_clientsession", return_value=Mock()),
            patch.object(
                integration, "async_get_loaded_integration", return_value=Mock()
            ),
        ):
            await integration.async_setup_entry(self.hass, self.entry)
        self.source = EufyRecordingMediaSource(self.hass)
        self.addCleanup(
            lambda: (
                self.entry.runtime_data.recordings
                and self.entry.runtime_data.recordings.close()
            )
        )

    async def deliver(self, *payloads: dict):
        class Frames:
            closed = False

            def __aiter__(self):
                async def frames():
                    for payload in payloads:
                        yield Mock(
                            type=aiohttp.WSMsgType.TEXT, json=Mock(return_value=payload)
                        )

                return frames()

        self.client._ws = Frames()
        await self.client._receive_loop()

    async def root(self):
        self.client.rpc = AsyncMock(
            return_value={"ok": True, "recordings": fixture.cameras()}
        )
        return await self.source.async_browse_media(SimpleNamespace(identifier=""))

    async def test_early_hello_then_enrollment_enables_media_without_reconnect(self):
        # Two consecutive frames in one receive loop, with no reconnect between.
        states = []
        on_event = self.client._on_event

        def observe(event):
            on_event(event)
            states.append(self.entry.runtime_data.recordings)

        self.client._on_event = observe
        await self.deliver(
            {
                "event": "hello",
                "recordings": {**fixture.capability(), "enabled": False},
            },
            {"event": "recordings.capability", "recordings": fixture.capability()},
        )
        self.assertIsNone(states[0])
        self.assertIsNotNone(states[1])
        self.assertEqual(len((await self.root()).children), 5)
        self.hass.bus.async_fire.assert_called_once()
        self.assertEqual(self.hass.bus.async_fire.call_args.args[1]["event"], "hello")

    async def test_capability_retirement_and_reconnect_hello_replace_runtime(self):
        await self.deliver({"event": "hello", "recordings": fixture.capability()})
        self.assertEqual(len((await self.root()).children), 5)
        old = self.entry.runtime_data.recordings
        old.interests[fixture.CAMERA] = ("B" * 22, 999999999, frozenset())
        await self.deliver(
            {
                "event": "recordings.capability",
                "recordings": {**fixture.capability(), "scopeId": "s_" + "Z" * 22},
            }
        )
        self.assertTrue(old.closed)
        self.assertEqual(old.interests, {})
        self.assertIsNot(self.entry.runtime_data.recordings, old)
        replacement = self.entry.runtime_data.recordings
        await self.deliver({"event": "hello"})
        self.assertTrue(replacement.closed)
        self.assertIsNone(self.entry.runtime_data.recordings)
        self.assertEqual((await self.root()).children, [])
        self.client.rpc.assert_not_awaited()
        await self.deliver({"event": "hello", "recordings": fixture.capability()})
        self.assertEqual(len((await self.root()).children), 5)
        await self.deliver(
            {
                "event": "recordings.capability",
                "recordings": {**fixture.capability(), "enabled": False},
            }
        )
        self.assertIsNone(self.entry.runtime_data.recordings)
        self.assertEqual((await self.root()).children, [])

    async def test_local_option_prevents_late_runtime_creation(self):
        self.entry.options = {"recordings_enabled": False}
        await self.deliver(
            {"event": "recordings.capability", "recordings": fixture.capability()}
        )
        self.assertIsNone(self.entry.runtime_data.recordings)
        self.assertEqual((await self.root()).children, [])
