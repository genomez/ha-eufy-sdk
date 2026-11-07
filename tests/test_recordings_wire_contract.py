# ruff: noqa: ANN201, D101, D102, INP001, PLR2004, PT009, PT027
"""Offline bridge integer and variable-window contract regressions."""

import unittest
from copy import deepcopy

from fixtures import recordings_v1 as fixture
from test_recordings_progressive import playback

from custom_components.eufy_sdk import recordings_models as models
from custom_components.eufy_sdk.media_source import folder_status


def diagnostic_payload() -> dict:
    """Build the legacy cache-only diagnostic shape."""
    return {
        "rowsKeptPerCamera": [],
        "rejected": dict.fromkeys(
            ("not_in_scope", "bad_record_id", "bad_start_time", "other"), 0
        ),
        "identity_conflict": 0,
        "timeStatus": dict.fromkeys(
            ("valid", "unknown_format", "ambiguous", "invalid"), 0
        ),
        "pagingStatus": "unverified",
        "lastPageCount": 20,
        "lastCatalogTime": None,
        "coverCache": dict.fromkeys(("files", "bytes", "pending"), 0),
        "unknownStartTimeShapes": [],
    }


class WireContractTests(unittest.TestCase):
    def test_variable_window_and_exact_disclosure(self):
        for window in (1, 20, 50, 100, 87381):
            disclosure = f"latest {window} station-wide; history incomplete"
            page = {**fixture.page(), "stationWindow": window, "disclosure": disclosure}
            self.assertEqual(
                models.catalog(page, fixture.SCOPE, fixture.CAMERA)["stationWindow"],
                window,
            )
            capability = {
                **fixture.capability(),
                "stationWindow": window,
                "disclosure": disclosure,
            }
            self.assertIsNotNone(models.capability(capability))
            if window != 20:
                self.assertIn(disclosure, folder_status(page, empty=False))
            else:
                self.assertEqual(disclosure, models.DISCLOSURE)
            for bad in (None, "latest 20", disclosure + " "):
                with self.subTest(window=window, disclosure=bad):
                    page["disclosure"] = bad
                    with self.assertRaises(models.RecordingsError):
                        models.catalog(page, fixture.SCOPE, fixture.CAMERA)
                    self.assertIsNone(
                        models.capability({**capability, "disclosure": bad})
                    )

    def test_window_bounds_and_legacy_capability(self):
        self.assertIsNotNone(models.capability(fixture.capability()))
        models.catalog(fixture.page(), fixture.SCOPE, fixture.CAMERA)
        for window in (True, False, 0, -1, 87382, 20.0, 50.25, "50", None):
            with self.subTest(window=window):
                self.assertIsNone(
                    models.capability({**fixture.capability(), "stationWindow": window})
                )
                with self.assertRaises(models.RecordingsError):
                    models.catalog(
                        {**fixture.page(), "stationWindow": window},
                        fixture.SCOPE,
                        fixture.CAMERA,
                    )
        self.assertIsNone(
            models.capability({**fixture.capability(), "stationWindow": 50})
        )
        self.assertIsNone(models.capability({**fixture.capability(), "pageSize": 50}))
        rows = [fixture.event("e_" + str(index).zfill(22)) for index in range(101)]
        page = {
            **fixture.page(rows),
            "stationWindow": 87381,
            "disclosure": models.disclosure(87381),
        }
        with self.assertRaises(models.RecordingsError):
            models.catalog(page, fixture.SCOPE, fixture.CAMERA)

    def test_diagnostic_count_bounds_with_and_without_advertised_window(self):
        for window in (1, 20, 50, 87381):
            payload = {
                **diagnostic_payload(),
                "stationWindow": window,
                "disclosure": models.disclosure(window),
                "lastPageCount": window,
            }
            self.assertEqual(models.diagnostics(payload)["lastPageCount"], window)
            for count in (window + 1, -1, float(window), True):
                with (
                    self.subTest(window=window, count=count),
                    self.assertRaises(models.RecordingsError),
                ):
                    models.diagnostics({**payload, "lastPageCount": count})
        legacy = diagnostic_payload()
        self.assertEqual(models.diagnostics(legacy)["lastPageCount"], 20)
        self.assertEqual(
            models.diagnostics({**legacy, "lastPageCount": 87381})["lastPageCount"],
            87381,
        )
        with self.assertRaises(models.RecordingsError):
            models.diagnostics({**legacy, "lastPageCount": 87382})

    def test_catalog_millisecond_fields_still_reject_floats(self):
        for field in (
            "lastSuccessfulQueryAtMs",
            "lastAttemptAtMs",
            "browseExpiresAtMs",
            "retryAfterMs",
        ):
            for value in (60999.75, 61000.0):
                page = deepcopy(fixture.page())
                target = page["refresh"] if field == "retryAfterMs" else page
                target[field] = value
                with (
                    self.subTest(field=field, value=value),
                    self.assertRaises(models.RecordingsError),
                ):
                    models.catalog(page, fixture.SCOPE, fixture.CAMERA)

    def test_playback_millisecond_fields_still_reject_floats(self):
        for path in (
            ("hardExpiresAtMs",),
            ("segments", 0, "sequence"),
            ("segments", 0, "startMs"),
            ("segments", 0, "durationMs"),
            ("segments", 0, "bytes"),
            ("clip", "progress", "pulledMs"),
            ("clip", "progress", "expectedMs"),
            ("clip", "durationMs"),
            ("clip", "expiresAtMs"),
        ):
            payload = playback()
            target = payload["playback"]
            for key in path[:-1]:
                target = target[key]
            original = target[path[-1]]
            for value in (float(original or 0), 0.25):
                target[path[-1]] = value
                with (
                    self.subTest(path=path, value=value),
                    self.assertRaises(models.RecordingsError),
                ):
                    models.playback(payload)
