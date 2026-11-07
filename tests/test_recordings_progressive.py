# ruff: noqa: ANN001, ANN002, ANN201, ANN202, ANN204, ANN206, D101, D102, INP001, PT009, PT027, SLF001, PLR2004
"""Offline progressive transport, authorization and lifecycle regressions."""

import asyncio
import time
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from fixtures import recordings_v1 as fixture
from homeassistant.components.http import auth
from homeassistant.helpers.http import KEY_HASS, request_handler_factory
from test_recordings_playback import PlaybackFixture

from custom_components.eufy_sdk import recordings_models as models
from custom_components.eufy_sdk.recordings_http import (
    EufyRecordingPlaylistView,
    EufyRecordingSegmentView,
)

SESSION = "p_" + "A" * 22
GENERATION = "12345678-1234-1234-1234-123456789abc"
BASE = (
    f"/api/eufy_sdk/recordings/{fixture.SCOPE}/{fixture.CAMERA}/{fixture.EVENT}/"
    f"playback/{SESSION}/{GENERATION}"
)


def playback(delivery="playable", *, state="pulling", count=6):
    """Build the actual versioned wire shape using only synthetic identifiers."""
    return {
        "version": 1,
        "playback": {
            "sessionId": SESSION,
            "generation": GENERATION,
            "cameraId": fixture.CAMERA,
            "eventId": fixture.EVENT,
            "hardExpiresAtMs": int(time.time() * 1000) + 900000,
            "capture": "verified_eof" if delivery == "complete" else "pulling",
            "delivery": delivery,
            "cache": "ready" if state == "ready" else "absent",
            "cleanup": "pending",
            "reason": "none",
            "segments": [
                {"sequence": i, "startMs": i * 1000, "durationMs": 1000, "bytes": 6}
                for i in range(count)
            ],
            "clip": {
                "state": state,
                "reason": "none",
                "progress": {"expectedMs": 10000, "pulledMs": 6000},
                "durationMs": 10000 if state == "ready" else None,
                "bytes": 12 if state == "ready" else 0,
                "expiresAtMs": None,
            },
        },
    }


class ProgressiveTests(PlaybackFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.client.recordings_capability["progressivePlayback"] = True
        self.reply = models.playback(playback())
        self.client.recordings_playback_open = AsyncMock(return_value=self.reply)

        async def command(name, _reply):
            return {} if name == "release" else self.reply

        self.client.recordings_playback_command = AsyncMock(side_effect=command)
        self.view = EufyRecordingPlaylistView()

    async def playlist(self):
        return await self.view.get(
            SimpleNamespace(app={KEY_HASS: self.hass}),
            fixture.SCOPE,
            fixture.CAMERA,
            fixture.EVENT,
            SESSION,
            GENERATION,
        )

    async def test_early_resolve_and_independent_child_signatures_remain_immutable(
        self,
    ):
        result = await self.resolve()
        self.assertEqual(result.mime_type, "application/x-mpegURL")
        self.assertIn(BASE + "/index.m3u8?", result.url)
        first = await self.playlist()
        self.assertIn("#EXT-X-START:TIME-OFFSET=0", first.text)
        self.assertNotIn("#EXT-X-ENDLIST", first.text)
        children = [
            line for line in first.text.splitlines() if not line.startswith("#")
        ]
        self.assertEqual(len(children), 6)
        self.assertTrue(
            all("authSig=" in line and line.startswith(BASE) for line in children)
        )
        self.assertNotIn("bridge.invalid", first.text)
        session = self.runtime.playbacks[SESSION]
        next_reply = models.playback(playback("complete", count=10))
        next_reply["hardExpiresAtMs"] = self.reply["hardExpiresAtMs"]
        self.reply = next_reply
        session["checked"] = 0
        complete = await self.playlist()
        self.assertIn("#EXT-X-ENDLIST", complete.text)
        self.assertEqual(
            [line for line in complete.text.splitlines() if not line.startswith("#")][
                :6
            ],
            children,
        )
        self.client.recordings_clip_prepare.assert_not_awaited()

    async def test_cached_mp4_and_old_bridge_fallback(self):
        self.reply = models.playback(playback("complete", state="ready", count=10))
        self.client.recordings_playback_open.return_value = self.reply
        result = await self.resolve()
        self.assertEqual(result.mime_type, "video/mp4")
        self.assertIn("clip.mp4?", result.url)
        self.assertEqual(self.runtime.playbacks, {})
        self.assertEqual(
            self.client.recordings_playback_command.call_args.args[0], "release"
        )
        del self.client.recordings_capability["progressivePlayback"]
        self.client.recordings_playback_open.reset_mock()
        result = await self.resolve()
        self.assertEqual(result.mime_type, "video/mp4")
        self.client.recordings_clip_prepare.assert_awaited_once()
        self.client.recordings_playback_open.assert_not_awaited()

    async def test_full_expired_table_is_pruned_before_admission(self):
        await self.resolve()
        session = self.runtime.playbacks.pop(SESSION)
        self.runtime.playbacks = {
            str(index): {**session, "idle": 0} for index in range(32)
        }
        self.client.recordings_playback_command.reset_mock()
        result = await self.resolve()
        self.assertEqual(result.mime_type, "application/x-mpegURL")
        self.assertEqual(list(self.runtime.playbacks), [SESSION])
        self.assertEqual(self.runtime.playback_opening, 0)
        self.assertEqual(
            [
                call.args[0]
                for call in self.client.recordings_playback_command.await_args_list
            ],
            ["release"] * 32,
        )

    async def test_completed_history_survives_buffering_and_paused_seek(self):
        for initial in ("playable", "complete"):
            with self.subTest(initial=initial):
                self.reply = models.playback(playback(initial))
                self.client.recordings_playback_open.return_value = self.reply
                await self.resolve()
                self.reply = models.playback(playback("complete", state="ready"))
                session = self.runtime.playbacks[SESSION]
                session["checked"] = 0
                completed = await self.playlist()
                self.assertIn("#EXT-X-ENDLIST", completed.text)
                children = session["signed"].copy()
                paused = time.monotonic() + 120
                with patch(
                    "custom_components.eufy_sdk.recordings_progressive.time.monotonic",
                    return_value=paused,
                ):
                    replay = await self.playlist()
                    self.assertEqual(replay.text, completed.text)
                    self.assertEqual(session["signed"], children)
                    app = web.Application()
                    app[KEY_HASS] = self.hass
                    seek = await EufyRecordingSegmentView().head(
                        make_mocked_request("HEAD", BASE + "/0.ts", app=app),
                        fixture.SCOPE,
                        fixture.CAMERA,
                        fixture.EVENT,
                        SESSION,
                        GENERATION,
                        "0",
                    )
                    self.assertEqual(seek.headers["Content-Length"], "6")
                with (
                    patch(
                        "custom_components.eufy_sdk.recordings_progressive.time.monotonic",
                        return_value=session["deadline"],
                    ),
                    self.assertRaises(web.HTTPGone),
                ):
                    await self.playlist()
                self.client.recordings_clip_prepare.assert_not_awaited()

    async def test_completion_during_long_pause_is_checked_before_retiring(self):
        await self.resolve()
        session = self.runtime.playbacks[SESSION]
        first = await self.playlist()
        self.assertNotIn("#EXT-X-ENDLIST", first.text)
        finished = models.playback(playback("complete", state="ready", count=10))
        finished["hardExpiresAtMs"] = self.reply["hardExpiresAtMs"]
        self.reply = finished
        paused = time.monotonic() + 120
        self.client.recordings_playback_command.reset_mock()
        with patch(
            "custom_components.eufy_sdk.recordings_progressive.time.monotonic",
            return_value=paused,
        ):
            resumed = await self.playlist()
        self.assertIn("#EXT-X-ENDLIST", resumed.text)
        self.assertEqual(
            self.client.recordings_playback_command.call_args.args[0], "renew"
        )
        self.assertEqual(session["idle"], session["deadline"])

    async def test_unfinished_capture_still_expires_after_idle(self):
        await self.resolve()
        await self.playlist()
        paused = time.monotonic() + 120
        with (
            patch(
                "custom_components.eufy_sdk.recordings_progressive.time.monotonic",
                return_value=paused,
            ),
            self.assertRaises(web.HTTPGone),
        ):
            await self.playlist()

    async def test_short_complete_hls_and_head_metadata_do_not_require_a_segment_fetch(
        self,
    ):
        self.reply = models.playback(playback("complete", count=2))
        self.client.recordings_playback_open.return_value = self.reply
        result = await self.resolve()
        self.assertEqual(result.mime_type, "application/x-mpegURL")
        response = await self.playlist()
        self.assertIn("#EXT-X-ENDLIST", response.text)
        app = web.Application()
        app[KEY_HASS] = self.hass
        request = make_mocked_request("HEAD", BASE + "/0.ts", app=app)
        head = await EufyRecordingSegmentView().head(
            request,
            fixture.SCOPE,
            fixture.CAMERA,
            fixture.EVENT,
            SESSION,
            GENERATION,
            "0",
        )
        self.assertEqual(head.headers["Content-Length"], "6")
        self.client._session.get.assert_not_called()

    async def test_session_control_rpc_validates_identity_and_never_retries(self):
        client = fixture.client()
        client.recordings_capability.update(playback=True, progressivePlayback=True)
        client.rpc.return_value = {"ok": True, "recordings": playback()}
        opened = await client.recordings_playback_open(
            fixture.CAMERA, fixture.EVENT, "synthetic-request-key"
        )
        self.assertEqual(client.rpc.call_args.args[0], "recordings.playback_open")
        self.assertEqual(
            client.rpc.call_args.kwargs["requestKey"], "synthetic-request-key"
        )
        result = await client.recordings_playback_command("renew", opened)
        self.assertEqual(result["generation"], GENERATION)
        wire = playback()
        wire["playback"]["sessionId"] = "p_" + "Z" * 22
        client.rpc.return_value = {"ok": True, "recordings": wire}
        with self.assertRaises(models.RecordingsError):
            await client.recordings_playback_command("status", opened)
        client.rpc.side_effect = TimeoutError("private-canary")
        count = client.rpc.call_count
        with self.assertRaises(models.RecordingsError):
            await client.recordings_playback_command("renew", opened)
        self.assertEqual(client.rpc.call_count, count + 1)

    async def test_warmup_renew_and_cancel_release_only_its_lease(self):
        warming = models.playback(playback("warming", count=0))
        self.client.recordings_playback_open.return_value = warming
        entered = asyncio.Event()

        async def command(name, reply):
            if name == "release":
                return {}
            entered.set()
            await asyncio.Event().wait()
            return reply

        self.client.recordings_playback_command.side_effect = command
        task = asyncio.create_task(
            self.runtime.open_playback(fixture.CAMERA, fixture.EVENT)
        )
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(
            self.client.recordings_playback_command.call_args.args[0], "release"
        )
        self.client.recordings_clip_prepare.assert_not_awaited()

    async def test_idle_expiry_stale_generation_revocation_and_unload_fail_closed(self):
        await self.resolve()
        session = self.runtime.playbacks[SESSION]
        session["idle"] = 0
        self.client.recordings_playback_command.reset_mock()
        with self.assertRaises(web.HTTPGone):
            await self.playlist()
        # An idle, unfinished capture asks the bridge once and still expires.
        self.client.recordings_playback_command.assert_awaited_once()
        self.assertEqual(
            self.client.recordings_playback_command.call_args.args[0], "renew"
        )
        session["idle"] = time.monotonic() + 30
        with self.assertRaises(models.RecordingsError):
            await self.runtime.playback_session(
                fixture.CAMERA, fixture.EVENT, SESSION, "wrong"
            )
        self.runtime.events[fixture.CAMERA][fixture.EVENT] = False
        with self.assertRaises(web.HTTPGone):
            await self.playlist()
        self.runtime.close()
        await asyncio.gather(*self.runtime.playback_releases)
        self.assertEqual(self.runtime.playbacks, {})
        self.assertEqual(
            self.client.recordings_playback_command.call_args.args[0], "release"
        )

    async def test_failed_or_changed_history_never_becomes_endlist(self):
        await self.resolve()
        session = self.runtime.playbacks[SESSION]
        for change in ("failed", "history"):
            reply = deepcopy(self.reply)
            if change == "failed":
                reply["delivery"] = "unavailable"
            else:
                reply["segments"][0]["bytes"] += 1
            self.client.recordings_playback_command.side_effect = None
            self.client.recordings_playback_command.return_value = reply
            session["checked"] = 0
            with self.subTest(change=change), self.assertRaises(web.HTTPGone):
                await self.playlist()

    async def test_nested_paths_use_real_ha_auth_middleware(self):
        parent = (await self.resolve()).url
        manifest = await self.playlist()
        child = next(
            line for line in manifest.text.splitlines() if line.startswith(BASE)
        )
        token = SimpleNamespace(id="synthetic-content-user", user=Mock())
        self.hass.auth.async_get_user = AsyncMock(
            return_value=SimpleNamespace(refresh_tokens={"synthetic": token})
        )
        self.hass.auth.async_get_refresh_token = Mock(return_value=token)
        app = web.Application()
        app[KEY_HASS] = self.hass
        store = Mock(async_load=AsyncMock(return_value={"content_user": "synthetic"}))

        class SyntheticStore:
            @classmethod
            def __class_getitem__(cls, item):
                return cls

            def __new__(cls, *_args):
                return store

        with patch.object(auth, "Store", SyntheticStore):
            await auth.async_setup_auth(self.hass, app)
        handler = AsyncMock(return_value=web.Response())
        segment_handler = request_handler_factory(
            self.hass, EufyRecordingSegmentView(), handler
        )
        with (
            patch.object(auth, "async_user_not_allowed_do_auth", return_value=False),
            patch.object(auth, "is_supervisor_unix_socket_request", return_value=False),
            patch(
                "homeassistant.helpers.network.get_url",
                return_value="https://ha.invalid",
            ),
        ):
            for path in (
                BASE + "/0.ts",
                parent.replace("index.m3u8?", "0.ts?"),
                child.replace("/0.ts?", "/1.ts?"),
                auth.async_sign_path(self.hass, BASE + "/0.ts", timedelta(seconds=-1)),
            ):
                with self.subTest(path=path), self.assertRaises(web.HTTPUnauthorized):
                    await app.middlewares[0](
                        make_mocked_request("GET", path, app=app), segment_handler
                    )
            handler.assert_not_awaited()
            result = await app.middlewares[0](
                make_mocked_request("GET", child, app=app), segment_handler
            )
            self.assertEqual(result.status, 200)
        self.assertTrue(EufyRecordingPlaylistView.requires_auth)
        self.assertTrue(EufyRecordingSegmentView.requires_auth)

    async def test_segment_fixed_origin_framing_and_truncation(self):
        await self.resolve()
        app = web.Application()
        app[KEY_HASS] = self.hass
        segment = EufyRecordingSegmentView()
        for content_type, length, chunks, status, okay in (
            ("video/mp2t", "6", [b"abc", b"def"], 200, True),
            ("text/html", "6", [b"abcdef"], 200, False),
            ("video/mp2t", "9", [b"abcdefghi"], 200, False),
            ("video/mp2t", "6", [], 302, False),
            ("video/mp2t", "6", [b"abc"], 200, True),
        ):
            upstream = Mock(
                status=status,
                headers={"Content-Type": content_type, "Content-Length": length},
            )

            async def iterate(_size, rows=chunks):
                for chunk in rows:
                    yield chunk

            upstream.content.iter_chunked = iterate
            context = Mock(
                __aenter__=AsyncMock(return_value=upstream),
                __aexit__=AsyncMock(return_value=False),
            )
            self.client._session.get.return_value = context
            request = make_mocked_request("GET", BASE + "/0.ts", app=app)
            args = (
                request,
                fixture.SCOPE,
                fixture.CAMERA,
                fixture.EVENT,
                SESSION,
                GENERATION,
                "0",
            )
            if okay:
                result = await segment.get(*args)
                self.assertEqual(result.status, 200)
                if sum(map(len, chunks)) != 6:
                    self.assertFalse(result.keep_alive)
            else:
                with self.assertRaises(web.HTTPGone):
                    await segment.get(*args)
            call = self.client._session.get.call_args
            self.assertEqual(
                str(call.args[0]),
                f"http://bridge.invalid:3000/recordings/v1/playback/{SESSION}/{GENERATION}/0.ts",
            )
            self.assertFalse(call.kwargs["allow_redirects"])
            self.assertFalse(call.kwargs["auto_decompress"])
            context.__aexit__.assert_awaited_once()

    def test_playable_threshold_is_the_bridge_choice_above_one_second(self):
        # The bridge owns the configurable threshold (1-6 s); HA keeps only the floor.
        for count in (1, 3, 6):
            with self.subTest(count=count):
                self.assertEqual(
                    len(models.playback(playback(count=count))["segments"]), count
                )
        wire = playback(count=1)
        wire["playback"]["segments"][0]["durationMs"] = 999
        with self.assertRaises(models.RecordingsError):
            models.playback(wire)

    def test_inventory_and_capability_validation(self):
        for change in ("url", "size", "duration", "order", "short", "false_complete"):
            wire = playback()
            row = wire["playback"]
            if change == "url":
                row["segments"][0]["url"] = "https://untrusted.invalid"
            elif change == "size":
                row["segments"][0]["bytes"] = 2**30
            elif change == "duration":
                row["segments"][0]["durationMs"] = 2001
            elif change == "order":
                row["segments"][0]["sequence"] = 1
            elif change == "short":
                row["segments"] = []
            else:
                row["delivery"] = "complete"
            with self.subTest(change=change), self.assertRaises(models.RecordingsError):
                models.playback(wire)
