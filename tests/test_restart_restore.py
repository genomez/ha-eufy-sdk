"""Exercise shutdown, restore dumps and bridge outages without external I/O."""

# ruff: noqa: ANN001, ANN201, D102, INP001, PT009, SLF001

import asyncio
import logging
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import restore_state
from homeassistant.helpers.entity_platform import EntityPlatform
from homeassistant.helpers.json import json_dumps
from homeassistant.util.json import json_loads
from test_camera_health_disconnect import OfflineSocket

from custom_components.eufy_sdk.api import EufySdkApiClient
from custom_components.eufy_sdk.binary_sensor import EufyPackageBinarySensor
from custom_components.eufy_sdk.const import DOMAIN, EVENT_TYPE
from custom_components.eufy_sdk.coordinator import EufySdkDataUpdateCoordinator
from custom_components.eufy_sdk.event import (
    EufySdkDetectionEvent,
    EufySdkDoorbellEvent,
)
from custom_components.eufy_sdk.select import EufySdkSnapshotPolicySelect

ROOT = str(Path(__file__).resolve().parents[1])
LOGGER = logging.getLogger(__name__)


class RestartRestoreTests(unittest.IsolatedAsyncioTestCase):
    """Use HA's state machine, restore serializer and native EventEntity restore."""

    async def asyncSetUp(self):
        self.runtimes = []
        self.hass, self.coordinator, self.client, self.entities = await self.setup()
        self.socket = OfflineSocket()
        self.client._ws = self.socket
        self.client._schedule_reconnect = Mock()
        self.client._recv_task = asyncio.create_task(self.client._receive_loop())
        self.client.auth_status = AsyncMock(return_value={"state": "ok"})
        self.client.list_solix_devices = AsyncMock(return_value=[])
        self.hass.bus.async_fire(
            EVENT_TYPE, {"deviceSn": "synthetic", "event": "packageDelivered"}
        )
        self.hass.bus.async_fire(
            EVENT_TYPE, {"deviceSn": "synthetic", "event": "doorbellPress"}
        )
        self.hass.bus.async_fire(
            EVENT_TYPE,
            {
                "deviceSn": "synthetic",
                "event": "vehicleDetected",
                "thumbnailUrl": "synthetic-thumbnail",
            },
        )
        await self.hass.async_block_till_done()
        await self.entities[-1].async_select_option("stored")
        self.original = {
            entity.entity_id: self.hass.states.get(entity.entity_id).state
            for entity in self.entities
        }
        self.assertNotIn("unknown", self.original.values())

    async def asyncTearDown(self):
        for hass, coordinator, client, entities in reversed(self.runtimes):
            for entity in entities:
                await entity.async_remove()
            receiver = client._recv_task
            await client.close()
            if receiver is not None:
                await receiver
            await coordinator.async_shutdown()
            await hass.async_block_till_done()

    async def setup(self, saved=None):
        hass = HomeAssistant(ROOT)
        hass.set_state(CoreState.running)
        devices = dr.DeviceRegistry(hass)
        hass.data[dr.DATA_REGISTRY] = devices
        await devices.async_load(load_empty=True)
        registry = er.EntityRegistry(hass)
        hass.data[er.DATA_REGISTRY] = registry
        await registry.async_load()
        restore = restore_state.async_get(hass)
        if saved is not None:
            with patch.object(
                restore.store, "async_load", AsyncMock(return_value=saved)
            ):
                await restore.async_load()
        coordinator = EufySdkDataUpdateCoordinator(
            hass, LOGGER, name=DOMAIN, config_entry=None
        )
        client = EufySdkApiClient("synthetic-bridge", 3000, Mock())
        coordinator.config_entry = SimpleNamespace(
            runtime_data=SimpleNamespace(client=client, snapshot_policy={}),
            pref_disable_polling=False,
        )
        coordinator.data = {"synthetic": {}}
        entities = [
            EufySdkDetectionEvent(coordinator, "synthetic"),
            EufySdkDoorbellEvent(coordinator, "synthetic"),
            EufyPackageBinarySensor(coordinator, "synthetic"),
            EufySdkSnapshotPolicySelect(coordinator, "synthetic"),
        ]
        self.runtimes.append((hass, coordinator, client, entities))
        for entity, domain, key in zip(
            entities,
            ("event", "event", "binary_sensor", "select"),
            ("detection", "doorbell", "package", "policy"),
            strict=True,
        ):
            platform = EntityPlatform(
                hass=hass,
                logger=LOGGER,
                domain=domain,
                platform_name=DOMAIN,
                platform=None,
                scan_interval=timedelta(seconds=30),
                entity_namespace=None,
            )
            entity.add_to_platform_start(hass, platform, None)
            entity.entity_id = f"{domain}.synthetic_{key}"
            await entity.add_to_platform_finish()
        return hass, coordinator, client, entities

    async def disconnect_during_refresh(self, *, stopping=False):
        entered = asyncio.Event()
        self.socket.send_json = AsyncMock(side_effect=lambda _frame: entered.set())
        refresh = asyncio.create_task(self.coordinator.async_refresh())
        await entered.wait()
        if stopping:
            self.hass.set_state(CoreState.stopping)
            self.hass.bus.async_fire_internal(EVENT_HOMEASSISTANT_STOP)
            await self.hass.async_block_till_done()
        with (
            patch(
                "custom_components.eufy_sdk.coordinator.async_call_later",
                return_value=Mock(),
            ),
            self.assertLogs(LOGGER, level="ERROR"),
        ):
            await self.socket.frames.put(None)
            await self.client._recv_task
            await refresh
        self.assertFalse(self.coordinator.last_update_success)

    async def dump(self):
        restore = restore_state.async_get(self.hass)
        with patch.object(restore.store, "async_save", AsyncMock()) as save:
            await restore.async_dump_states()
        return json_loads(json_dumps(save.call_args.args[0]))

    async def test_stop_then_disconnect_dump_restores_original_event_timestamps(self):
        await self.disconnect_during_refresh(stopping=True)
        self.hass.bus.async_fire(
            EVENT_TYPE, {"deviceSn": "synthetic", "event": "motion"}
        )
        await self.hass.async_block_till_done()
        self.hass.set_state(CoreState.final_write)
        for entity in self.entities:
            entity.async_write_ha_state()
        saved = await self.dump()
        self.assertEqual(
            {item["state"]["entity_id"]: item["state"]["state"] for item in saved},
            self.original,
        )
        restarted, _, _, entities = await self.setup(saved)
        for entity in entities:
            with self.subTest(entity=entity.entity_id):
                self.assertEqual(
                    restarted.states.get(entity.entity_id).state,
                    self.original[entity.entity_id],
                )
        self.assertEqual(entities[0].state_attributes["event_type"], "vehicle")
        self.assertEqual(
            entities[0].state_attributes["thumbnailUrl"], "synthetic-thumbnail"
        )
        self.assertEqual(entities[1].state_attributes["event_type"], "ring")

    async def test_real_disconnect_while_running_still_publishes_unavailable(self):
        await self.disconnect_during_refresh()
        for entity in self.entities:
            with self.subTest(entity=entity.entity_id):
                self.assertEqual(
                    self.hass.states.get(entity.entity_id).state, "unavailable"
                )
        self.coordinator.async_set_updated_data(self.coordinator.data)
        for entity in self.entities:
            self.assertEqual(
                self.hass.states.get(entity.entity_id).state,
                self.original[entity.entity_id],
            )

    async def test_unavailable_dump_restores_event_timestamps_and_metadata(self):
        await self.disconnect_during_refresh()
        saved = await self.dump()
        detection = next(
            item
            for item in saved
            if item["state"]["entity_id"] == self.entities[0].entity_id
        )
        self.assertEqual(detection["state"]["state"], "unavailable")
        self.assertEqual(detection["extra_data"]["last_event_type"], "vehicle")
        self.assertEqual(
            detection["extra_data"]["last_event_timestamp"],
            self.original[self.entities[0].entity_id],
        )
        restarted, _, _, entities = await self.setup(saved)
        for entity in entities[:2]:
            self.assertEqual(
                restarted.states.get(entity.entity_id).state,
                self.original[entity.entity_id],
            )
        self.assertEqual(entities[0].state_attributes["event_type"], "vehicle")

    async def test_legacy_valid_timestamps_restore_without_extra_timestamp(self):
        saved = await self.dump()
        for item in saved:
            if item["extra_data"]:
                item["extra_data"].pop("last_event_timestamp", None)
        restarted, _, _, entities = await self.setup(saved)
        for entity in entities[:2]:
            self.assertEqual(
                restarted.states.get(entity.entity_id).state,
                self.original[entity.entity_id],
            )

    async def test_missing_or_invalid_extra_timestamp_does_not_invent_event_time(self):
        await self.disconnect_during_refresh()
        saved = await self.dump()
        for timestamp in (
            None,
            1,
            "not a timestamp",
            "2026-10-23T10:00:00",
            "2026-99-99T99:99:99+00:00",
        ):
            with self.subTest(timestamp=timestamp):
                for item in saved:
                    if item["extra_data"]:
                        item["extra_data"]["last_event_timestamp"] = timestamp
                restarted, _, _, entities = await self.setup(saved)
                self.assertEqual(
                    restarted.states.get(entities[0].entity_id).state, "unknown"
                )
                self.assertEqual(entities[0].state_attributes["event_type"], "vehicle")

    async def test_two_restarts_preserve_time_without_firing_a_new_event(self):
        await self.disconnect_during_refresh()
        saved = await self.dump()
        for _ in range(2):
            with (
                patch.object(EufySdkDetectionEvent, "_trigger_event") as detection,
                patch.object(EufySdkDoorbellEvent, "_trigger_event") as doorbell,
            ):
                restarted, _, _, entities = await self.setup(saved)
            detection.assert_not_called()
            doorbell.assert_not_called()
            for entity in entities[:2]:
                self.assertEqual(
                    restarted.states.get(entity.entity_id).state,
                    self.original[entity.entity_id],
                )
            restore = restore_state.async_get(restarted)
            saved = json_loads(
                json_dumps(
                    [state.as_dict() for state in restore.async_get_stored_states()]
                )
            )
