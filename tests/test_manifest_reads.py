# ruff: noqa: ANN201, D100, D101, D102, INP001, PT009, SLF001

import json
import runpy
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from custom_components.eufy_sdk.api import EufySdkApiClient
from custom_components.eufy_sdk.coordinator import EufySdkDataUpdateCoordinator
from custom_components.eufy_sdk.manifest_reads import (
    normalize_properties,
    normalize_snapshot,
)
from custom_components.eufy_sdk.property_sync import apply_property_event


def manifest(*reads: dict) -> dict:
    """Describe synthetic camera reads without device or account data."""
    return {
        "bound": True,
        "details": [
            {"capability": "camera", "accessor": "camera", "reads": list(reads)}
        ],
    }


QUALITY = {
    "property": "recordingQuality",
    "accessor": "recordingQuality",
    "type": "string",
    "kind": "enum",
    "values": [0, 1, 2],
    "labels": {"0": "HD", "1": "Full HD", "2": "Max"},
    "writable": True,
}
LIGHT = {"property": "light", "accessor": "light", "type": "bool", "writable": True}


def snapshot(value: object = 0) -> dict:
    """One raw structured setting and its decoded getter result."""
    return {
        "sn": "synthetic-camera",
        "state": {"recordingQuality": '{"cur_mode":0}', "light": "1", "cameraInfo": {}},
        "decodedProperties": manifest(QUALITY, LIGHT),
        "decodedState": {"camera": {"recordingQuality": value, "light": False}},
    }


class ManifestReadTests(unittest.TestCase):
    def test_fixture_is_reproducible_from_offline_synthetic_construction(self):
        fixtures = Path(__file__).parent / "fixtures"
        generator = runpy.run_path(str(fixtures / "generate_manifest_readings.py"))
        fixture = json.loads(
            (fixtures / "manifest_sdk_readings.json").read_text(encoding="utf-8")
        )
        self.assertEqual(fixture, generator["build_cases"]())

    def test_synthetic_sdk_manifests_keep_hub_and_floodlight_values_and_controls(self):
        # Regenerated offline by fixtures/generate_manifest_readings.py; metadata
        # is SDK contract schema and all readings are synthetic construction.
        fixture = Path(__file__).parent / "fixtures" / "manifest_sdk_readings.json"
        for case in json.loads(fixture.read_text(encoding="utf-8")):
            with self.subTest(case=case["case"]):
                normalized = normalize_snapshot(case["snapshot"])["state"]
                for name, value in case["previous"].items():
                    self.assertEqual(normalized[name], value)
                    self.assertIs(type(normalized[name]), type(value))
                properties = normalize_properties(
                    {
                        "properties": case["properties"],
                        "decodedProperties": case["snapshot"]["decodedProperties"],
                    }
                )
                by_name = {prop["name"]: prop for prop in properties}
                if case["case"].startswith("T9000"):
                    self.assertTrue(by_name["armingMode"]["writable"])
                    self.assertEqual(
                        by_name["armingMode"]["enumValues"]["3"], "custom1"
                    )
                    self.assertFalse(by_name["battery"]["writable"])
                    self.assertNotIn("currentMode", case["previous"])
                else:
                    self.assertTrue(by_name["light"]["writable"])
                    self.assertEqual(by_name["light"]["type"], "bool")

    def test_numeric_enum_without_domain_and_oversize_integer(self):
        read = {
            "property": "armingMode",
            "accessor": "mode",
            "type": "enum",
            "writable": True,
        }
        for value, expected in (
            (0, 0),
            (3, 3),
            ("3", None),
            (False, None),
            (10**400, None),
        ):
            source = {
                "state": {"armingMode": value},
                "decodedProperties": manifest(read),
                "decodedState": {"camera": {"mode": value}},
            }
            self.assertEqual(
                normalize_snapshot(source)["state"]["armingMode"], expected
            )
        result = normalize_properties(
            {
                "properties": [{"name": "armingMode", "type": "enum"}],
                "decodedProperties": manifest(read),
            }
        )
        self.assertEqual(result[0]["type"], "number")

    def test_zero_false_and_original_raw_snapshot_preserved(self):
        source = snapshot()
        original = deepcopy(source)
        result = normalize_snapshot(source)
        self.assertEqual(
            result["state"], {"recordingQuality": 0, "light": False, "cameraInfo": None}
        )
        self.assertEqual(source, original)

    def test_unknown_malformed_and_out_of_domain_never_fall_back_to_raw(self):
        for value in (None, "0", True, {}, [], float("nan"), float("inf"), 99):
            self.assertIsNone(
                normalize_snapshot(snapshot(value))["state"]["recordingQuality"]
            )
        for decoded in ({}, {"camera": {}}, None, []):
            source = snapshot()
            source["decodedState"] = decoded
            self.assertIsNone(normalize_snapshot(source)["state"]["recordingQuality"])

    def test_partial_additive_protocol_is_unknown_not_raw(self):
        for missing in ("decodedProperties", "decodedState"):
            source = snapshot()
            source.pop(missing)
            self.assertIsNone(normalize_snapshot(source)["state"]["recordingQuality"])

    def test_namespace_collision_is_unknown_and_not_writable(self):
        source = snapshot()
        source["decodedProperties"]["details"].append(
            {"accessor": "video", "reads": [QUALITY]}
        )
        source["decodedState"]["video"] = {"recordingQuality": 2}
        self.assertIsNone(normalize_snapshot(source)["state"]["recordingQuality"])
        specs = normalize_properties(
            {
                "properties": [
                    {"name": "recordingQuality", "type": "string", "writable": True}
                ],
                "decodedProperties": source["decodedProperties"],
            }
        )
        self.assertFalse(specs[0]["writable"])

    def test_typed_metadata_labels_and_write_only_survive(self):
        properties = [
            {"name": "recordingQuality", "type": "string", "raw": True},
            {
                "name": "hubAlarmVolume",
                "type": "number",
                "writeOnly": True,
                "writable": True,
                "min": 0,
                "max": 100,
            },
            {"name": "missingReading", "type": "number", "writable": False},
            {"name": "cameraInfo", "type": "string", "raw": True},
        ]
        result = normalize_properties(
            {"properties": properties, "decodedProperties": manifest(QUALITY)}
        )
        self.assertEqual(result[0]["name"], "recordingQuality")
        self.assertEqual(result[0]["type"], "number")
        self.assertEqual(result[0]["enumValues"], QUALITY["labels"])
        self.assertNotIn("raw", result[0])
        self.assertTrue(result[0]["writable"])
        self.assertEqual(result[1:], properties[1:])

    def test_legacy_bridge_rollback_keeps_previous_contract(self):
        source = {"sn": "synthetic-camera", "state": {"recordingQuality": 0}}
        self.assertIs(normalize_snapshot(source), source)
        properties = [{"name": "recordingQuality", "type": "number"}]
        self.assertIs(normalize_properties({"properties": properties}), properties)


class ManifestApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_api_boundary_normalizes_without_extra_rpc(self):
        client = EufySdkApiClient("synthetic-bridge", 3000, Mock())
        client.rpc = AsyncMock(return_value={"devices": [snapshot()]})
        result = await client.list_devices()
        self.assertEqual(result[0]["state"]["recordingQuality"], 0)
        client.rpc.assert_awaited_once_with("devices.list")
        client.rpc = AsyncMock(
            return_value={
                "properties": [{"name": "recordingQuality", "type": "string"}],
                "decodedProperties": manifest(QUALITY),
            }
        )
        self.assertEqual(
            (await client.get_properties("synthetic-camera"))[0]["type"], "number"
        )
        client.rpc.assert_awaited_once_with("device.properties", sn="synthetic-camera")

    async def test_realtime_during_raw_snapshot_wins_after_normalization(self):
        client = EufySdkApiClient("synthetic-bridge", 3000, Mock())
        client._ws = SimpleNamespace(closed=False)
        client.auth_status = AsyncMock(return_value={"state": "ok"})
        client.list_solix_devices = AsyncMock(return_value=[])
        coordinator = object.__new__(EufySdkDataUpdateCoordinator)
        coordinator.config_entry = SimpleNamespace(
            runtime_data=SimpleNamespace(client=client)
        )
        coordinator.hass = Mock()
        coordinator.async_update_listeners = Mock()
        coordinator.data = {
            "synthetic-camera": {"state": {"recordingQuality": 2, "light": True}}
        }

        async def rpc(command: str) -> dict:
            self.assertEqual(command, "devices.list")
            apply_property_event(
                coordinator,
                {
                    "event": "propertyChanged",
                    "deviceSn": "synthetic-camera",
                    "property": "recordingQuality",
                    "value": 1,
                },
            )
            apply_property_event(
                coordinator,
                {
                    "event": "propertyChanged",
                    "deviceSn": "synthetic-camera",
                    "property": "light",
                    "value": True,
                },
            )
            return {"devices": [snapshot()]}

        client.rpc = rpc
        result = await coordinator._async_update_data()
        self.assertEqual(result["synthetic-camera"]["state"]["recordingQuality"], 1)
        self.assertTrue(result["synthetic-camera"]["state"]["light"])
