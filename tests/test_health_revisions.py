"""Reject reordered health projections across receipts, events, and snapshots."""

# ruff: noqa: ANN001, ANN201, ANN202, D102, FBT003, INP001, PT009, PT027, SLF001
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import test_aligned_state as aligned
import test_light_correction as fixtures
from aiohttp import WSMsgType

from custom_components.eufy_sdk.api import (
    MAX_COMMAND_HEALTH_TARGETS,
    EufySdkApiClientCommunicationError,
    _command_receipt,
    command_health,
    command_health_event,
)


def projection(status="command_degraded", remaining=120000, revision=1):
    """Build a synthetic SDK health projection with a runtime revision."""
    return {"status": status, "cooldownRemainingMs": remaining, "revision": revision}


class HealthRevisionTests(fixtures.SafetyFixture, unittest.IsolatedAsyncioTestCase):
    """Exercise current gate state independently from historical receipt evidence."""

    def event(self, health):
        self.entity._handle_property_changed(
            SimpleNamespace(
                data={
                    "event": "commandHealth",
                    "sn": "fixture-device",
                    "property": "light",
                    **health,
                }
            )
        )

    async def test_older_healthy_receipt_cannot_clear_newer_degraded_event(self):
        entity, _, _, _ = await self.activate()
        await entity.write(False)
        self.emit()
        self.event(projection())
        await self.advance(10)
        receipt = _command_receipt(
            {
                "status": "acknowledged",
                "sendAttempted": True,
                "effect": "unconfirmed",
                "intent": 1,
                "commandHealth": projection("healthy", 0, 0),
            }
        )
        entity._light_correction.receipt(receipt)
        await self.advance(119)
        self.assertEqual(
            entity.extra_state_attributes["command_health"], "command_degraded"
        )
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(receipt.command_health, projection("healthy", 0, 0))

    async def test_older_event_snapshot_and_unversioned_health_cannot_clear_gate(self):
        entity, coordinator, _, _ = await self.activate()
        await entity.write(False)
        self.emit()
        self.event(projection())
        await self.advance(10)
        for health in (
            projection("healthy", 0, 0),
            {"status": "healthy", "cooldownRemainingMs": 0},
        ):
            self.event(health)
            coordinator.data["fixture-device"]["commandHealth"] = health
            entity._handle_coordinator_update()
        await self.advance(119)
        self.assertEqual(
            entity.extra_state_attributes["command_health"], "command_degraded"
        )
        self.assertEqual(len(self.commands), 1)

    async def test_same_revision_cannot_extend_original_cooldown(self):
        entity, _, _, _ = await self.activate()
        await entity.write(False)
        self.emit()
        self.event(projection())
        await self.advance(60)
        self.event(projection(remaining=119000))
        await self.advance(120)
        self.assertEqual(entity.extra_state_attributes["command_health"], "healthy")
        self.assertEqual(self.commands[-1][:2], (120, False))

    async def test_disconnect_ready_rearms_original_expiry_without_projection(self):
        entity, _, _, _ = await self.activate()
        await entity.write(False)
        self.event(projection())
        await self.advance(10)
        entity._handle_property_changed(
            SimpleNamespace(data={"event": "bridgeDisconnected"})
        )
        await self.advance(60)
        entity._handle_property_changed(SimpleNamespace(data={"event": "ready"}))
        await entity.write(False)
        self.emit()
        await self.advance(119)
        self.assertEqual(len(self.commands), 2)
        await self.advance(120)
        self.assertNotIn("command_health", entity.extra_state_attributes)
        self.assertEqual(self.commands[-1][:2], (120, False))
        entity._removed = True
        entity._light_correction.stop("closed")
        self.assertIsNone(entity._light_correction.timer)

    async def test_new_ready_accepts_new_runtime_revision_zero(self):
        entity, _, _, _ = await self.activate()
        await entity.write(False)
        self.event(projection(revision=8))
        entity._handle_property_changed(
            SimpleNamespace(data={"event": "bridgeDisconnected"})
        )
        entity._handle_property_changed(SimpleNamespace(data={"event": "ready"}))
        self.event(projection("healthy", 0, 0))
        self.assertEqual(entity.extra_state_attributes["command_health"], "healthy")

    async def test_coordinator_rejects_older_events_and_snapshot_health(self):
        coordinator = aligned.coordinator_for(Mock())
        coordinator._clear_fast_retry = Mock()
        coordinator.record_command_health("station", projection())
        coordinator.apply_command_health_event(
            {
                "event": "commandHealth",
                "sn": "station",
                "property": "light",
                **projection("healthy", 0, 0),
            }
        )
        self.assertEqual(coordinator.data["station"]["commandHealth"], projection())
        coordinator._read_snapshot = AsyncMock(
            return_value={"station": {"commandHealth": projection("healthy", 0, 0)}}
        )
        result = await coordinator._async_update_data()
        self.assertEqual(result["station"]["commandHealth"], projection())

    async def test_newer_recovery_revision_wins_over_stale_and_conflicting_events(self):
        entity, _, _, _ = await self.activate()
        await entity.write(False)
        self.emit()
        self.event(projection())
        await self.advance(5)
        self.event(projection("healthy", 0, 2))
        self.event(projection(revision=1))
        self.event(projection(revision=2))
        await self.advance(59)
        self.assertEqual(entity.extra_state_attributes["command_health"], "healthy")
        self.assertEqual(len(self.commands), 1)
        await self.advance(65)
        self.assertEqual(self.commands[-1][:2], (65, False))

    async def test_coordinator_runtime_reset_drops_inflight_old_snapshot_health(self):
        coordinator = aligned.coordinator_for(Mock())
        coordinator._clear_fast_retry = Mock()
        coordinator.record_command_health("station", projection(revision=8))

        async def snapshot():
            coordinator.apply_command_health_event({"event": "ready"})
            coordinator.apply_command_health_event(
                {
                    "event": "commandHealth",
                    "sn": "station",
                    "property": "light",
                    **projection("healthy", 0, 0),
                }
            )
            return {"station": {"commandHealth": projection(revision=8)}}

        coordinator._read_snapshot = snapshot
        result = await coordinator._async_update_data()
        self.assertEqual(
            result["station"]["commandHealth"], projection("healthy", 0, 0)
        )


class HealthRevisionProtocolTests(unittest.IsolatedAsyncioTestCase):
    """Verify schema carry and API lifecycle/order boundaries."""

    client = fixtures.bounded.BoundedProtocolTests.client

    def test_revision_is_carried_and_malformed_revision_rejects_projection(self):
        value = projection()
        self.assertEqual(command_health(value), value)
        event = {
            "event": "commandHealth",
            "sn": "fixture",
            "property": "light",
            **value,
        }
        self.assertEqual(command_health_event(event), event)
        for revision in (True, -1, 1.5, 2**53, float("nan"), "1"):
            with self.subTest(revision=revision):
                self.assertIsNone(command_health({**value, "revision": revision}))

    async def test_receive_loop_filters_reordered_and_unversioned_events(self):
        client = self.client()
        seen = []
        client._on_event = seen.append
        current = {
            "event": "commandHealth",
            "sn": "fixture",
            "property": "light",
            **projection(),
        }

        async def frames():
            for health in (
                projection(),
                projection("healthy", 0, 0),
                {"status": "healthy", "cooldownRemainingMs": 0},
            ):
                value = {
                    "event": "commandHealth",
                    "sn": "fixture",
                    "property": "light",
                    **health,
                }
                yield SimpleNamespace(
                    type=WSMsgType.TEXT, json=lambda value=value: value
                )

        client._ws.__aiter__ = lambda _: frames()
        client._closing = True
        await client._receive_loop()
        self.assertEqual(seen, [current])

    async def test_delayed_snapshot_cannot_revert_newer_api_health(self):
        client = self.client()
        seen = []
        client._on_event = seen.append

        async def frames():
            value = {
                "event": "commandHealth",
                "sn": "fixture",
                "property": "light",
                **projection(),
            }
            yield SimpleNamespace(type=WSMsgType.TEXT, json=lambda: value)
            client.rpc = AsyncMock(
                return_value={
                    "devices": [
                        {"sn": "fixture", "commandHealth": projection("healthy", 0, 0)}
                    ]
                }
            )
            self.snapshot = await client.list_devices()

        client._ws.__aiter__ = lambda _: frames()
        client._closing = True
        await client._receive_loop()
        self.assertEqual(self.snapshot[0]["commandHealth"], projection())

    async def test_old_resolved_rpc_cannot_cross_replaced_connection(self):
        client = self.client()
        old = client._ws

        async def reply(payload):
            client._pending[payload["id"]].set_result(
                {
                    "ok": True,
                    "devices": [
                        {"sn": "fixture", "commandHealth": projection("healthy", 0, 0)}
                    ],
                }
            )
            client._ws = Mock(closed=False)

        old.send_json.side_effect = reply
        with self.assertRaises(EufySdkApiClientCommunicationError):
            await client.list_devices()

    async def test_delayed_receipt_retains_raw_health_but_does_not_publish_stale_health(
        self,
    ):
        client = self.client()
        seen = []
        client._on_event = seen.append
        send = client._ws.send_json.side_effect
        receipt_health = projection("healthy", 0, 0)
        current = {
            "event": "commandHealth",
            "sn": "fixture",
            "property": "light",
            **projection(),
        }

        async def reply(payload):
            if payload["cmd"] == "device.set":
                client._dispatch_event(current)
                client._pending[payload["id"]].set_result(
                    {
                        "ok": True,
                        "receipt": {
                            "status": "acknowledged",
                            "sendAttempted": True,
                            "effect": "unconfirmed",
                            "commandHealth": receipt_health,
                        },
                    }
                )
            else:
                await send(payload)

        client._ws.send_json.side_effect = reply
        receipt = await client.set_property("fixture", "light", False, bounded=True)
        self.assertEqual(seen, [current])
        self.assertEqual(receipt.command_health, receipt_health)
        self.assertEqual(client._command_health_projections["fixture"], projection())

    async def test_ready_invalidates_inflight_old_receipt_and_resets_api_watermark(
        self,
    ):
        client = self.client()
        seen = []
        client._on_event = seen.append
        client._dispatch_event(
            {
                "event": "commandHealth",
                "sn": "fixture",
                "property": "light",
                **projection(revision=8),
            }
        )
        send = client._ws.send_json.side_effect

        async def reply(payload):
            if payload["cmd"] == "device.set":
                client._dispatch_event({"event": "ready"})
                client._pending[payload["id"]].set_result(
                    {
                        "ok": True,
                        "receipt": {
                            "status": "acknowledged",
                            "sendAttempted": True,
                            "effect": "unconfirmed",
                            "commandHealth": projection(revision=8),
                        },
                    }
                )
            else:
                await send(payload)

        client._ws.send_json.side_effect = reply
        with self.assertRaises(EufySdkApiClientCommunicationError):
            await client.set_property("fixture", "light", False, bounded=True)
        initial = {
            "event": "commandHealth",
            "sn": "fixture",
            "property": "light",
            **projection("healthy", 0, 0),
        }
        client._dispatch_event(initial)
        self.assertEqual(seen[-2:], [{"event": "ready"}, initial])
        self.assertEqual(
            client._command_health_projections["fixture"], projection("healthy", 0, 0)
        )


class HealthCapacityTests(unittest.IsolatedAsyncioTestCase):
    """Preserve active history rather than evicting it for unfamiliar targets."""

    client = fixtures.bounded.BoundedProtocolTests.client

    async def test_api_and_coordinator_health_caches_are_bounded_without_eviction(self):
        client = self.client()
        coordinator = aligned.coordinator_for(Mock())
        devices = [
            {"sn": f"fixture-{index}", "commandHealth": projection()}
            for index in range(MAX_COMMAND_HEALTH_TARGETS + 1)
        ]
        client.rpc = AsyncMock(return_value={"devices": devices})
        snapshots = await client.list_devices()
        self.assertEqual(
            len(client._command_health_projections), MAX_COMMAND_HEALTH_TARGETS
        )
        self.assertNotIn("commandHealth", snapshots[-1])
        coordinator._project_snapshot_health(
            {device["sn"]: device for device in devices},
            coordinator._command_health_generation,
        )
        self.assertEqual(
            len(coordinator._command_health_projections), MAX_COMMAND_HEALTH_TARGETS
        )
        self.assertNotIn("commandHealth", devices[-1])
        current = {
            "event": "commandHealth",
            "sn": "fixture-0",
            "property": "light",
            **projection("healthy", 0, 2),
        }
        seen = []
        client._on_event = seen.append
        client._dispatch_event(current)
        client._dispatch_event({**current, "sn": "overflow"})
        self.assertEqual(seen, [current])
        self.assertEqual(
            client._command_health_projections["fixture-0"], projection("healthy", 0, 2)
        )
        self.assertEqual(
            len(client._command_health_projections), MAX_COMMAND_HEALTH_TARGETS
        )
        coordinator.record_command_health("fixture-0", projection("healthy", 0, 2))
        self.assertEqual(
            coordinator._command_health_projections["fixture-0"],
            projection("healthy", 0, 2),
        )
        coordinator.apply_command_health_event(
            {**current, "sn": "unknown-inventory-target"}
        )
        self.assertNotIn(
            "unknown-inventory-target", coordinator._command_health_projections
        )
        self.assertEqual(
            len(coordinator._command_health_projections), MAX_COMMAND_HEALTH_TARGETS
        )

    async def test_coordinator_ignores_unknown_inventory_events_before_capacity(self):
        coordinator = aligned.coordinator_for(Mock())
        event = {
            "event": "commandHealth",
            "sn": "unknown",
            "property": "light",
            **projection(),
        }
        coordinator.apply_command_health_event(event)
        self.assertFalse(getattr(coordinator, "_command_health_projections", {}))
        coordinator.apply_command_health_event({**event, "sn": "station"})
        self.assertEqual(coordinator.data["station"]["commandHealth"], projection())
