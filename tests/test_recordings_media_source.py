# ruff: noqa: ANN201, D100, D101, D102, INP001, PT009, PT027
import unittest
from datetime import UTC, datetime, timedelta, tzinfo
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import unquote
from zoneinfo import ZoneInfo

from fixtures import recordings_v1 as fixture
from homeassistant.components.media_player import BrowseError
from homeassistant.components.media_source.error import Unresolvable

from custom_components.eufy_sdk.media_source import (
    EufyRecordingMediaSource,
    folder_id,
    navigation_path,
    parse_identifier,
    title,
)
from custom_components.eufy_sdk.recordings_models import DISCLOSURE

TIMEZONE = fixture.TIMEZONE

MEDIA_LOGGER = "custom_components.eufy_sdk.media_source"


def timed_event(letter: str, timestamp: str):
    """Keep all wire timestamps consistent around midnight and DST changes."""
    utc = datetime.fromisoformat(timestamp).astimezone(UTC)
    local = utc.astimezone(ZoneInfo(TIMEZONE))
    return {
        **fixture.event("e_" + letter * 22, playable=True),
        "startedAtMs": int(utc.timestamp() * 1000),
        "endedAtMs": int(utc.timestamp() * 1000) + 10000,
        "startedAtUtc": utc.isoformat().replace("+00:00", "Z"),
        "startedAtLocal": local.isoformat(),
        "sourceStartLocal": local.strftime("%Y-%m-%d %H:%M:%S"),
    }


def unknown_event(event_id: str = fixture.OTHER, status: str = "unknown_format"):
    """Build an unknown-time event using only synthetic metadata."""
    return {
        **fixture.event(event_id, playable=True),
        "startedAtMs": None,
        "endedAtMs": None,
        "startedAtLocal": None,
        "startedAtUtc": None,
        "sourceStartLocal": "2026-11-01 01:30:00" if status == "ambiguous" else None,
        "timeStatus": status,
        "stationPresence": "seen_in_latest_window",
    }


class MediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_unsupported_camera_has_one_inert_information_item(self):
        for rows in ([], [fixture.event(playable=False)]):
            self.runtime.client.rpc.return_value = {
                "ok": True,
                "recordings": {**fixture.page(rows), "playbackSupported": False},
            }
            self.runtime.client.rpc.reset_mock()
            result = await self.source.async_browse_media(
                SimpleNamespace(identifier=f"s/{fixture.SCOPE}/c/{fixture.CAMERA}")
            )
            self.assertEqual(len(result.children), 1)
            info = result.children[0]
            self.assertEqual(info.title, "Playback not yet supported for this camera")
            self.assertFalse(info.can_play)
            self.assertFalse(info.can_expand)
            self.assertEqual(info.media_content_type, "")
            self.assertIsNone(info.thumbnail)
            self.assertEqual(info.children, [])
            self.runtime.client.rpc.assert_awaited_once_with(
                "recordings.catalog",
                timeout=3,
                cameraId=fixture.CAMERA,
                refresh="if_due",
            )
            self.runtime.client.rpc.reset_mock()
            item = SimpleNamespace(
                identifier=info.media_content_id.removeprefix(
                    "media-source://eufy_sdk/"
                )
            )
            with self.assertRaises(BrowseError):
                await self.source.async_browse_media(item)
            with self.assertRaises(Unresolvable):
                await self.source.async_resolve_media(item)
            self.runtime.client.rpc.assert_not_awaited()

    async def test_supported_empty_camera_has_no_recordings_item(self):
        self.runtime.client.rpc.return_value = {
            "ok": True,
            "recordings": {**fixture.page([]), "playbackSupported": True},
        }
        result = await self.source.async_browse_media(
            SimpleNamespace(identifier=f"s/{fixture.SCOPE}/c/{fixture.CAMERA}")
        )
        self.assertEqual([row.title for row in result.children], ["No recordings yet"])
        info = result.children[0]
        self.assertFalse(info.can_play)
        self.assertFalse(info.can_expand)
        self.assertIsNone(info.thumbnail)
        self.runtime.client.rpc.reset_mock()
        item = SimpleNamespace(
            identifier=info.media_content_id.removeprefix("media-source://eufy_sdk/")
        )
        with self.assertRaises(BrowseError):
            await self.source.async_browse_media(item)
        with self.assertRaises(Unresolvable):
            await self.source.async_resolve_media(item)
        self.runtime.client.rpc.assert_not_awaited()

    async def test_supported_camera_with_only_unplayable_rows_has_notice(self):
        self.runtime.client.rpc.return_value = {
            "ok": True,
            "recordings": {
                **fixture.page([fixture.event(playable=False)]),
                "playbackSupported": True,
            },
        }
        result = await self.source.async_browse_media(
            SimpleNamespace(identifier=f"s/{fixture.SCOPE}/c/{fixture.CAMERA}")
        )
        self.assertEqual(
            [row.title for row in result.children], ["No playable recordings yet"]
        )
        info = result.children[0]
        self.assertFalse(info.can_play)
        self.assertFalse(info.can_expand)
        self.assertIsNone(info.thumbnail)

    async def test_older_bridge_empty_camera_keeps_existing_behavior(self):
        for rows in ([], [fixture.event(playable=False)]):
            self.runtime.client.rpc.return_value = {
                "ok": True,
                "recordings": fixture.page(rows),
            }
            result = await self.source.async_browse_media(
                SimpleNamespace(identifier=f"s/{fixture.SCOPE}/c/{fixture.CAMERA}")
            )
            self.assertEqual(result.children, [])

    async def test_model_policy_change_restores_normal_rows(self):
        item = SimpleNamespace(identifier=f"s/{fixture.SCOPE}/c/{fixture.CAMERA}")
        self.runtime.client.rpc.return_value = {
            "ok": True,
            "recordings": {
                **fixture.page([fixture.event(playable=False)]),
                "playbackSupported": False,
            },
        }
        self.assertEqual(
            (await self.source.async_browse_media(item)).children[0].title,
            "Playback not yet supported for this camera",
        )
        self.runtime.client.rpc.return_value = {
            "ok": True,
            "recordings": {
                **fixture.page([fixture.event(playable=True)]),
                "playbackSupported": True,
            },
        }
        result = await self.source.async_browse_media(item)
        self.assertEqual(len(result.children), 1)
        self.assertTrue(result.children[0].can_expand)
        self.assertIn("/d/", result.children[0].media_content_id)

    async def test_excluded_camera_stays_hidden_without_information_item(self):
        payload = fixture.cameras()
        payload["cameras"] = [
            row for row in payload["cameras"] if row["cameraId"] != fixture.CAMERA
        ]
        self.runtime.client.rpc.return_value = {"ok": True, "recordings": payload}
        result = await self.source.async_browse_media(SimpleNamespace(identifier=""))
        self.assertEqual(len(result.children), 4)
        self.assertNotIn(
            folder_id(fixture.SCOPE, fixture.CAMERA),
            [row.media_content_id for row in result.children],
        )
        self.runtime.client.rpc.reset_mock()
        with self.assertRaises(BrowseError):
            await self.source.async_browse_media(
                SimpleNamespace(identifier=f"s/{fixture.SCOPE}/c/{fixture.CAMERA}")
            )
        self.runtime.client.rpc.assert_awaited_once_with(
            "recordings.cameras", timeout=3
        )

    async def test_ha_zone_groups_old_bridge_utc_times_at_midnight_and_dst(self):
        base = f"s/{fixture.SCOPE}/c/{fixture.CAMERA}"
        cases = (
            (
                "Europe/Berlin",
                ("2026-11-05T22:59:00Z", "2026-11-05T23:00:00Z"),
                ("2026-11-06", "2026-11-05"),
                ("12:00 AM · 10 s",),
            ),
            (
                "Australia/Sydney",
                ("2026-11-05T12:59:00Z", "2026-11-05T13:00:00Z"),
                ("2026-11-06", "2026-11-05"),
                ("12:00 AM · 10 s",),
            ),
            (
                "Europe/Berlin",
                ("2026-10-25T00:30:00Z", "2026-10-25T01:30:00Z"),
                ("2026-10-25",),
                ("2:30 AM · 10 s", "2:30 AM · 10 s"),
            ),
            (
                "Australia/Sydney",
                ("2026-10-03T15:59:00Z", "2026-10-03T16:00:00Z"),
                ("2026-10-04",),
                ("3:00 AM · 10 s", "1:59 AM · 10 s"),
            ),
        )
        for zone, instants, days, titles in cases:
            self.hass.config.time_zone = zone
            self.runtime.client.rpc.return_value = {
                "ok": True,
                "recordings": fixture.page(
                    [
                        timed_event(letter, instant)
                        for letter, instant in zip("HI", instants, strict=True)
                    ]
                ),
            }
            with self.subTest(zone=zone, instants=instants):
                result = await self.source.async_browse_media(
                    SimpleNamespace(identifier=base)
                )
                self.assertEqual(
                    [
                        row.media_content_id.rsplit("/", 1)[-1]
                        for row in result.children
                    ],
                    list(days),
                )
                clips = await self.source.async_browse_media(
                    SimpleNamespace(identifier=base + "/d/" + days[0])
                )
                self.assertEqual([row.title for row in clips.children], list(titles))

    async def test_ha_zone_relative_labels_use_local_calendar(self):
        base = f"s/{fixture.SCOPE}/c/{fixture.CAMERA}"
        for zone in ("Europe/Berlin", "Australia/Sydney"):
            current = datetime(2026, 11, 7, 0, 10, tzinfo=ZoneInfo(zone))

            class FixedNow(datetime):
                current_time = current

                @classmethod
                def now(cls, tz: tzinfo | None = None) -> datetime:
                    return cls.current_time.astimezone(tz)

            self.hass.config.time_zone = zone
            self.runtime.client.rpc.return_value = {
                "ok": True,
                "recordings": fixture.page(
                    [
                        timed_event(
                            letter, (current - timedelta(days=offset)).isoformat()
                        )
                        for offset, letter in enumerate("HIJ")
                    ]
                ),
            }
            with (
                self.subTest(zone=zone),
                patch("custom_components.eufy_sdk.media_source.datetime", FixedNow),
            ):
                result = await self.source.async_browse_media(
                    SimpleNamespace(identifier=base)
                )
                self.assertEqual(
                    [row.title for row in result.children],
                    ["Today", "Yesterday", "2026-11-05"],
                )

    async def test_days_group_local_midnight_and_both_dst_transitions(self):
        class FixedNow(datetime):
            @classmethod
            def now(cls, tz: tzinfo | None = None) -> datetime:
                return cls(2027, 1, 2, tzinfo=UTC).astimezone(tz)

        cases = (
            (
                ("2026-11-08T05:59:00Z", "2026-11-08T06:00:00Z"),
                ("2026-11-08", "2026-11-07"),
            ),
            (
                ("2026-03-08T07:59:00Z", "2026-03-08T08:00:00Z"),
                ("2026-03-08",),
            ),
            (
                ("2026-11-01T06:30:00Z", "2026-11-01T07:30:00Z"),
                ("2026-11-01",),
            ),
        )
        base = f"s/{fixture.SCOPE}/c/{fixture.CAMERA}"
        for timestamps, dates in cases:
            with (
                self.subTest(dates=dates),
                patch("custom_components.eufy_sdk.media_source.datetime", FixedNow),
            ):
                older, newer = [
                    timed_event(letter, timestamp)
                    for letter, timestamp in zip("HI", timestamps, strict=True)
                ]
                self.runtime.client.rpc.return_value = {
                    "ok": True,
                    "recordings": fixture.page([older, newer]),
                }
                self.runtime.client.rpc.reset_mock()
                result = await self.source.async_browse_media(
                    SimpleNamespace(identifier=base)
                )
                self.assertEqual(
                    [
                        row.media_content_id.rsplit("/", 1)[-1]
                        for row in result.children
                    ],
                    list(dates),
                )
                self.assertEqual([row.title for row in result.children], list(dates))
                self.runtime.client.rpc.assert_awaited_once_with(
                    "recordings.catalog",
                    timeout=3,
                    cameraId=fixture.CAMERA,
                    refresh="if_due",
                )
                day = await self.source.async_browse_media(
                    SimpleNamespace(identifier=base + "/d/" + dates[0])
                )
                expected = [newer, older] if len(dates) == 1 else [newer]
                self.assertEqual(
                    [row.media_content_id.rsplit("/", 1)[-1] for row in day.children],
                    [row["eventId"] for row in expected],
                )
                self.assertTrue(all("Nov" not in row.title for row in day.children))
                if len(dates) == 1 and dates[0] == "2026-11-01":
                    self.assertEqual(
                        [row.title for row in day.children], ["1:30 AM · 10 s"] * 2
                    )

    async def test_today_yesterday_labels_use_current_chicago_calendar(self):
        base = f"s/{fixture.SCOPE}/c/{fixture.CAMERA}"
        for now in (
            "2026-11-07T00:10:00-06:00",
            "2026-03-09T00:10:00-05:00",
            "2026-11-02T00:10:00-06:00",
            "2027-01-01T00:10:00-06:00",
            "2027-01-02T00:10:00-06:00",
            "2028-03-01T00:10:00-06:00",
            "2028-03-02T00:10:00-06:00",
            "2026-11-08T02:10:00+00:00",
        ):
            current = datetime.fromisoformat(now).astimezone(ZoneInfo(TIMEZONE))

            class FixedNow(datetime):
                current_time = current

                @classmethod
                def now(cls, tz: tzinfo | None = None) -> datetime:
                    return cls.current_time.astimezone(tz)

            rows = [
                timed_event(letter, (current - timedelta(days=offset)).isoformat())
                for offset, letter in enumerate("HIJ")
            ]
            self.runtime.client.rpc.return_value = {
                "ok": True,
                "recordings": fixture.page(rows),
            }
            with (
                self.subTest(now=now),
                patch("custom_components.eufy_sdk.media_source.datetime", FixedNow),
            ):
                result = await self.source.async_browse_media(
                    SimpleNamespace(identifier=base)
                )
                self.assertEqual(
                    [row.title for row in result.children[:2]], ["Today", "Yesterday"]
                )
                self.assertEqual(
                    result.children[2].title,
                    (current.date() - timedelta(days=2)).isoformat(),
                )
                yesterday = result.children[1].media_content_id.removeprefix(
                    "media-source://eufy_sdk/"
                )
                day = await self.source.async_browse_media(
                    SimpleNamespace(identifier=yesterday)
                )
                self.assertEqual(day.title, "Yesterday")

    async def test_year_safe_folders_keep_identifiers_order_and_clip_titles(self):
        cases = (
            (
                "2027-01-02T20:00:00-06:00",
                ("2026-12-31", "2027-01-02", "2025-12-31", "2027-01-01"),
                ("2027-01-02", "2027-01-01", "2026-12-31", "2025-12-31"),
            ),
            (
                "2028-03-02T20:00:00-06:00",
                ("2028-02-28", "2028-03-02", "2024-02-29", "2028-02-29", "2028-03-01"),
                ("2028-03-02", "2028-03-01", "2028-02-29", "2028-02-28", "2024-02-29"),
            ),
        )
        base = f"s/{fixture.SCOPE}/c/{fixture.CAMERA}"
        for now, unsorted_days, ordered_days in cases:
            current = datetime.fromisoformat(now)

            class FixedNow(datetime):
                current_time = current

                @classmethod
                def now(cls, tz: tzinfo | None = None) -> datetime:
                    return cls.current_time.astimezone(tz)

            rows = [
                {
                    **timed_event(letter, day + "T16:58:00-06:00"),
                    "durationSeconds": 36,
                }
                for letter, day in zip(
                    "HIJKL"[: len(unsorted_days)], unsorted_days, strict=True
                )
            ]
            self.runtime.client.rpc.return_value = {
                "ok": True,
                "recordings": fixture.page([unknown_event(), *rows]),
            }
            with (
                self.subTest(now=now),
                patch("custom_components.eufy_sdk.media_source.datetime", FixedNow),
            ):
                result = await self.source.async_browse_media(
                    SimpleNamespace(identifier=base)
                )
                expected_titles = ["Today", "Yesterday", *ordered_days[2:]]
                self.assertEqual(
                    [row.title for row in result.children],
                    [*expected_titles, "Time unknown"],
                )
                self.assertEqual(
                    [row.media_content_id for row in result.children],
                    [
                        folder_id(fixture.SCOPE, fixture.CAMERA) + "/d/" + day
                        for day in ordered_days
                    ]
                    + [folder_id(fixture.SCOPE, fixture.CAMERA) + "/u"],
                )
                for day, expected_title in zip(
                    ordered_days, expected_titles, strict=True
                ):
                    clips = await self.source.async_browse_media(
                        SimpleNamespace(identifier=base + "/d/" + day)
                    )
                    self.assertEqual(clips.title, expected_title)
                    self.assertEqual(
                        [row.title for row in clips.children], ["4:58 PM · 36 s"]
                    )
                unknown = await self.source.async_browse_media(
                    SimpleNamespace(identifier=base + "/u")
                )
                self.assertEqual(unknown.title, "Time unknown")
                self.assertEqual(
                    [row.title for row in unknown.children], ["Time unknown · 10 s"]
                )

    async def test_hidden_or_unplayable_only_days_are_never_shown(self):
        base = f"s/{fixture.SCOPE}/c/{fixture.CAMERA}"
        rows = [
            {**timed_event("H", "2026-11-07T18:00:00Z"), "recordingAvailable": False},
            {**timed_event("I", "2026-11-06T18:00:00Z"), "playable": False},
            timed_event("J", "2026-11-05T18:00:00Z"),
        ]
        self.runtime.client.rpc.return_value = {
            "ok": True,
            "recordings": fixture.page(rows),
        }
        result = await self.source.async_browse_media(SimpleNamespace(identifier=base))
        self.assertEqual(len(result.children), 1)
        self.assertTrue(result.children[0].media_content_id.endswith("/d/2026-11-05"))
        for day in ("2026-11-07", "2026-11-06", "2026-11-04"):
            with self.subTest(day=day), self.assertRaises(BrowseError):
                await self.source.async_browse_media(
                    SimpleNamespace(identifier=base + "/d/" + day)
                )

    async def test_day_paths_reject_noncanonical_or_forged_dates_without_rpc(self):
        base = f"s/{fixture.SCOPE}/c/{fixture.CAMERA}/d/"
        for day in (
            "20261107",
            "2026-W45-6",
            "2026-11-7",
            "2026-02-30",
            "2026-11-07?x",
            "2026-11-07/e/" + fixture.EVENT,
        ):
            with self.subTest(day=day), self.assertRaises(BrowseError):
                await self.source.async_browse_media(
                    SimpleNamespace(identifier=base + day)
                )
        self.runtime.client.rpc.assert_not_awaited()

    async def test_unavailable_rows_do_not_affect_children_or_unknown_folder_count(
        self,
    ):
        legacy = {**fixture.event(playable=True), "durationSeconds": 3}
        visible = {
            **unknown_event(),
            "recordingAvailable": True,
            "coverRevision": "cover_2",
        }
        rows = [
            legacy,
            visible,
            {**fixture.event("e_" + "H" * 22), "recordingAvailable": False},
            {**unknown_event("e_" + "I" * 22), "recordingAvailable": False},
        ]
        self.runtime.client.rpc.return_value = {
            "ok": True,
            "recordings": fixture.page(rows),
        }
        base = f"s/{fixture.SCOPE}/c/{fixture.CAMERA}"
        result = await self.source.async_browse_media(SimpleNamespace(identifier=base))
        self.assertEqual(len(result.children), 2)
        self.assertEqual(result.children[-1].title, "Time unknown")
        result = await self.source.async_browse_media(
            SimpleNamespace(identifier=base + "/u")
        )
        self.assertEqual(result.title, "Time unknown")
        self.assertEqual(len(result.children), 1)
        self.assertTrue(result.children[0].thumbnail.endswith("?v=cover_2"))

    async def test_hidden_only_page_is_empty_and_hidden_detail_is_unavailable(self):
        hidden = {**fixture.event(playable=True), "recordingAvailable": False}
        self.runtime.client.rpc.return_value = {
            "ok": True,
            "recordings": fixture.page([hidden]),
        }
        base = f"s/{fixture.SCOPE}/c/{fixture.CAMERA}"
        result = await self.source.async_browse_media(SimpleNamespace(identifier=base))
        self.assertEqual(result.children, [])
        self.assertTrue(result.title.endswith("checking for events"))
        with self.assertRaises(BrowseError):
            await self.source.async_browse_media(
                SimpleNamespace(identifier=base + f"/e/{fixture.EVENT}")
            )

    async def asyncSetUp(self):
        self.runtime = await fixture.runtime(playable=True)
        self.runtime.client.recordings_capability["playback"] = True
        self.hass = fixture.hass(self.runtime)
        self.source = EufyRecordingMediaSource(self.hass)
        self.addCleanup(self.runtime.close)

    async def test_root_has_five_stable_folders_and_no_station_work(self):
        self.runtime.client.rpc.return_value = {
            "ok": True,
            "recordings": fixture.cameras(),
        }
        result = await self.source.async_browse_media(SimpleNamespace(identifier=""))
        self.assertEqual(len(result.children), 5)
        self.assertEqual(
            result.children[0].media_content_id,
            folder_id(fixture.SCOPE, fixture.CAMERA),
        )
        self.assertTrue(
            all(
                not child.can_play
                and child.can_expand
                and child.media_content_type == ""
                for child in result.children
            )
        )
        self.runtime.client.rpc.assert_awaited_once_with(
            "recordings.cameras", timeout=3
        )

    async def test_camera_browse_one_metadata_rpc_zero_cover_calls(self):
        self.runtime.client.rpc.return_value = {
            "ok": True,
            "recordings": fixture.page([fixture.event(playable=True)]),
        }
        result = await self.source.async_browse_media(
            SimpleNamespace(identifier=f"s/{fixture.SCOPE}/c/{fixture.CAMERA}")
        )
        self.runtime.client.rpc.assert_awaited_once_with(
            "recordings.catalog", timeout=3, cameraId=fixture.CAMERA, refresh="if_due"
        )
        self.assertEqual(len(result.children), 1)
        self.assertTrue(all(DISCLOSURE not in child.title for child in result.children))
        self.assertEqual(
            result.title, self.runtime.cameras[fixture.CAMERA]["name"].strip()
        )
        folder = result.children[0]
        self.assertTrue(folder.media_content_id.endswith("/d/2026-11-04"))
        self.assertTrue(folder.can_expand)
        self.assertFalse(folder.can_play)
        self.runtime.client.rpc.reset_mock()
        result = await self.source.async_browse_media(
            SimpleNamespace(
                identifier=f"s/{fixture.SCOPE}/c/{fixture.CAMERA}/d/2026-11-04"
            )
        )
        self.runtime.client.rpc.assert_awaited_once_with(
            "recordings.catalog", timeout=3, cameraId=fixture.CAMERA, refresh="if_due"
        )
        row = result.children[0]
        self.assertTrue(row.title.startswith("12:00 PM · 10 s"))
        self.assertNotIn("May no longer be on HomeBase", row.title)
        self.assertTrue(row.can_play)
        self.assertFalse(row.can_expand)
        self.assertTrue(row.thumbnail.startswith("/api/eufy_sdk/recordings/"))
        self.assertNotIn("bridge", row.thumbnail)

    async def test_detail_has_cover_utc_and_zero_playable_children(self):
        self.runtime.client.rpc.return_value = {
            "ok": True,
            "recordings": fixture.page(),
        }
        result = await self.source.async_browse_media(
            SimpleNamespace(
                identifier=f"s/{fixture.SCOPE}/c/{fixture.CAMERA}/e/{fixture.EVENT}"
            )
        )
        self.assertEqual(result.children, [])
        self.assertIn("UTC 2026-11-04T18:00:00Z", result.title)
        self.assertIn("May no longer be on HomeBase", result.title)
        self.assertFalse(result.can_play)
        self.assertTrue(result.thumbnail)

    async def test_rebrowse_changes_thumbnail_when_pending_cover_becomes_ready(self):
        item = SimpleNamespace(
            identifier=f"s/{fixture.SCOPE}/c/{fixture.CAMERA}/d/2026-11-04"
        )
        self.runtime.client.rpc.return_value = {
            "ok": True,
            "recordings": fixture.page([fixture.event(playable=True)]),
        }
        before = (await self.source.async_browse_media(item)).children[0].thumbnail
        ready = {
            **fixture.event(playable=True),
            "coverState": "cached",
            "coverRevision": "cover_2",
        }
        self.runtime.client.rpc.return_value = {
            "ok": True,
            "recordings": fixture.page([ready]),
        }
        after = (await self.source.async_browse_media(item)).children[0].thumbnail
        self.assertNotEqual(before, after)
        self.assertEqual(after, before + "?v=cover_2")

    async def test_resolve_always_fails_with_zero_bridge_calls(self):
        self.runtime.client.recordings_capability.pop("playback")
        for identifier in (
            "",
            "https://source.invalid/video.mp4",
            f"s/{fixture.SCOPE}/c/{fixture.CAMERA}/e/{fixture.EVENT}",
        ):
            with self.assertRaises(Unresolvable):
                await self.source.async_resolve_media(
                    SimpleNamespace(identifier=identifier)
                )
        self.runtime.client.rpc.assert_not_awaited()

    async def test_bad_cross_scope_and_disabled_are_safe(self):
        for identifier in (
            "https://source.invalid",
            f"s/s_{'Z' * 22}/c/{fixture.CAMERA}",
            f"s/{fixture.SCOPE}/c/{fixture.CAMERA}/p/cursor",
            f"s/{fixture.SCOPE}/c/{fixture.CAMERA}?url=bad",
        ):
            with self.assertRaisesRegex(BrowseError, "^Recordings unavailable$"):
                await self.source.async_browse_media(
                    SimpleNamespace(identifier=identifier)
                )
        self.runtime.client.rpc.assert_not_awaited()
        self.runtime.client.recordings_capability = None
        result = await self.source.async_browse_media(SimpleNamespace(identifier=""))
        self.assertEqual(result.children, [])

    async def test_empty_failed_and_truncated_states(self):
        name = self.runtime.cameras[fixture.CAMERA]["name"].strip()
        expected = {
            "idle": " · no playable recordings · catalog limited to newest 100",
            "scheduled": (
                " · no playable recordings · checking for events"
                " · catalog limited to newest 100"
            ),
            "deferred": (
                " · showing saved list · no playable recordings"
                " · catalog limited to newest 100"
            ),
            "failed": (
                " · showing saved list · no playable recordings"
                " · catalog limited to newest 100"
            ),
            "rate_limited": " · no playable recordings · catalog limited to newest 100",
        }
        for state in ("idle", "scheduled", "deferred", "failed", "rate_limited"):
            with self.subTest(state=state):
                page = fixture.page([])
                page["truncated"] = True
                page["refresh"]["state"] = state
                page["lastSuccessfulQueryAtMs"] = fixture.event()["startedAtMs"]
                self.runtime.client.rpc.return_value = {"ok": True, "recordings": page}
                result = await self.source.async_browse_media(
                    SimpleNamespace(identifier=f"s/{fixture.SCOPE}/c/{fixture.CAMERA}")
                )
                self.assertEqual(result.children, [])
                self.assertEqual(result.title, name + expected[state])
                self.assertEqual(
                    self.runtime.pages[fixture.CAMERA]["disclosure"], DISCLOSURE
                )
                self.assertEqual(
                    self.runtime.pages[fixture.CAMERA]["refresh"]["state"], state
                )

    async def test_folder_status_only_when_something_is_off(self):
        name = self.runtime.cameras[fixture.CAMERA]["name"].strip()
        for state, suffix in (
            ("idle", ""),
            ("rate_limited", ""),
            ("scheduled", ""),
            ("failed", " · showing saved list"),
            ("deferred", " · showing saved list"),
        ):
            with self.subTest(state=state):
                page = fixture.page([fixture.event(playable=True)])
                page["refresh"]["state"] = state
                self.runtime.pages.clear()
                self.runtime.client.rpc.return_value = {"ok": True, "recordings": page}
                result = await self.source.async_browse_media(
                    SimpleNamespace(identifier=f"s/{fixture.SCOPE}/c/{fixture.CAMERA}")
                )
                self.assertEqual(len(result.children), 1)
                self.assertEqual(result.title, name + suffix)

    async def test_deep_link_exact_empty_content_type_and_rename_stability(self):
        path = navigation_path(fixture.SCOPE, fixture.CAMERA)
        self.assertEqual(
            path,
            f"/media-browser/browser/%2Cmedia-source%3A%2F%2Feufy_sdk%2Fs%2F{fixture.SCOPE}%2Fc%2F{fixture.CAMERA}",
        )
        content_type, uri = unquote(path.removeprefix("/media-browser/browser/")).split(
            ",", 1
        )
        self.assertEqual(content_type, "")
        self.assertEqual(uri, folder_id(fixture.SCOPE, fixture.CAMERA))
        self.assertEqual(
            parse_identifier(uri.removeprefix("media-source://eufy_sdk/")),
            (fixture.SCOPE, fixture.CAMERA, None),
        )
        self.runtime.cameras[fixture.CAMERA]["name"] = "Renamed synthetic camera"
        self.assertEqual(uri, folder_id(fixture.SCOPE, fixture.CAMERA))

    async def test_time_first_titles_preserve_station_wall_time(self):
        cases = (
            ("2026-11-05T11:32:04-06:00", "11:32 AM · 24 s"),
            ("2026-11-05T00:07:00-06:00", "12:07 AM · 24 s"),
            ("2026-11-05T12:07:00-06:00", "12:07 PM · 24 s"),
            ("2026-11-05T23:09:00-06:00", "11:09 PM · 24 s"),
            ("2026-07-05T09:03:00-05:00", "9:03 AM · 24 s"),
            ("2026-11-01T01:30:00-05:00", "1:30 AM · 24 s"),
            ("2026-11-01T01:30:00-06:00", "1:30 AM · 24 s"),
            ("2026-01-01T00:00:00-06:00", "12:00 AM · 24 s"),
            ("2026-12-31T23:59:59-06:00", "11:59 PM · 24 s"),
        )
        for local, expected in cases:
            with self.subTest(local=local):
                event = {
                    **fixture.event(),
                    "startedAtLocal": local,
                    "startedAtUtc": datetime.fromisoformat(local)
                    .astimezone(UTC)
                    .isoformat(),
                    "durationSeconds": 24,
                    "stationPresence": "seen_in_latest_window",
                }
                self.assertEqual(title(event, time_zone=TIMEZONE), expected)

    async def test_titles_do_not_use_locale_or_platform_strftime(self):
        class NoStrftime(datetime):
            def strftime(self, _format: str) -> str:
                raise AssertionError

        event = {**fixture.event(), "stationPresence": "seen_in_latest_window"}
        with patch("custom_components.eufy_sdk.media_source.datetime", NoStrftime):
            self.assertEqual(title(event, time_zone=TIMEZONE), "12:00 PM · 10 s")

    async def test_unknown_and_invalid_duration_labelled(self):
        for duration in (None, -1, float("nan"), float("inf"), True, "24", 86401):
            with self.subTest(duration=duration):
                event = {
                    **fixture.event(),
                    "durationSeconds": duration,
                    "stationPresence": "seen_in_latest_window",
                }
                self.assertEqual(
                    title(event, time_zone=TIMEZONE), "12:00 PM · duration unknown"
                )
                self.assertEqual(
                    title(
                        {**unknown_event(), "durationSeconds": duration},
                        time_zone=TIMEZONE,
                    ),
                    "Time unknown · duration unknown",
                )
        for duration, expected in (
            (59, "59 s"),
            (59.6, "59.6 s"),
            (60.4, "1 min"),
            (60, "1 min"),
            (78, "1 min 18 s"),
            (120, "2 min"),
            (3599, "59 min 59 s"),
        ):
            with self.subTest(duration=duration):
                self.assertEqual(
                    title(
                        {**unknown_event(), "durationSeconds": duration},
                        time_zone=TIMEZONE,
                    ),
                    f"Time unknown · {expected}",
                )
        for duration, expected in ((0, "0"), (2.5, "2.5")):
            with self.subTest(duration=duration):
                self.assertEqual(
                    title(
                        {**unknown_event(), "durationSeconds": duration},
                        time_zone=TIMEZONE,
                    ),
                    f"Time unknown · {expected} s",
                )

    async def test_unknown_titles_keep_time_status_in_detail_only(self):
        for status in ("ambiguous", "invalid", "unknown_format"):
            with self.subTest(status=status):
                event = unknown_event(status=status)
                self.assertEqual(
                    title(event, time_zone=TIMEZONE), "Time unknown · 10 s"
                )
                self.assertIn(
                    "local time " + status.replace("_", " "),
                    title(event, time_zone=TIMEZONE, detail=True),
                )
                self.assertIn(
                    "Browse only", title(event, time_zone=TIMEZONE, detail=True)
                )

    async def test_unknown_folder_last_and_known_events_newest_first(self):
        newer = fixture.event(playable=True)
        older = {
            **fixture.event(fixture.OTHER, playable=True),
            "startedAtMs": newer["startedAtMs"] - 60000,
            "endedAtMs": newer["endedAtMs"] - 60000,
            "startedAtUtc": "2026-11-04T17:59:00Z",
            "startedAtLocal": "2026-11-04T11:59:00-06:00",
            "sourceStartLocal": "2026-11-04 11:59:00",
        }
        rows = [
            unknown_event("e_" + "U" * 22),
            older,
            unknown_event("e_" + "V" * 22, "ambiguous"),
            newer,
            unknown_event("e_" + "W" * 22, "invalid"),
        ]
        self.runtime.client.rpc.return_value = {
            "ok": True,
            "recordings": fixture.page(rows),
        }
        result = await self.source.async_browse_media(
            SimpleNamespace(identifier=f"s/{fixture.SCOPE}/c/{fixture.CAMERA}")
        )
        self.assertEqual(len(result.children), 2)
        self.assertTrue(result.children[0].media_content_id.endswith("/d/2026-11-04"))
        folder = result.children[-1]
        self.assertEqual(folder.title, "Time unknown")
        self.assertEqual(
            folder.media_content_id, folder_id(fixture.SCOPE, fixture.CAMERA) + "/u"
        )
        self.assertTrue(folder.can_expand)
        self.assertFalse(folder.can_play)
        self.assertEqual(folder.media_content_type, "")
        self.assertIsNone(folder.thumbnail)
        self.assertTrue(all(DISCLOSURE not in row.title for row in result.children))
        self.runtime.client.rpc.assert_awaited_once_with(
            "recordings.catalog", timeout=3, cameraId=fixture.CAMERA, refresh="if_due"
        )

    async def test_unknown_folder_contents_order_and_event_shape(self):
        older = unknown_event()
        newer = {
            **unknown_event("e_" + "U" * 22, "ambiguous"),
            "firstObservedAtMs": older["firstObservedAtMs"] + 1000,
            "durationSeconds": None,
            "coverRevision": "cover_2",
        }
        self.runtime.client.rpc.return_value = {
            "ok": True,
            "recordings": fixture.page([older, fixture.event(playable=True), newer]),
        }
        result = await self.source.async_browse_media(
            SimpleNamespace(identifier=f"s/{fixture.SCOPE}/c/{fixture.CAMERA}/u")
        )
        self.assertEqual(result.title, "Time unknown")
        self.assertEqual(
            [row.title for row in result.children],
            ["Time unknown · duration unknown", "Time unknown · 10 s"],
        )
        for row, event in zip(result.children, (newer, older), strict=True):
            self.assertEqual(
                row.media_content_id,
                folder_id(fixture.SCOPE, fixture.CAMERA) + "/e/" + event["eventId"],
            )
            self.assertFalse(row.can_expand)
            self.assertTrue(row.can_play)
            self.assertEqual(row.media_content_type, "video/mp4")
            self.assertIsNone(row.children)
            self.assertEqual(
                row.thumbnail,
                f"/api/eufy_sdk/recordings/{fixture.SCOPE}/{fixture.CAMERA}/{event['eventId']}/cover"
                + ("?v=cover_2" if event is newer else ""),
            )
        self.runtime.client.rpc.assert_awaited_once_with(
            "recordings.catalog", timeout=3, cameraId=fixture.CAMERA, refresh="if_due"
        )

    async def test_only_unknown_events_have_one_folder(self):
        self.runtime.client.rpc.return_value = {
            "ok": True,
            "recordings": fixture.page([unknown_event()]),
        }
        result = await self.source.async_browse_media(
            SimpleNamespace(identifier=f"s/{fixture.SCOPE}/c/{fixture.CAMERA}")
        )
        self.assertEqual([row.title for row in result.children], ["Time unknown"])

    async def test_unknown_event_detail_retains_canonical_identifier(self):
        event = {**unknown_event(), "playable": False}
        self.runtime.client.rpc.return_value = {
            "ok": True,
            "recordings": fixture.page([event]),
        }
        identifier = f"s/{fixture.SCOPE}/c/{fixture.CAMERA}/e/{fixture.OTHER}"
        result = await self.source.async_browse_media(
            SimpleNamespace(identifier=identifier)
        )
        self.assertEqual(
            result.media_content_id, "media-source://eufy_sdk/" + identifier
        )
        self.assertEqual(result.children, [])
        self.assertTrue(result.title.startswith("Time unknown · 10 s"))
        self.assertFalse(result.can_play)
        self.assertTrue(result.thumbnail)
        self.runtime.client.rpc.assert_awaited_once_with(
            "recordings.catalog",
            timeout=3,
            cameraId=fixture.CAMERA,
            refresh="if_due",
            focusEventId=fixture.OTHER,
        )

    async def test_unknown_folder_absent_route_fails_closed(self):
        for rows in (
            [],
            [fixture.event(playable=True)],
            [{**unknown_event(), "playable": False}],
        ):
            with self.subTest(empty=not rows):
                self.runtime.client.rpc.return_value = {
                    "ok": True,
                    "recordings": fixture.page(rows),
                }
                with self.assertRaisesRegex(BrowseError, "^Recordings unavailable$"):
                    await self.source.async_browse_media(
                        SimpleNamespace(
                            identifier=f"s/{fixture.SCOPE}/c/{fixture.CAMERA}/u"
                        )
                    )

    async def test_unknown_folder_and_note_identifiers_fail_closed(self):
        base = f"s/{fixture.SCOPE}/c/{fixture.CAMERA}"
        for identifier in (
            base + "/U",
            base + "/u/",
            base + "/u?query=value",
            base + "/u#fragment",
            base + "/u/e/" + fixture.EVENT,
            base + "/u/../u",
            base + "/u/unknown",
            base + "/note",
            base + "/n/disclosure",
            base + "/e/u",
            f"s/s_{'Z' * 22}/c/{fixture.CAMERA}/u",
            f"s/{fixture.SCOPE}/c/invalid/u",
        ):
            with (
                self.subTest(identifier=identifier),
                self.assertRaisesRegex(BrowseError, "^Recordings unavailable$"),
            ):
                await self.source.async_browse_media(
                    SimpleNamespace(identifier=identifier)
                )
        self.runtime.client.rpc.assert_not_awaited()

    async def test_browse_failure_logs_site_without_values(self):
        self.runtime.client.rpc.return_value = {
            "ok": True,
            "recordings": fixture.cameras(),
        }
        identifier = f"s/{fixture.SCOPE}/c/c_{'Z' * 22}/u"
        with (
            self.assertLogs(MEDIA_LOGGER, "WARNING") as logs,
            self.assertRaisesRegex(BrowseError, "^Recordings unavailable$"),
        ):
            await self.source.async_browse_media(SimpleNamespace(identifier=identifier))
        self.assertEqual(len(logs.output), 1)
        self.assertIn("Recordings browse failed: RecordingsError at ", logs.output[0])
        self.assertNotIn("unknown", logs.output[0])
        self.assertNotIn("Z" * 22, logs.output[0])
        self.assertNotIn(fixture.SCOPE, logs.output[0])

    async def test_unknown_folder_for_unenrolled_opaque_camera_fails_closed(self):
        self.runtime.client.rpc.return_value = {
            "ok": True,
            "recordings": fixture.cameras(),
        }
        with self.assertRaisesRegex(BrowseError, "^Recordings unavailable$"):
            await self.source.async_browse_media(
                SimpleNamespace(identifier=f"s/{fixture.SCOPE}/c/c_{'Z' * 22}/u")
            )
        self.runtime.client.rpc.assert_awaited_once_with(
            "recordings.cameras", timeout=3
        )
