"""Storage attempt diagnostics explain empty readings without changing quantities."""

import copy
import unittest
from unittest.mock import patch

from custom_components.eufy_sdk.storage_telemetry import FRESHNESS_MS, storage_sensors
from test_storage_telemetry import MODULE, NOW, coordinator, device


class StorageAttemptStatusTests(unittest.TestCase):
    def attributes(self, row):
        coord = coordinator({"S1": row})
        entity = storage_sensors(coord)[0]
        with patch(MODULE + "._now_ms", return_value=NOW):
            return entity.extra_state_attributes, entity.available, entity.native_value

    def test_off_waiting_failure_current_and_stale_preserve_numeric_states(self):
        for bridge_status, expected, available, value in (
            ("disabled", "off", True, None),
            ("unknown", "waiting_for_first_reading", True, None),
            ("unknown", "last_attempt_failed", True, None),
            ("current", "current", True, 1000),
            ("stale", "stale", False, None),
        ):
            with self.subTest(expected=expected):
                row = device(bridge_status)
                telemetry = row["storageTelemetry"]
                if bridge_status in ("disabled", "unknown"):
                    telemetry.update(
                        observedAtMs=None,
                        unitProfile=None,
                        correlation=None,
                        values=None,
                    )
                if bridge_status == "stale":
                    telemetry.update(observedAtMs=NOW - FRESHNESS_MS, values=None)
                telemetry.update(
                    lastAttemptAtMs=NOW - 100,
                    lastFailureCategory="busy"
                    if expected == "last_attempt_failed"
                    else None,
                    consecutiveFailures=1 if expected == "last_attempt_failed" else 0,
                    nextEligibleAtMs=NOW - 100 + 3_600_000,
                )
                attributes, actual_available, actual_value = self.attributes(row)
                self.assertEqual(attributes["storage_status"], expected)
                self.assertEqual(actual_available, available)
                self.assertEqual(actual_value, value)
                self.assertEqual(attributes["last_attempt_at_ms"], NOW - 100)
                self.assertEqual(
                    attributes["next_eligible_at_ms"], NOW - 100 + 3_600_000
                )
                self.assertEqual(
                    attributes["last_failure_category"],
                    telemetry["lastFailureCategory"],
                )

    def test_each_failure_category_is_visible_without_refreshing_saved_values(self):
        for category in (
            "busy",
            "consumed_session",
            "timeout",
            "invalid_quantity",
            "ownership_changed",
            "other",
        ):
            row = device()
            row["storageTelemetry"].update(
                lastAttemptAtMs=NOW, lastFailureCategory=category, consecutiveFailures=3
            )
            before = copy.deepcopy(row)
            attributes, available, value = self.attributes(row)
            self.assertEqual(attributes["storage_status"], "last_attempt_failed")
            self.assertEqual(attributes["last_failure_category"], category)
            self.assertEqual(attributes["consecutive_failures"], 3)
            self.assertEqual(attributes["observed_at_ms"], NOW - 1000)
            self.assertTrue(available)
            self.assertEqual(value, 1000)
            self.assertEqual(row, before)

    def test_old_bridge_and_local_expiry_still_work_without_attempt_fields(self):
        for status, expected in (
            ("disabled", "off"),
            ("unknown", "waiting_for_first_reading"),
            ("current", "current"),
            ("stale", "stale"),
        ):
            attributes, _, _ = self.attributes(device(status))
            self.assertEqual(attributes["storage_status"], expected)
            self.assertIsNone(attributes["last_attempt_at_ms"])
            self.assertIsNone(attributes["next_eligible_at_ms"])
            self.assertIsNone(attributes["last_failure_category"])
            self.assertEqual(attributes["consecutive_failures"], 0)
        attributes, available, value = self.attributes(
            device("current", NOW - FRESHNESS_MS)
        )
        self.assertEqual(attributes["storage_status"], "stale")
        self.assertFalse(available)
        self.assertIsNone(value)

    def test_fractional_next_attempt_time_is_truncated(self):
        row = device()
        row["storageTelemetry"].update(
            lastAttemptAtMs=NOW, consecutiveFailures=0, nextEligibleAtMs=NOW + 0.48
        )
        attributes, _, _ = self.attributes(row)
        self.assertEqual(attributes["next_eligible_at_ms"], NOW)
        self.assertIs(type(attributes["next_eligible_at_ms"]), int)
        for bad in (float("nan"), float("inf"), -1.5):
            row["storageTelemetry"]["nextEligibleAtMs"] = bad
            self.assertIsNone(self.attributes(row)[0]["next_eligible_at_ms"])

    def test_malformed_diagnostics_cannot_expose_raw_details_or_change_values(self):
        row = device()
        row["storageTelemetry"].update(
            lastAttemptAtMs=NOW,
            lastFailureCategory="private-payload-token-serial",
            consecutiveFailures=1,
            nextEligibleAtMs="private-next-attempt",
        )
        attributes, available, value = self.attributes(row)
        self.assertEqual(attributes["last_failure_category"], "other")
        self.assertIsNone(attributes["next_eligible_at_ms"])
        self.assertNotIn("private-", str(attributes))
        self.assertTrue(available)
        self.assertEqual(value, 1000)
        for bad in (True, -1, 1 << 53, "1", None, {}, []):
            row["storageTelemetry"].update(
                lastAttemptAtMs=bad,
                consecutiveFailures=bad,
                nextEligibleAtMs=bad,
                lastFailureCategory=[],
            )
            attributes, available, value = self.attributes(row)
            self.assertEqual(attributes["storage_status"], "current")
            self.assertIsNone(attributes["last_attempt_at_ms"])
            self.assertIsNone(attributes["next_eligible_at_ms"])
            self.assertEqual(attributes["consecutive_failures"], 0)
            self.assertTrue(available)
            self.assertEqual(value, 1000)

    def test_disconnection_hides_diagnostics_and_does_not_request_storage(self):
        coord = coordinator()
        coord.data["S1"]["storageTelemetry"].update(
            lastAttemptAtMs=NOW, lastFailureCategory="timeout", consecutiveFailures=1
        )
        entity = storage_sensors(coord)[0]
        coord.last_update_success = False
        with patch(MODULE + "._now_ms", return_value=NOW):
            attributes = entity.extra_state_attributes
            self.assertEqual(attributes["storage_status"], "unavailable")
            self.assertIsNone(attributes["last_attempt_at_ms"])
            self.assertIsNone(attributes["last_failure_category"])
            self.assertFalse(entity.available)
            self.assertIsNone(entity.native_value)
        self.assertEqual(coord.config_entry.runtime_data.client.mock_calls, [])
