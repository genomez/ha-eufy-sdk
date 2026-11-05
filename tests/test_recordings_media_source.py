# ruff: noqa: ANN201, D100, D101, D102, INP001, PT009, PT027
import unittest
from types import SimpleNamespace
from urllib.parse import unquote

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


class MediaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runtime = await fixture.runtime()
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
            "recordings": fixture.page(),
        }
        result = await self.source.async_browse_media(
            SimpleNamespace(identifier=f"s/{fixture.SCOPE}/c/{fixture.CAMERA}")
        )
        self.runtime.client.rpc.assert_awaited_once_with(
            "recordings.catalog", timeout=3, cameraId=fixture.CAMERA, refresh="if_due"
        )
        self.assertIn(DISCLOSURE, result.title)
        row = result.children[0]
        self.assertTrue(row.title.startswith("2026-11-04 12:00:00 · Event 7 · 10 s"))
        self.assertIn("May no longer be on HomeBase", row.title)
        self.assertFalse(row.can_play)
        self.assertTrue(row.can_expand)
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
        self.assertFalse(result.can_play)
        self.assertTrue(result.thumbnail)

    async def test_resolve_always_fails_with_zero_bridge_calls(self):
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
        page = fixture.page([])
        page["truncated"] = True
        page["refresh"]["state"] = "failed"
        self.runtime.client.rpc.return_value = {"ok": True, "recordings": page}
        result = await self.source.async_browse_media(
            SimpleNamespace(identifier=f"s/{fixture.SCOPE}/c/{fixture.CAMERA}")
        )
        self.assertIn("cached; refresh failed", result.title)
        self.assertIn("truncated", result.title)
        self.assertIn("use Refresh", result.title)

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

    async def test_ambiguous_unknown_titles_and_known_time_order(self):
        unknown = {
            **fixture.event(),
            "startedAtLocal": None,
            "startedAtUtc": None,
            "sourceStartLocal": "2026-11-01 01:30:00",
            "timeStatus": "ambiguous",
            "durationSeconds": None,
        }
        result = title(unknown)
        self.assertTrue(
            result.startswith("2026-11-01 01:30:00 · Event 7 · duration unknown")
        )
        self.assertIn("Chicago time ambiguous", result)
        unknown.update(sourceStartLocal=None, timeStatus="unknown_format")
        self.assertTrue(title(unknown).startswith("Time unknown"))
