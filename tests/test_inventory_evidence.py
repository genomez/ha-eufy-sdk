"""Roster retention and diagnostic quality remain separate from scalar state."""

# ruff: noqa: ANN001, ANN201, D102, INP001, PT009, SLF001
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from custom_components.eufy_sdk.api import EufySdkApiClient
from custom_components.eufy_sdk.coordinator import EufySdkDataUpdateCoordinator
from custom_components.eufy_sdk.entity import EufySdkDeviceEntity
from custom_components.eufy_sdk.inventory_evidence import (
    inventory_attributes,
    inventory_evidence,
)


def evidence(quality="partial"):
    """Build synthetic inventory quality with one retained row."""
    return {
        "version": 1,
        "generation": 2,
        "completedAtMs": 2000,
        "quality": quality,
        "attempted": 3,
        "failed": 1,
        "lastCompleteSuccessMs": 1000,
        "rows": {
            "synthetic-a": {"quality": "fresh", "lastSuccessfulObservationMs": 2000},
            "synthetic-b": {"quality": "retained", "lastSuccessfulObservationMs": 1000},
        },
    }


class InventoryEvidenceTests(unittest.IsolatedAsyncioTestCase):
    """Preserve entities and original successful inventory observations."""

    async def test_refresh_timestamps_are_excluded_from_recorder(self):
        entity = EufySdkDeviceEntity(
            SimpleNamespace(data={"synthetic-b": {}}, inventory_evidence=evidence()),
            "synthetic-b",
        )
        attrs = entity.extra_state_attributes
        self.assertIn("inventory_age_seconds", attrs)
        self.assertEqual(attrs["inventory_last_success_ms"], 1000)
        self.assertTrue(
            {"inventory_age_seconds", "inventory_last_success_ms"}
            <= entity._unrecorded_attributes
        )
        self.assertNotIn("inventory_quality", entity._unrecorded_attributes)
        self.assertNotIn("inventory_refresh_quality", entity._unrecorded_attributes)

    async def test_api_keeps_atomic_evidence_separate_from_property_state(self):
        client = EufySdkApiClient("synthetic", 3000, Mock())
        client._ws = Mock(closed=False)
        client.rpc = AsyncMock(
            return_value={
                "devices": [
                    {"sn": "synthetic-a", "state": {"light": False}},
                    {"sn": "synthetic-b", "state": {"light": True}},
                ],
                "inventoryEvidence": evidence(),
            }
        )
        devices = await client.list_devices()
        self.assertEqual(devices[1]["state"]["light"], True)
        self.assertEqual(
            client.inventory_evidence["rows"]["synthetic-b"]["quality"], "retained"
        )
        self.assertNotIn("inventoryEvidence", devices[1]["state"])
        client.rpc.return_value = {"devices": devices}
        await client.list_devices()
        self.assertIsNone(client.inventory_evidence)

    async def test_partial_and_retained_absence_preserve_entities_and_topology(self):
        for quality in ("partial", "retained"):
            coordinator = EufySdkDataUpdateCoordinator.__new__(
                EufySdkDataUpdateCoordinator
            )
            coordinator.data = {
                "synthetic-b": {
                    "stationSn": "synthetic-a",
                    "model": "T8425",
                    "state": {"light": True},
                }
            }
            coordinator._inventory_rows = {
                "synthetic-b": {"quality": "fresh", "lastSuccessfulObservationMs": 1000}
            }
            snapshot = {"synthetic-a": {"model": "T9000"}}
            client = SimpleNamespace(inventory_evidence=evidence(quality))
            coordinator._apply_inventory_snapshot(snapshot, client)
            self.assertEqual(snapshot["synthetic-b"], coordinator.data["synthetic-b"])
            self.assertEqual(
                coordinator.inventory_evidence["rows"]["synthetic-b"],
                {"quality": "retained", "lastSuccessfulObservationMs": 1000},
            )
            self.assertEqual(
                inventory_attributes(
                    coordinator.inventory_evidence, "synthetic-b", 5000
                )["inventory_age_seconds"],
                4,
            )

    async def test_complete_absence_keeps_existing_removal_policy(self):
        coordinator = EufySdkDataUpdateCoordinator.__new__(EufySdkDataUpdateCoordinator)
        coordinator.data = {"synthetic-b": {}}
        snapshot = {}
        coordinator._apply_inventory_snapshot(
            snapshot, SimpleNamespace(inventory_evidence=evidence("complete"))
        )
        self.assertEqual(snapshot, {})

    async def test_malformed_missing_and_clock_rollback_are_unknown_or_retained(self):
        for raw in (
            None,
            {},
            evidence() | {"quality": []},
            evidence() | {"failed": True},
            evidence() | {"version": True},
            evidence() | {"completedAtMs": -1},
        ):
            self.assertIsNone(inventory_evidence(raw, [{"sn": "synthetic-b"}]))
        attrs = inventory_attributes(evidence(), "synthetic-b", 500)
        self.assertIsNone(attrs["inventory_age_seconds"])
        self.assertEqual(attrs["inventory_quality"], "retained")
        self.assertEqual(
            inventory_attributes(None, "synthetic-b", 5000)["inventory_quality"],
            "unknown",
        )

    async def test_projection_drops_unbounded_keys_and_private_payload(self):
        raw = evidence() | {
            "message": "PRIVATE",
            "rows": evidence()["rows"]
            | {"PRIVATE": {"quality": "fresh", "lastSuccessfulObservationMs": 1}},
        }
        projected = inventory_evidence(raw, [{"sn": "synthetic-b"}])
        self.assertNotIn("PRIVATE", str(projected))
        self.assertEqual(len(projected["rows"]), 1)
