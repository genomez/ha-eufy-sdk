# ruff: noqa: ANN001, ANN002, ANN201, ANN202, ANN204, ARG001, D100, D101, D102, INP001, PT009, PT027, SLF001
import asyncio
import json
import unittest
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from fixtures import recordings_v1 as fixture
from homeassistant.components import websocket_api
from homeassistant.components.http import auth
from homeassistant.components.media_player import MediaClass
from homeassistant.components.media_source.const import MEDIA_SOURCE_DATA
from homeassistant.components.media_source.error import Unresolvable
from homeassistant.components.media_source.http import (
    websocket_browse_media,
    websocket_resolve_media,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.http import KEY_HASS, request_handler_factory

from custom_components.eufy_sdk import recordings
from custom_components.eufy_sdk import recordings_models as models
from custom_components.eufy_sdk.media_source import (
    EufyRecordingMediaSource,
    playback_error,
)
from custom_components.eufy_sdk.recordings_http import EufyRecordingClipView

IDENTIFIER = f"s/{fixture.SCOPE}/c/{fixture.CAMERA}/e/{fixture.EVENT}"
PATH = (
    f"/api/eufy_sdk/recordings/{fixture.SCOPE}/{fixture.CAMERA}/"
    f"{fixture.EVENT}/clip.mp4"
)
CANARY = "synthetic-private-canary"
WS_CONFIG_DIR = str(Path(".offline/ws-playback").resolve())


def clip(state="ready", reason="none", expected=10000):
    """Build a synthetic clip response without transport selectors."""
    return {
        "clip": {
            "state": state,
            "reason": reason,
            # Mirror the bridge: unknown values are numeric 0 in progress, the
            # duration is set only once ready, and bytes are 0 until then.
            "progress": {"pulledMs": 0, "expectedMs": expected or 0},
            "durationMs": expected if state == "ready" else None,
            "bytes": 12 if state == "ready" else 0,
            "expiresAtMs": 1794000000000 if state == "ready" else None,
        }
    }


class PlaybackFixture(unittest.IsolatedAsyncioTestCase):
    initial_playable = True

    async def asyncSetUp(self):
        self.runtime = await fixture.runtime(playable=self.initial_playable)
        self.addCleanup(self.runtime.close)
        self.client = self.runtime.client
        self.client.recordings_capability["playback"] = True
        self.hass = fixture.hass(self.runtime)
        self.hass.data = {
            auth.DATA_SIGN_SECRET: "synthetic-signing-key-for-offline-tests-only",
            auth.STORAGE_KEY: "synthetic-content-user",
        }
        self.hass.is_stopping = False
        self.source = EufyRecordingMediaSource(self.hass)
        self.view = EufyRecordingClipView()
        self.client.rpc.return_value = {
            "ok": True,
            "recordings": fixture.page([fixture.event(playable=True)]),
        }
        self.client.recordings_clip_prepare = AsyncMock(
            return_value=models.clip(clip())
        )
        self.client.recordings_clip_status = AsyncMock(return_value=models.clip(clip()))

    async def resolve(self, identifier=IDENTIFIER):
        return await self.source.async_resolve_media(
            SimpleNamespace(identifier=identifier)
        )


class PlaybackTests(PlaybackFixture):
    async def test_common_playback_failures_log_one_bounded_warning(self):
        cases = (
            (recordings.ClipUnavailableError("busy"), "busy"),
            (recordings.ClipUnavailableError("convert_failed"), "convert_failed"),
            (recordings.ClipUnavailableError(CANARY), "failed"),
            (TimeoutError(CANARY), "TimeoutError"),
        )
        for error, reason in cases:
            with (
                self.subTest(reason=reason),
                patch.object(self.runtime, "prepare_clip", side_effect=error),
                self.assertLogs(
                    "custom_components.eufy_sdk.media_source", "WARNING"
                ) as logs,
                self.assertRaises(Unresolvable),
            ):
                await self.resolve()
            self.assertEqual(len(logs.records), 1)
            self.assertRegex(
                logs.records[0].getMessage(),
                rf"^Recording playback failed: {reason} at async_resolve_media:\d+$",
            )
            for private in (CANARY, fixture.SCOPE, fixture.CAMERA, fixture.EVENT):
                self.assertNotIn(private, logs.output[0])

    async def test_playable_rows_detail_and_browse_only_fallback(self):
        for playback in (True, False, None, "true"):
            with self.subTest(playback=playback):
                self.client.recordings_capability["playback"] = playback
                row = await self.source.async_browse_media(
                    SimpleNamespace(identifier=IDENTIFIER)
                )
                self.assertIs(row.can_play, playback is True)
                self.assertIs(row.can_expand, playback is not True)
                self.assertEqual(
                    row.media_class,
                    MediaClass.VIDEO if playback is True else MediaClass.DIRECTORY,
                )
                self.assertEqual(
                    row.media_content_type, "video/mp4" if playback is True else ""
                )
                self.assertEqual("Browse only" in row.title, playback is not True)
                page = await self.source.async_browse_media(
                    SimpleNamespace(identifier=IDENTIFIER.rsplit("/e/", 1)[0])
                )
                self.assertIs(page.children[0].can_play, playback is True)

    async def test_event_authority_and_feature_both_gate_browse_and_resolve(self):
        for playback, playable in (
            (True, True),
            (True, False),
            (False, True),
            (False, False),
        ):
            with self.subTest(playback=playback, playable=playable):
                self.client.recordings_capability["playback"] = playback
                self.client.rpc.return_value = {
                    "ok": True,
                    "recordings": fixture.page([fixture.event(playable=playable)]),
                }
                row = await self.source.async_browse_media(
                    SimpleNamespace(identifier=IDENTIFIER)
                )
                self.assertIs(row.can_play, playback and playable)
                self.assertIs(row.can_expand, not (playback and playable))
                self.assertEqual(
                    "Browse only" in row.title, not (playback and playable)
                )
                self.client.recordings_clip_prepare.reset_mock()
                if playback and playable:
                    self.assertEqual((await self.resolve()).mime_type, "video/mp4")
                else:
                    with self.assertRaisesRegex(Unresolvable, "Browse only"):
                        await self.resolve()
                    self.client.recordings_clip_prepare.assert_not_awaited()

    async def test_prepare_ready_returns_real_signed_relative_url(self):
        result = await self.resolve()
        self.assertEqual(result.mime_type, "video/mp4")
        self.assertTrue(result.url.startswith(PATH + "?authSig="))
        self.assertNotIn("bridge", result.url)
        self.client.recordings_clip_prepare.assert_awaited_once_with(
            fixture.CAMERA, fixture.EVENT
        )
        self.client.recordings_clip_status.assert_not_awaited()

    async def test_short_translated_refusals_and_failures(self):
        cases = (
            ("refused", "live_view", "Live view is on", "live_view"),
            ("failed", "not_found", "Recording is no longer", "not_found"),
            ("refused", "busy", "HomeBase is busy", "busy"),
            (
                "refused",
                "owner_unavailable",
                "HomeBase is unavailable",
                "owner_unavailable",
            ),
            ("refused", "too_long", "Recording is too long", "too_long"),
            ("failed", "storage", "Couldn't prepare", "failed"),
            ("failed", "convert_failed", "Couldn't prepare", "failed"),
            ("cancelled", "cleanup_uncertain", "Couldn't prepare", "failed"),
        )
        for state, reason, message, key in cases:
            with self.subTest(state=state, reason=reason):
                self.client.recordings_clip_prepare.return_value = models.clip(
                    clip(state, reason)
                )
                with self.assertRaises(Unresolvable) as caught:
                    await self.resolve()
                self.assertTrue(str(caught.exception).startswith(message))
                self.assertEqual(
                    caught.exception.translation_key, "recordings_clip_" + key
                )
        self.assertFalse(self.runtime.clip_wakeups)

    async def test_notice_wakes_without_waiting_for_fallback(self):
        prepared = asyncio.Event()

        async def prepare(*args):
            prepared.set()
            return models.clip(clip("pulling"))

        self.client.recordings_clip_prepare.side_effect = prepare
        with (
            patch.object(recordings, "CLIP_POLL_SECONDS", 3600),
            patch.object(recordings, "CLIP_NOTICE_SECONDS", 0),
        ):
            task = asyncio.create_task(self.resolve())
            await prepared.wait()
            self.runtime.changed(
                {
                    "version": 1,
                    "scopeId": fixture.SCOPE,
                    "cameraId": fixture.CAMERA,
                    "revision": "clip_1",
                    "reason": "clip",
                }
            )
            result = await task
        self.assertEqual(result.mime_type, "video/mp4")
        self.client.recordings_clip_status.assert_awaited_once()
        self.assertFalse(self.runtime.clip_wakeups)

    async def test_polling_fallback_and_long_duration_deadline(self):
        self.client.recordings_clip_prepare.return_value = models.clip(
            clip("queued", expected=600000)
        )
        timeout = asyncio.timeout
        reschedule = asyncio.Timeout.reschedule
        started = asyncio.get_running_loop().time()
        with (
            patch.object(recordings, "CLIP_POLL_SECONDS", 0.001),
            patch.object(recordings, "CLIP_NOTICE_SECONDS", 0),
            patch.object(recordings.asyncio, "timeout", wraps=timeout) as deadlines,
            patch.object(
                asyncio.Timeout, "reschedule", autospec=True, side_effect=reschedule
            ) as extensions,
        ):
            self.assertEqual((await self.resolve()).mime_type, "video/mp4")
        # Extend the original deadline with the full pipeline estimate.
        self.assertIn(420, [call.args[0] for call in deadlines.call_args_list])
        extended = max(call.args[1] for call in extensions.call_args_list)
        self.assertAlmostEqual(extended - started, 5670, delta=1)
        self.client.recordings_clip_status.assert_awaited_once()

    async def test_unknown_duration_has_pipeline_budget_and_poll_errors_are_private(
        self,
    ):
        self.client.rpc.return_value = {
            "ok": True,
            "recordings": fixture.page(
                [{**fixture.event(playable=True), "durationSeconds": None}]
            ),
        }
        self.client.recordings_clip_prepare.return_value = models.clip(
            clip("converting", expected=None)
        )
        self.client.recordings_clip_status.side_effect = models.RecordingsError()
        timeout = asyncio.timeout
        with (
            patch.object(recordings, "CLIP_POLL_SECONDS", 0.001),
            patch.object(recordings, "CLIP_NOTICE_SECONDS", 0),
            patch.object(recordings.asyncio, "timeout", wraps=timeout) as deadlines,
            self.assertRaisesRegex(Unresolvable, "Couldn't prepare the video"),
        ):
            await self.resolve()
        self.assertIn(1350, [call.args[0] for call in deadlines.call_args_list])
        self.assertFalse(self.runtime.clip_wakeups)

    def test_pipeline_deadline_covers_stage_bounds_and_caps_extreme_reports(self):
        for duration in (10, 12, 120, 600):
            budget = recordings.clip_wait_seconds(duration)
            self.assertGreaterEqual(
                budget,
                150 + 3 * duration + 31 + 60 + max(120, 6 * duration) + 5,
            )
        self.assertEqual(
            recordings.clip_wait_seconds(2**53), recordings.CLIP_MAX_WAIT_SECONDS
        )

    async def test_timeout_is_bounded_and_translated(self):
        self.client.rpc.return_value = {
            "ok": True,
            "recordings": fixture.page(
                [{**fixture.event(playable=True), "durationSeconds": 0}]
            ),
        }
        self.client.recordings_clip_prepare.return_value = models.clip(
            clip("pulling", expected=0)
        )
        with (
            patch.object(recordings, "CLIP_MAX_WAIT_SECONDS", 0.001),
            self.assertRaisesRegex(Unresolvable, "Video is still preparing") as caught,
        ):
            await self.resolve()
        self.assertEqual(caught.exception.translation_key, "recordings_clip_timeout")
        self.assertFalse(self.runtime.clip_wakeups)

    async def test_viewer_cancel_does_not_cancel_another_viewer_or_shared_bridge_job(
        self,
    ):
        both_started = asyncio.Event()
        count = 0

        async def prepare(*args):
            nonlocal count
            count += 1
            if count == 2:  # noqa: PLR2004 - two independent viewers
                both_started.set()
            return models.clip(clip("pulling"))

        self.client.recordings_clip_prepare.side_effect = prepare
        self.client.recordings_clip_cancel = AsyncMock()
        with (
            patch.object(recordings, "CLIP_POLL_SECONDS", 3600),
            patch.object(recordings, "CLIP_NOTICE_SECONDS", 0),
        ):
            first = asyncio.create_task(self.resolve())
            second = asyncio.create_task(self.resolve())
            await both_started.wait()
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            self.assertEqual(len(self.runtime.clip_wakeups), 1)
            for _, wakeup in self.runtime.clip_wakeups:
                wakeup.set()
            self.assertEqual((await second).mime_type, "video/mp4")
        self.client.recordings_clip_cancel.assert_not_awaited()
        self.assertFalse(self.runtime.clip_wakeups)

    async def test_disconnect_releases_pending_resolve(self):
        prepared = asyncio.Event()

        async def prepare(*args):
            prepared.set()
            return models.clip(clip("pulling"))

        self.client.recordings_clip_prepare.side_effect = prepare
        with (
            patch.object(recordings, "CLIP_POLL_SECONDS", 3600),
            patch.object(recordings, "CLIP_NOTICE_SECONDS", 0),
        ):
            task = asyncio.create_task(self.resolve())
            await prepared.wait()
            self.runtime.reset()
            with self.assertRaisesRegex(Unresolvable, "Couldn't prepare"):
                await task
        self.assertFalse(self.runtime.clip_wakeups)
        self.client.recordings_clip_status.assert_not_awaited()

    async def test_disabled_notice_releases_waiter_without_more_rpc(self):
        prepared = asyncio.Event()

        async def prepare(*args):
            prepared.set()
            return models.clip(clip("pulling"))

        self.client.recordings_clip_prepare.side_effect = prepare
        with (
            patch.object(recordings, "CLIP_POLL_SECONDS", 3600),
            patch.object(recordings, "CLIP_NOTICE_SECONDS", 0),
        ):
            task = asyncio.create_task(self.resolve())
            await prepared.wait()
            self.runtime.changed(
                {
                    "version": 1,
                    "scopeId": fixture.SCOPE,
                    "revision": "disabled_1",
                    "reason": "disabled",
                }
            )
            with self.assertRaises(Unresolvable):
                await task
        self.assertFalse(self.runtime.clip_wakeups)
        self.client.recordings_clip_status.assert_not_awaited()

    async def test_complete_resolve_uses_real_clip_rpc_wrappers(self):
        del self.client.recordings_clip_prepare
        del self.client.recordings_clip_status
        self.client.rpc.side_effect = [
            {"ok": True, "recordings": fixture.page([fixture.event(playable=True)])},
            {"ok": True, "recordings": clip("converting")},
            {"ok": True, "recordings": clip()},
        ]
        with (
            patch.object(recordings, "CLIP_POLL_SECONDS", 0.001),
            patch.object(recordings, "CLIP_NOTICE_SECONDS", 0),
        ):
            result = await self.resolve()
        self.assertEqual(result.mime_type, "video/mp4")
        self.assertEqual(
            [call.args[0] for call in self.client.rpc.await_args_list],
            ["recordings.catalog", "recordings.clip_prepare", "recordings.clip_status"],
        )

    async def test_resolve_validation_and_older_bridge_make_no_prepare_call(self):
        for identifier in (
            "",
            "https://source.invalid",
            IDENTIFIER + "?url=bad",
            IDENTIFIER.rsplit("/e/", 1)[0],
            IDENTIFIER.replace(fixture.SCOPE, "s_" + "Z" * 22),
        ):
            with self.subTest(identifier=identifier), self.assertRaises(Unresolvable):
                await self.resolve(identifier)
        self.client.rpc.assert_not_awaited()
        del self.client.recordings_capability["playback"]
        with self.assertRaisesRegex(Unresolvable, "Browse only"):
            await self.resolve()
        self.client.recordings_clip_prepare.assert_not_awaited()

    async def test_unknown_camera_or_missing_event_cannot_prepare(self):
        self.client.rpc.return_value = {"ok": True, "recordings": fixture.cameras()}
        with self.assertRaises(Unresolvable):
            await self.resolve(IDENTIFIER.replace(fixture.CAMERA, "c_" + "Z" * 22))
        self.client.rpc.return_value = {"ok": True, "recordings": fixture.page([])}
        with self.assertRaises(Unresolvable):
            await self.resolve()
        self.client.recordings_clip_prepare.assert_not_awaited()

    def upstream(self, status=206, headers=None, chunks=None):
        response = Mock(
            status=status,
            headers=headers
            or {
                "Content-Type": "video/mp4",
                "Content-Length": "6",
                "Content-Range": "bytes 2-7/12",
                "Accept-Ranges": "bytes",
            },
        )

        async def iterate(size):
            self.assertEqual(size, 65536)
            for chunk in [b"abc", b"def"] if chunks is None else chunks:
                yield chunk

        response.content.iter_chunked = iterate
        context = Mock(
            __aenter__=AsyncMock(return_value=response),
            __aexit__=AsyncMock(return_value=False),
        )
        self.client._session.get.return_value = context
        return response, context

    def request(self, path=PATH, headers=None):
        app = web.Application()
        app[KEY_HASS] = self.hass
        return make_mocked_request("GET", path, app=app, headers=headers or {})

    async def get(
        self,
        request=None,
        scope=fixture.SCOPE,
        camera=fixture.CAMERA,
        event=fixture.EVENT,
    ):
        return await self.view.get(request or self.request(), scope, camera, event)

    async def test_range_passthrough_streams_chunks_and_closes_upstream(self):
        _, context = self.upstream()
        request = self.request(headers={"Range": "bytes=2-7"})
        result = await self.get(request)
        self.assertEqual(result.status, 206)
        self.assertEqual(result.headers["Content-Range"], "bytes 2-7/12")
        self.assertEqual(result.headers["Accept-Ranges"], "bytes")
        self.assertEqual(result.headers["Cache-Control"], "private, no-store")
        self.assertEqual(
            request._payload_writer.write.call_args_list[0].args, (b"abc",)
        )
        self.assertEqual(
            request._payload_writer.write.call_args_list[1].args, (b"def",)
        )
        call = self.client._session.get.call_args
        self.assertEqual(
            str(call.args[0]),
            f"http://bridge.invalid:3000/recordings/v1/clips/{fixture.CAMERA}/{fixture.EVENT}.mp4",
        )
        self.assertEqual(call.kwargs["headers"], {"Range": "bytes=2-7"})
        self.assertFalse(call.kwargs["allow_redirects"])
        self.assertFalse(call.kwargs["auto_decompress"])
        context.__aexit__.assert_awaited_once()

    async def test_full_response_and_unsatisfiable_range(self):
        self.upstream(200, {"Content-Type": "video/mp4", "Content-Length": "6"})
        result = await self.get()
        self.assertEqual(result.status, 200)
        self.assertNotIn("Content-Range", result.headers)
        self.upstream(416, {"Content-Range": "bytes */12"}, chunks=[])
        result = await self.get(self.request(headers={"Range": "bytes=99-"}))
        self.assertEqual(result.status, 416)
        self.assertEqual(result.headers["Content-Range"], "bytes */12")

    async def test_view_validation_auth_and_unsupported_never_contact_bridge(self):
        for args in (
            {"scope": "../bad"},
            {"camera": "raw-serial"},
            {"event": fixture.EVENT + "?url=bad"},
            {"scope": "s_" + "Z" * 22},
            {"camera": "c_" + "Z" * 22},
            {"event": fixture.OTHER},
        ):
            with self.subTest(args=args), self.assertRaises(web.HTTPNotFound):
                await self.get(**args)
        for value in ("bytes=0-1,4-8", "bad", "bytes=-", "bytes=" + "1" * 129 + "-"):
            with (
                self.subTest(value=value),
                self.assertRaises(web.HTTPRequestRangeNotSatisfiable),
            ):
                await self.get(self.request(headers={"Range": value}))
        self.client.recordings_capability["playback"] = False
        with self.assertRaises(web.HTTPNotFound):
            await self.get()
        self.client._session.get.assert_not_called()

    async def test_browse_only_event_cannot_open_cached_clip(self):
        self.client.rpc.return_value = {"ok": True, "recordings": fixture.page()}
        await self.runtime.browse(fixture.CAMERA)
        with self.assertRaises(web.HTTPNotFound):
            await self.get()
        self.client._session.get.assert_not_called()

    async def test_auth_middleware_accepts_signature_and_rejects_anonymous_or_tampering(
        self,
    ):
        token = SimpleNamespace(id="synthetic-content-user", user=Mock())
        self.hass.auth.async_get_user = AsyncMock(
            return_value=SimpleNamespace(refresh_tokens={"synthetic": token})
        )
        self.hass.auth.async_get_refresh_token = Mock(return_value=token)
        app = web.Application()
        store = Mock(async_load=AsyncMock(return_value={"content_user": "synthetic"}))

        class SyntheticStore:
            @classmethod
            def __class_getitem__(cls, item) -> type:
                return cls

            def __new__(cls, *_args):
                return store

        with patch.object(auth, "Store", SyntheticStore):
            await auth.async_setup_auth(self.hass, app)
        handler = AsyncMock(return_value=web.Response())
        authenticated = request_handler_factory(self.hass, self.view, handler)
        signed = (await self.resolve()).url
        expired = auth.async_sign_path(self.hass, PATH, timedelta(seconds=-1))
        with (
            patch.object(auth, "async_user_not_allowed_do_auth", return_value=False),
            patch.object(auth, "is_supervisor_unix_socket_request", return_value=False),
            patch(
                "homeassistant.helpers.network.get_url",
                return_value="https://ha.invalid",
            ),
        ):
            for path in (
                PATH,
                signed.replace("clip.mp4?", "other.mp4?"),
                signed + "&extra=value",
                expired,
            ):
                with self.subTest(path=path), self.assertRaises(web.HTTPUnauthorized):
                    await app.middlewares[0](self.request(path), authenticated)
            handler.assert_not_awaited()
            result = await app.middlewares[0](
                self.request(signed, headers={"Range": "bytes=0-"}), authenticated
            )
        self.assertEqual(result.status, 200)
        handler.assert_awaited_once()

    async def test_upstream_redirect_type_range_and_length_are_validated(self):
        for status, headers in (
            (302, {"Location": "https://private.invalid"}),
            (200, {"Content-Type": "text/html", "Content-Length": "6"}),
            (
                206,
                {
                    "Content-Type": "video/mp4",
                    "Content-Length": "6",
                    "Content-Range": "bytes 0-99/100",
                },
            ),
            (
                200,
                {
                    "Content-Type": "video/mp4",
                    "Content-Length": "6",
                    "Content-Encoding": "gzip",
                },
            ),
        ):
            self.upstream(status, headers)
            with (
                self.subTest(status=status, headers=headers),
                self.assertRaises(web.HTTPBadGateway),
            ):
                await self.get()
        self.upstream(404, {"Content-Length": "0"})
        with self.assertRaises(web.HTTPNotFound):
            await self.get()

    async def test_cancelled_stream_releases_context(self):
        started = asyncio.Event()
        blocked = asyncio.Event()
        upstream, context = self.upstream()

        async def chunks(size):
            started.set()
            await blocked.wait()
            yield b"abcdef"

        upstream.content.iter_chunked = chunks
        task = asyncio.create_task(self.get())
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        context.__aexit__.assert_awaited_once()

    async def test_stream_backpressure_and_scope_change_prevent_late_bytes(self):
        upstream, context = self.upstream()
        request = self.request()

        async def chunks(size):
            yield b"abc"
            self.assertEqual(request._payload_writer.write.call_count, 1)
            self.client.recordings_capability = {
                "scopeId": "s_" + "Z" * 22,
                "playback": True,
            }
            yield b"def"

        upstream.content.iter_chunked = chunks
        result = await self.get(request)
        self.assertFalse(result.keep_alive)
        self.assertEqual(request._payload_writer.write.call_count, 1)
        context.__aexit__.assert_awaited_once()


class ClipContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_rpc_names_validation_and_playback_gating(self):
        client = fixture.client()
        client.recordings_capability["playback"] = True
        client.rpc.return_value = {"ok": True, "recordings": clip()}
        for method, command in (
            (client.recordings_clip_prepare, "recordings.clip_prepare"),
            (client.recordings_clip_status, "recordings.clip_status"),
            (client.recordings_clip_cancel, "recordings.clip_cancel"),
        ):
            self.assertEqual(
                (await method(fixture.CAMERA, fixture.EVENT))["state"], "ready"
            )
            client.rpc.assert_awaited_with(
                command, timeout=3, cameraId=fixture.CAMERA, eventId=fixture.EVENT
            )
        client.rpc.reset_mock()
        for capability in (
            {"scopeId": fixture.SCOPE},
            {"scopeId": fixture.SCOPE, "playback": False},
            None,
        ):
            client.recordings_capability = capability
            with self.assertRaises(models.RecordingsError):
                await client.recordings_clip_prepare(fixture.CAMERA, fixture.EVENT)
        client.recordings_capability = {"scopeId": fixture.SCOPE, "playback": True}
        with self.assertRaises(models.RecordingsError):
            await client.recordings_clip_prepare("raw-serial", fixture.EVENT)
        client.rpc.assert_not_awaited()

    def test_bridge_capability_combinations_and_legacy_browse_only(self):
        for playback in (True, False):
            with self.subTest(playback=playback):
                wire = fixture.capability(playback=playback)
                self.assertIs(wire["browseOnly"], not playback)
                projected = models.capability(wire)
                self.assertIsNotNone(projected)
                self.assertEqual(projected["scopeId"], fixture.SCOPE)
                self.assertIs(projected.get("playback", False), playback)
        self.assertEqual(
            models.capability(fixture.capability()), {"scopeId": fixture.SCOPE}
        )

    def test_bridge_catalog_retains_event_playback_authority(self):
        for playable in (True, False):
            with self.subTest(playable=playable):
                page = models.catalog(
                    fixture.page([fixture.event(playable=playable)]),
                    fixture.SCOPE,
                    fixture.CAMERA,
                )
                self.assertIs(page["events"][0]["playable"], playable)
        for invalid in (None, 0, 1, "true"):
            with (
                self.subTest(invalid=invalid),
                self.assertRaises(models.RecordingsError),
            ):
                models.event({**fixture.event(), "playable": invalid}, fixture.CAMERA)

    def test_disabled_clip_reason_uses_browse_only_message(self):
        reply = models.clip(clip("refused", "disabled"))
        error = playback_error(reply["reason"])
        self.assertEqual(str(error), "Browse only — video unavailable")
        self.assertEqual(error.translation_key, "recordings_browse_only")

    def test_capability_switch_is_strict_and_additive(self):
        for value in (None, False, "true", 1):
            self.assertNotIn(
                "playback",
                models.capability({**fixture.capability(), "playback": value}),
            )
        self.assertTrue(
            models.capability(fixture.capability(playback=True))["playback"]
        )

    def test_status_projection_rejects_untrusted_fields_and_keeps_errors_private(self):
        wire = clip()
        wire["clip"]["private"] = CANARY
        self.assertNotIn(CANARY, json.dumps(models.clip(wire)))
        for mutation in (
            {"reason": CANARY},
            {"state": CANARY},
            {"url": CANARY},
            {"durationMs": True},
            {"bytes": -1},
            {"progress": {"pulledMs": float("inf"), "expectedMs": 10}},
        ):
            value = deepcopy(clip())
            value["clip"].update(mutation)
            with (
                self.subTest(mutation=mutation),
                self.assertRaises(models.RecordingsError) as caught,
            ):
                models.clip(value)
            self.assertNotIn(CANARY, str(caught.exception))


class WebsocketPlaybackTests(PlaybackFixture):
    """Exercise the installed HA dispatcher, handlers and background tasks offline."""

    initial_playable = False

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.ws_hass = HomeAssistant(WS_CONFIG_DIR)
        self.addCleanup(self.ws_hass.import_executor.shutdown, wait=False)
        self.ws_hass.config_entries = self.hass.config_entries
        self.ws_hass.data.update(self.hass.data)
        self.ws_hass.data["media_source"] = {}
        self.ws_hass.data[MEDIA_SOURCE_DATA] = {
            "eufy_sdk": EufyRecordingMediaSource(self.ws_hass)
        }
        websocket_api.async_register_command(self.ws_hass, websocket_browse_media)
        websocket_api.async_register_command(self.ws_hass, websocket_resolve_media)
        self.original_handlers = self.ws_hass.data["websocket_api"]
        self.addCleanup(websocket_api.current_connection.set, None)
        self.addAsyncCleanup(self.cancel_background)
        # Isolate viewer ownership from wire-model regressions: this is the real
        # runtime's validated catalog boundary, not a replacement prepare loop.
        self.client.recordings_catalog = AsyncMock(
            return_value=fixture.page([fixture.event(playable=True)])
        )
        self.client.recordings_clip_prepare.return_value = models.clip(clip("pulling"))
        self.client.recordings_clip_status.return_value = models.clip(clip("pulling"))
        self.client.recordings_clip_cancel = AsyncMock()

    async def cancel_background(self):
        tasks = list(self.ws_hass._background_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def connection(self):
        messages = []
        connection = websocket_api.ActiveConnection(
            Mock(),
            self.ws_hass,
            messages.append,
            Mock(name="synthetic-user"),
            None,
            None,
        )
        return connection, messages

    def dispatch(
        self, connection, msg_id, command="resolve_media", identifier=IDENTIFIER
    ):
        token = websocket_api.current_connection.set(connection)
        try:
            connection.async_handle(
                {
                    "id": msg_id,
                    "type": "media_source/" + command,
                    "media_content_id": "media-source://eufy_sdk/" + identifier,
                }
            )
        finally:
            websocket_api.current_connection.reset(token)

    async def test_real_ha_websocket_close_releases_admission_and_stops_polling(self):
        with patch.object(recordings, "CLIP_POLL_SECONDS", 0.005):
            for _ in range(9):
                connection, messages = self.connection()
                self.dispatch(connection, 1)
                await asyncio.sleep(0)
                self.assertEqual(len(self.runtime.clip_wakeups), 1)
                self.assertFalse(messages)
                connection.async_handle_close()
                await asyncio.sleep(0)
                self.assertFalse(self.runtime.clip_wakeups)
                self.assertFalse(connection.subscriptions)
                self.assertIs(connection.handlers, self.original_handlers)
                await asyncio.gather(
                    *list(self.ws_hass._background_tasks), return_exceptions=True
                )
            polls = self.client.recordings_clip_status.await_count
            await asyncio.sleep(0.015)
            self.assertEqual(self.client.recordings_clip_status.await_count, polls)
            self.assertFalse(self.ws_hass._background_tasks)
            self.client.recordings_clip_status.assert_not_awaited()
            self.client.recordings_clip_cancel.assert_not_awaited()

    async def test_real_ha_navigation_cancels_only_that_connection(self):
        first, first_messages = self.connection()
        second, second_messages = self.connection()
        with (
            patch.object(recordings, "CLIP_POLL_SECONDS", 3600),
            patch.object(recordings, "CLIP_NOTICE_SECONDS", 0),
        ):
            self.dispatch(first, 1)
            self.dispatch(second, 1)
            for _ in range(5):
                await asyncio.sleep(0)
            self.assertEqual(len(self.runtime.clip_wakeups), 2)
            # Navigate to the global source list, which never calls our browse.
            token = websocket_api.current_connection.set(first)
            try:
                first.async_handle(
                    {
                        "id": 2,
                        "type": "media_source/browse_media",
                        "media_content_id": "media-source://",
                    }
                )
            finally:
                websocket_api.current_connection.reset(token)
            for _ in range(5):
                await asyncio.sleep(0)
            self.assertEqual(len(self.runtime.clip_wakeups), 1)
            self.assertFalse(first.subscriptions)
            self.assertIs(first.handlers, self.original_handlers)
            self.assertEqual(len(first_messages), 1)
            self.client.recordings_clip_status.return_value = models.clip(clip())
            for _, wakeup in self.runtime.clip_wakeups:
                wakeup.set()
            await asyncio.gather(
                *list(self.ws_hass._background_tasks), return_exceptions=True
            )
            self.assertEqual(
                json.loads(second_messages[0])["result"]["mime_type"], "video/mp4"
            )
        self.assertFalse(self.runtime.clip_wakeups)
        self.assertFalse(second.subscriptions)
        self.assertIs(second.handlers, self.original_handlers)
        self.client.recordings_clip_cancel.assert_not_awaited()

    async def test_real_ha_replacement_at_capacity_is_admitted(self):
        with patch.object(recordings, "CLIP_POLL_SECONDS", 3600):
            pairs = [self.connection() for _ in range(8)]
            for number, (connection, _messages) in enumerate(pairs, start=1):
                self.dispatch(connection, number)
            for _ in range(5):
                await asyncio.sleep(0)
            self.assertEqual(len(self.runtime.clip_wakeups), 8)
            connection, messages = pairs[0]
            self.dispatch(connection, 100)
            for _ in range(10):
                await asyncio.sleep(0)
            # The replacement waits in place of its predecessor: no refusal.
            self.assertEqual(len(self.runtime.clip_wakeups), 8)
            self.assertFalse(messages)
            for other, _messages in pairs:
                other.async_handle_close()
            for _ in range(5):
                await asyncio.sleep(0)
            self.assertFalse(self.runtime.clip_wakeups)

    async def test_real_ha_new_resolve_replaces_only_its_previous_wait(self):
        connection, messages = self.connection()
        with patch.object(recordings, "CLIP_POLL_SECONDS", 3600):
            self.dispatch(connection, 1)
            for _ in range(5):
                await asyncio.sleep(0)
            self.dispatch(connection, 2)
            for _ in range(5):
                await asyncio.sleep(0)
            self.assertEqual(len(self.runtime.clip_wakeups), 1)
            self.assertFalse(messages)
            self.assertEqual(len(connection.subscriptions), 1)
            connection.async_handle_close()
            for _ in range(5):
                await asyncio.sleep(0)
            self.assertFalse(self.runtime.clip_wakeups)
            self.client.recordings_clip_status.assert_not_awaited()
            self.client.recordings_clip_cancel.assert_not_awaited()
