"""State presentation stays bounded by scalar data, independent of payload size."""

# ruff: noqa: ANN001, ANN201, ANN204, D101, D102, D105, EM101, INP001, PT009, TRY003

# Added for genomez S1 experimental source preview, 2026-10-17.

import copy
import json
import time
import unittest
from unittest.mock import patch

from test_storage_telemetry import NOW, coordinator, device

from custom_components.eufy_sdk import storage_telemetry as storage
from custom_components.eufy_sdk.binary_sensor import (
    EufyPushBinarySensor,
    EufySdkBinarySensor,
)
from custom_components.eufy_sdk.select import EufySdkSnapshotPolicySelect


class UnreadPayload:
    """Fail if a getter walks or copies unrelated large snapshot data."""

    def __iter__(self):
        raise AssertionError("Unrelated payload must not be scanned")

    def __deepcopy__(self, memo):
        raise AssertionError("Unrelated payload must not be copied")


class StoragePresentationCostTests(unittest.TestCase):
    def test_storage_getters_do_no_io_serialization_or_payload_scans(self):
        coord = coordinator()
        coord.data["S1"]["history"] = UnreadPayload()
        with (
            patch.object(storage, "_now_ms", return_value=NOW),
            patch.object(json, "dumps", side_effect=AssertionError("JSON in getter")),
            patch.object(json, "loads", side_effect=AssertionError("JSON in getter")),
            patch.object(
                copy, "deepcopy", side_effect=AssertionError("Copy in getter")
            ),
            patch("builtins.open", side_effect=AssertionError("File I/O in getter")),
        ):
            entity = storage.storage_sensors(coord)[0]
            attributes = entity.extra_state_attributes
            for _ in range(100):
                self.assertTrue(entity.available)
                self.assertEqual(entity.native_value, 1000)
                self.assertEqual(entity.extra_state_attributes, attributes)
        self.assertEqual(coord.config_entry.runtime_data.client.mock_calls, [])

    def test_in_place_updates_are_revalidated_in_getters(self):
        coord = coordinator()
        entity = storage.storage_sensors(coord)[0]
        with patch.object(storage, "_now_ms", return_value=NOW):
            self.assertEqual(entity.native_value, 1000)
            coord.data["S1"]["storageTelemetry"]["values"].update(
                total_bytes=2000, available_bytes=1250, used_percent=37.5
            )

            self.assertTrue(entity.available)
            self.assertEqual(entity.native_value, 2000)
            coord.data["S1"]["storageTelemetry"]["supported"] = 1
            self.assertFalse(entity.available)
            self.assertIsNone(entity.native_value)

    def test_expiry_guard_works_even_before_timer_callback_runs(self):
        entity = storage.storage_sensors(coordinator())[0]
        with patch.object(storage, "_now_ms", return_value=NOW):
            self.assertEqual(entity.native_value, 1000)
        with patch.object(
            storage, "_now_ms", return_value=NOW - 1000 + storage.FRESHNESS_MS
        ):
            self.assertFalse(entity.available)
            self.assertIsNone(entity.native_value)
            self.assertEqual(entity.extra_state_attributes["storage_status"], "stale")
            self.assertEqual(
                entity.extra_state_attributes["observed_at_ms"], NOW - 1000
            )

    def test_record_and_telemetry_replacement_keep_live_validation(self):
        coord = coordinator()
        entity = storage.storage_sensors(coord)[0]
        with patch.object(storage, "_now_ms", return_value=NOW):
            self.assertEqual(entity.native_value, 1000)
            coord.data["S1"] = device("disabled", None)
            self.assertIsNone(entity.native_value)
            self.assertEqual(entity.extra_state_attributes["storage_status"], "off")
            coord.data["S1"]["storageTelemetry"] = device()["storageTelemetry"]
            self.assertEqual(entity.native_value, 1000)

    def test_disconnect_skips_validation_and_hides_values_and_diagnostics(self):
        coord = coordinator()
        entity = storage.storage_sensors(coord)[0]
        with patch.object(storage, "_now_ms", return_value=NOW):
            self.assertEqual(entity.native_value, 1000)
            coord.last_update_success = False
            with (
                patch.object(
                    storage,
                    "storage_reading",
                    side_effect=AssertionError("Disconnected validation"),
                ),
                patch.object(
                    storage,
                    "storage_observed_at",
                    side_effect=AssertionError("Disconnected validation"),
                ),
            ):
                self.assertFalse(entity.available)
                self.assertIsNone(entity.native_value)
                self.assertIsNone(entity.extra_state_attributes["observed_at_ms"])
            coord.last_update_success = True
            self.assertEqual(entity.native_value, 1000)

    def test_clock_rollback_and_future_observation_cannot_revive_early(self):
        entity = storage.storage_sensors(coordinator())[0]
        with patch.object(storage, "_now_ms", return_value=NOW):
            self.assertEqual(entity.native_value, 1000)
        with patch.object(storage, "_now_ms", return_value=NOW - 2000):
            self.assertIsNone(entity.native_value)
            self.assertFalse(entity.available)
        with patch.object(storage, "_now_ms", return_value=NOW - 1000):
            self.assertEqual(entity.native_value, 1000)

    def test_timestamp_conversion_is_reused_between_updates(self):
        entity = storage.storage_sensors(coordinator())[-1]
        with patch.object(storage, "_now_ms", return_value=NOW):
            first = entity.native_value
            for _ in range(100):
                self.assertIs(entity.native_value, first)
            entity.coordinator.data["S1"]["storageTelemetry"]["observedAtMs"] = NOW
            self.assertIsNot(entity.native_value, first)
            entity.coordinator.data["S1"]["storageTelemetry"]["unitProfile"] = "wrong"
            self.assertIsNone(entity.native_value)


class OtherPresentationCostTests(unittest.TestCase):
    def test_binary_push_and_policy_getters_do_no_io_serialization_or_scans(self):
        coord = coordinator()
        coord.data["S1"].update(
            history=UnreadPayload(), state={"solarConnected24h": True}
        )
        binary = EufySdkBinarySensor(
            coord, "S1", {"name": "solarConnected24h", "type": "bool"}
        )
        push = EufyPushBinarySensor(
            coord, "S1", frozenset({"motion"}), ("motion", "Motion", None)
        )
        policy = EufySdkSnapshotPolicySelect(coord, "S1")
        with (
            patch.object(json, "dumps", side_effect=AssertionError("JSON in getter")),
            patch.object(json, "loads", side_effect=AssertionError("JSON in getter")),
            patch.object(
                copy, "deepcopy", side_effect=AssertionError("Copy in getter")
            ),
            patch("builtins.open", side_effect=AssertionError("File I/O in getter")),
        ):
            for _ in range(100):
                self.assertTrue(binary.available)
                self.assertTrue(binary.is_on)
                self.assertTrue(push.available)
                self.assertFalse(push.is_on)
                self.assertTrue(policy.available)
                self.assertEqual(policy.current_option, "default")
        self.assertEqual(coord.config_entry.runtime_data.client.mock_calls, [])

    def test_warm_getter_timing_evidence(self):
        """Record local cost; structural checks avoid machine-speed assertions."""
        coord = coordinator()
        entities = {
            "storage": storage.storage_sensors(coord)[0],
            "binary": EufySdkBinarySensor(
                coord, "S1", {"name": "solarConnected24h", "type": "bool"}
            ),
            "push": EufyPushBinarySensor(
                coord, "S1", frozenset({"motion"}), ("motion", "Motion", None)
            ),
            "policy": EufySdkSnapshotPolicySelect(coord, "S1"),
        }
        measurements = {}
        with patch.object(storage, "_now_ms", new=lambda: NOW):
            for kind, entity in entities.items():

                def read(entity=entity, kind=kind) -> object:
                    _ = entity.available, entity.extra_state_attributes
                    if kind == "storage":
                        return entity.native_value
                    if kind == "policy":
                        return entity.current_option
                    return entity.is_on

                read()
                start = time.perf_counter()
                for _ in range(10_000):
                    read()
                measurements[kind] = round(
                    (time.perf_counter() - start) * 1000 / 10_000, 6
                )
        print(  # noqa: T201 - expose timing evidence in the offline test log
            "Warm availability/state/attribute getters, ms per read: "
            + str(measurements)
        )
