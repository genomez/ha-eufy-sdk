# ruff: noqa: ASYNC240, FBT003, ANN002, ANN003, ARG001, ANN201, ANN202, D100, D101, D102, INP001, PT009, PT027, SLF001
import asyncio
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import web
from fixtures import recordings_v1 as fixture
from homeassistant.helpers.http import KEY_HASS

from custom_components.eufy_sdk.recordings_http import (
    EufyRecordingCoverView,
    async_register_view,
)
from custom_components.eufy_sdk.recordings_models import RecordingsError


class CoverTests(unittest.IsolatedAsyncioTestCase):
    async def test_ready_bytes_after_scope_replacement_are_discarded(self):
        self.runtime.client.rpc.return_value = {
            "ok": True,
            "recordings": fixture.cover(),
        }

        async def fetched(*_args):
            self.runtime.client.recordings_capability = {"scopeId": "s_" + "Z" * 22}
            return fixture.jpeg(), None

        self.runtime.client.fetch_recordings_cover = AsyncMock(side_effect=fetched)
        response = await self.get()
        self.assertEqual(response.content_type, "image/png")
        self.assertEqual(len(self.runtime.cache), 0)
        self.assertEqual(len(self.runtime.pages), 0)

    async def test_ram_cache_eviction_count_and_bytes(self):
        rows = [fixture.event("e_" + str(index).zfill(22)) for index in range(33)]
        self.runtime.client.rpc.return_value = {
            "ok": True,
            "recordings": fixture.page(rows),
        }
        await self.runtime.browse(fixture.CAMERA)

        async def reply(_cmd: str, **kwargs):
            return {
                "ok": True,
                "recordings": {**fixture.cover(), "eventId": kwargs["eventId"]},
            }

        self.runtime.client.rpc.side_effect = reply
        data = b"x" * 524288
        self.runtime.client.fetch_recordings_cover = AsyncMock(
            return_value=(data, None)
        )
        for row in rows:
            await self.runtime.cover(fixture.CAMERA, row["eventId"])
        self.assertEqual(len(self.runtime.cache), 32)
        self.assertLessEqual(
            sum(len(row[1]) for row in self.runtime.cache.values()), 16 * 1024 * 1024
        )
        self.assertNotIn((fixture.CAMERA, rows[0]["eventId"]), self.runtime.cache)

    async def asyncSetUp(self):
        self.runtime = await fixture.runtime()
        self.addCleanup(self.runtime.close)
        self.hass = fixture.hass(self.runtime)
        self.request = SimpleNamespace(app={KEY_HASS: self.hass})
        self.placeholder = Path(
            "custom_components/eufy_sdk/recordings_placeholder.png"
        ).read_bytes()
        self.view = EufyRecordingCoverView(self.placeholder)

    async def get(self):
        return await self.view.get(
            self.request, fixture.SCOPE, fixture.CAMERA, fixture.EVENT
        )

    async def test_auth_required_even_for_cached_bytes_and_success_headers(self):
        self.assertIs(EufyRecordingCoverView.requires_auth, True)
        self.runtime.cache[(fixture.CAMERA, fixture.EVENT)] = (
            time.monotonic(),
            fixture.jpeg(),
            "2026-11-04T18:00:00Z",
        )
        response = await self.get()
        self.assertEqual(response.content_type, "image/jpeg")
        self.assertEqual(response.headers["Cache-Control"], "private, no-store")
        self.assertEqual(response.headers["X-Eufy-Event-Time"], "2026-11-04T18:00:00Z")
        self.runtime.client.rpc.assert_not_awaited()

    async def test_placeholder_correct_mime_for_unavailable_and_timeout(self):
        self.runtime.client.rpc.return_value = {
            "ok": True,
            "recordings": fixture.cover("unavailable"),
        }
        response = await self.get()
        self.assertEqual(response.content_type, "image/png")
        self.assertEqual(response.body, self.placeholder)
        self.assertEqual(response.headers["Retry-After"], "60")
        self.runtime.client.rpc.return_value = {
            "ok": True,
            "recordings": fixture.cover("pending"),
        }
        with patch("custom_components.eufy_sdk.recordings.WAIT_SECONDS", 0.01):
            response = await self.get()
        self.assertEqual(response.content_type, "image/png")

    async def test_invalid_cross_scope_unknown_camera_event_disabled_and_unloaded(self):
        for scope, camera, event in (
            ("../bad", fixture.CAMERA, fixture.EVENT),
            ("s_" + "Z" * 22, fixture.CAMERA, fixture.EVENT),
            (fixture.SCOPE, "c_" + "Z" * 22, fixture.EVENT),
            (fixture.SCOPE, fixture.CAMERA, fixture.OTHER),
        ):
            with self.assertRaises(web.HTTPNotFound):
                await self.view.get(self.request, scope, camera, event)
        self.runtime.client.rpc.assert_not_awaited()
        self.runtime.client.recordings_capability = None
        with self.assertRaises(web.HTTPNotFound):
            await self.get()
        self.hass.config_entries.async_entries.return_value = []
        with self.assertRaises(web.HTTPNotFound):
            await self.get()

    async def test_direct_proxy_cache_only_and_repeated_admission(
        self,
    ):
        self.runtime.interests.clear()
        self.runtime.client.rpc.return_value = {
            "ok": True,
            "recordings": fixture.cover("unavailable"),
        }
        await self.get()
        self.runtime.client.rpc.assert_awaited_once_with(
            "recordings.cover",
            timeout=3,
            cameraId=fixture.CAMERA,
            eventId=fixture.EVENT,
            browseId=None,
            mode="cache_only",
        )
        self.runtime.client.rpc.reset_mock()
        self.runtime.interests[fixture.CAMERA] = (
            "B" * 22,
            time.monotonic() + 60,
            frozenset({fixture.EVENT}),
        )
        await self.get()
        await self.get()
        modes = [
            call.kwargs["mode"] for call in self.runtime.client.rpc.await_args_list
        ]
        self.assertEqual(modes, ["request_if_admitted", "cache_only"])

    async def test_pending_notification_wakes_only_existing_request_and_deduplicates(
        self,
    ):
        self.runtime.client.rpc.side_effect = [
            {"ok": True, "recordings": fixture.cover("pending")},
            {"ok": True, "recordings": fixture.cover()},
        ]
        self.runtime.client.fetch_recordings_cover = AsyncMock(
            return_value=(fixture.jpeg(), None)
        )
        calls = [asyncio.create_task(self.get()) for _ in range(3)]
        for _ in range(8):
            await asyncio.sleep(0)
        self.runtime.changed(
            {
                "event": "recordings.changed",
                "version": 1,
                "scopeId": fixture.SCOPE,
                "cameraId": fixture.CAMERA,
                "revision": "cover_1",
                "reason": "cover",
            }
        )
        responses = await asyncio.gather(*calls)
        self.assertTrue(
            all(response.content_type == "image/jpeg" for response in responses)
        )
        self.assertEqual(self.runtime.client.rpc.await_count, 2)
        self.runtime.client.fetch_recordings_cover.assert_awaited_once()
        self.assertEqual(self.runtime.waiters, 0)

    async def test_disconnect_unload_and_late_completion_cannot_fill_cache(self):
        started = asyncio.Event()
        blocked = asyncio.Event()

        async def rpc(*args, **kwargs):
            started.set()
            await blocked.wait()
            return {"ok": True, "recordings": fixture.cover("pending")}

        self.runtime.client.rpc.side_effect = rpc
        task = asyncio.create_task(self.get())
        await started.wait()
        self.runtime.client._notify_disconnected()
        result = await task
        self.assertEqual(result.content_type, "image/png")
        self.assertEqual(len(self.runtime.cache), 0)
        self.assertEqual(len(self.runtime.interests), 0)
        self.assertEqual(len(self.runtime.jobs), 0)
        self.runtime.close()
        with self.assertRaises(web.HTTPNotFound):
            await self.get()

    async def test_request_and_fetch_bounds_expired_cache_and_interest(self):
        self.runtime.waiters = 8
        with self.assertRaises(RecordingsError):
            await self.runtime.cover(fixture.CAMERA, fixture.EVENT)
        self.runtime.waiters = 0
        self.runtime.jobs = {(str(index), "synthetic"): Mock() for index in range(4)}
        with self.assertRaises(RecordingsError):
            await self.runtime.cover(fixture.CAMERA, fixture.EVENT)
        self.runtime.jobs.clear()
        self.runtime.cache[(fixture.CAMERA, fixture.EVENT)] = (
            time.monotonic() - 61,
            b"expired",
            None,
        )
        self.runtime.interests[fixture.CAMERA] = (
            "B" * 22,
            time.monotonic() - 1,
            frozenset({fixture.EVENT}),
        )
        self.runtime.client.rpc.return_value = {
            "ok": True,
            "recordings": fixture.cover("unavailable"),
        }
        await self.get()
        self.assertEqual(
            self.runtime.client.rpc.await_args.kwargs["mode"], "cache_only"
        )

    async def test_registration_is_once_and_placeholder_is_valid_png(self):
        hass = Mock(data={})
        hass.async_add_executor_job = AsyncMock(return_value=self.placeholder)
        await async_register_view(hass)
        await async_register_view(hass)
        self.assertEqual(hass.http.register_view.call_count, 2)
        self.assertEqual(
            [call.args[0].name for call in hass.http.register_view.call_args_list],
            ["api:eufy_sdk:recordings:cover", "api:eufy_sdk:recordings:clip"],
        )
        self.assertTrue(self.placeholder.startswith(b"\x89PNG\r\n\x1a\n"))
