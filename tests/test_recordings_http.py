# ruff: noqa: ASYNC240, FBT003, ANN002, ANN003, ARG001, ANN201, ANN202, D100, D101, D102, INP001, PT009, PT027, SLF001
import asyncio
import time
import unittest
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import web
from fixtures import recordings_v1 as fixture
from homeassistant.helpers.http import KEY_HASS

from custom_components.eufy_sdk.recordings import CACHE_SECONDS
from custom_components.eufy_sdk.recordings_http import (
    EufyRecordingCoverView,
    async_register_view,
)


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
        self.request = SimpleNamespace(app={KEY_HASS: self.hass}, headers={})
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
        self.assertEqual(response.headers["Cache-Control"], "private, max-age=86400")
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
        self.assertEqual(response.headers["Cache-Control"], "private, no-store")
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
        self.runtime.waiters = 64
        self.assertIsNone(await self.runtime.cover(fixture.CAMERA, fixture.EVENT))
        self.runtime.waiters = 0
        self.runtime.jobs = {(str(index), "synthetic"): Mock() for index in range(64)}
        self.assertIsNone(await self.runtime.cover(fixture.CAMERA, fixture.EVENT))
        self.runtime.jobs.clear()
        self.runtime.cache[(fixture.CAMERA, fixture.EVENT)] = (
            time.monotonic() - CACHE_SECONDS - 1,
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
        self.assertEqual(hass.http.register_view.call_count, 4)
        self.assertEqual(
            [call.args[0].name for call in hass.http.register_view.call_args_list],
            [
                "api:eufy_sdk:recordings:cover",
                "api:eufy_sdk:recordings:clip",
                "api:eufy_sdk:recordings:playlist",
                "api:eufy_sdk:recordings:segment",
            ],
        )
        self.assertTrue(self.placeholder.startswith(b"\x89PNG\r\n\x1a\n"))

    async def test_thirty_distinct_requests_queue_without_http_errors(self):
        active = peak = 0

        async def reply(_cmd: str, **kwargs):
            return {
                "ok": True,
                "recordings": {**fixture.cover(), "eventId": kwargs["eventId"]},
            }

        async def fetch(*_args):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.001)
            active -= 1
            return fixture.jpeg(), None

        self.runtime.client.rpc.side_effect = reply
        self.runtime.client.fetch_recordings_cover = AsyncMock(side_effect=fetch)
        started = time.monotonic()
        responses = await asyncio.gather(
            *(
                self.view.get(
                    self.request,
                    fixture.SCOPE,
                    fixture.CAMERA,
                    "e_" + str(index).zfill(22),
                )
                for index in range(30)
            )
        )
        self.assertTrue(all(response.status == HTTPStatus.OK for response in responses))
        self.assertTrue(
            all(response.content_type == "image/jpeg" for response in responses)
        )
        self.assertLessEqual(peak, 4)
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(self.runtime.waiters, 0)

    async def test_cache_precedes_overload_and_survives_hours_without_event_list(self):
        self.runtime.cache[(fixture.CAMERA, fixture.EVENT)] = (
            time.monotonic() - 3600,
            fixture.jpeg(),
            None,
        )
        self.runtime.events.clear()
        self.runtime.waiters = 64
        self.assertEqual((await self.get()).content_type, "image/jpeg")
        self.runtime.client.rpc.assert_not_awaited()
        self.runtime.waiters = 0

    async def test_overload_returns_uncached_placeholder(self):
        self.runtime.waiters = 64
        response = await self.get()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.content_type, "image/png")
        self.assertEqual(response.headers["Cache-Control"], "private, no-store")
        self.runtime.client.rpc.assert_not_awaited()
        self.runtime.waiters = 0

    async def test_etag_revalidation_and_mismatch(self):
        self.runtime.cache[(fixture.CAMERA, fixture.EVENT)] = (
            time.monotonic(),
            fixture.jpeg(),
            None,
        )
        original = await self.get()
        etag = original.headers["ETag"]
        for validator in (etag, f'"other", W/{etag}', "*"):
            self.request.headers = {"If-None-Match": validator}
            response = await self.get()
            self.assertEqual(response.status, 304)
            self.assertIsNone(response.body)
            self.assertEqual(response.headers["ETag"], etag)
            self.assertEqual(
                response.headers["Cache-Control"], "private, max-age=86400"
            )
        self.request.headers = {"If-None-Match": '"other"'}
        self.assertEqual((await self.get()).status, 200)

    async def test_restart_restores_enrollment_and_reads_only_bridge_cache(self):
        self.runtime.reset()
        self.runtime.client.rpc.side_effect = [
            {"ok": True, "recordings": fixture.cameras()},
            {"ok": True, "recordings": fixture.cover()},
        ]
        self.runtime.client.fetch_recordings_cover = AsyncMock(
            return_value=(fixture.jpeg(), None)
        )
        self.assertEqual((await self.get()).content_type, "image/jpeg")
        calls = self.runtime.client.rpc.await_args_list
        self.assertEqual(
            [call.args[0] for call in calls], ["recordings.cameras", "recordings.cover"]
        )
        self.assertEqual(calls[-1].kwargs["mode"], "cache_only")
        self.assertEqual(self.runtime.events, {})

    async def test_pending_fill_in_polls_without_notification(self):
        self.runtime.client.rpc.side_effect = [
            {"ok": True, "recordings": fixture.cover("pending")},
            {"ok": True, "recordings": fixture.cover()},
        ]
        self.runtime.client.fetch_recordings_cover = AsyncMock(
            return_value=(fixture.jpeg(), None)
        )
        with patch("custom_components.eufy_sdk.recordings.COVER_POLL_SECONDS", 0.001):
            response = await self.get()
        self.assertEqual(response.content_type, "image/jpeg")
        self.assertEqual(
            self.runtime.client.rpc.await_args_list[-1].kwargs["mode"], "cache_only"
        )

    async def test_pending_requests_release_slots_for_cached_bridge_covers(self):
        pending = asyncio.Event()

        async def reply(_cmd: str, **kwargs):
            event = kwargs["eventId"]
            state = "ready" if event == fixture.OTHER else "pending"
            if state == "pending":
                pending.set()
            return {
                "ok": True,
                "recordings": {**fixture.cover(state), "eventId": event},
            }

        self.runtime.client.rpc.side_effect = reply
        self.runtime.client.fetch_recordings_cover = AsyncMock(
            return_value=(fixture.jpeg(), None)
        )
        tasks = [asyncio.create_task(self.get()) for _ in range(30)]
        await pending.wait()
        response = await asyncio.wait_for(
            self.view.get(self.request, fixture.SCOPE, fixture.CAMERA, fixture.OTHER),
            timeout=1,
        )
        self.assertEqual(response.content_type, "image/jpeg")
        self.runtime.reset()
        self.assertTrue(
            all(
                response.content_type == "image/png"
                for response in await asyncio.gather(*tasks)
            )
        )

    async def test_ram_cache_keeps_256_small_covers(self):
        self.runtime.client.recordings_cover = AsyncMock(return_value=fixture.cover())
        self.runtime.client.fetch_recordings_cover = AsyncMock(
            return_value=(fixture.jpeg(), None)
        )
        for index in range(257):
            await self.runtime.cover(fixture.CAMERA, "e_" + str(index).zfill(22))
        self.assertEqual(len(self.runtime.cache), 256)
        self.assertNotIn((fixture.CAMERA, "e_" + "0" * 22), self.runtime.cache)
