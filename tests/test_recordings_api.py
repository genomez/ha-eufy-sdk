# ruff: noqa: ANN001, ANN202, ANN204, PT027, ANN201, D100, D101, D102, INP001, PT009, SLF001
import unittest
from copy import deepcopy
from unittest.mock import AsyncMock, Mock

import aiohttp
from fixtures import recordings_v1 as fixture

from custom_components.eufy_sdk import recordings_models as models
from custom_components.eufy_sdk.recordings import RecordingRuntime


class ContractTests(unittest.TestCase):
    def test_recording_availability_is_optional_and_strictly_boolean(self):
        for available in (True, False):
            row = {**fixture.event(), "recordingAvailable": available}
            self.assertIs(
                models.event(row, fixture.CAMERA)["recordingAvailable"], available
            )
        self.assertTrue(
            models.event(fixture.event(), fixture.CAMERA)["recordingAvailable"] is True
        )
        for invalid in (None, 0, 1, "false"):
            with (
                self.subTest(invalid=invalid),
                self.assertRaises(models.RecordingsError),
            ):
                models.event(
                    {**fixture.event(), "recordingAvailable": invalid}, fixture.CAMERA
                )

    def test_cover_budget_accepts_legacy_and_hundred_and_rejects_larger_values(self):
        for budget in (4, 20, 21, 100):
            self.assertEqual(
                models.capability({**fixture.capability(), "coverBudget": budget}),
                {"scopeId": fixture.SCOPE},
            )
        for budget in (True, False, 0, -1, 101, 100.0, "100"):
            self.assertIsNone(
                models.capability({**fixture.capability(), "coverBudget": budget})
            )

    def test_eligible_list_accepts_twenty_and_hundred_and_rejects_larger_lists(self):
        rows = [fixture.event("e_" + str(index).zfill(22)) for index in range(100)]
        page = fixture.page(rows)
        for count in (20, 100):
            page["coverEligibleEventIds"] = [row["eventId"] for row in rows[:count]]
            self.assertEqual(
                len(
                    models.catalog(page, fixture.SCOPE, fixture.CAMERA)[
                        "coverEligibleEventIds"
                    ]
                ),
                count,
            )
        page["coverEligibleEventIds"].append(rows[0]["eventId"])
        with self.assertRaises(models.RecordingsError):
            models.catalog(page, fixture.SCOPE, fixture.CAMERA)

    def test_eligible_list_still_requires_unique_members_of_the_page(self):
        for eligible in ([fixture.EVENT, fixture.EVENT], [fixture.OTHER]):
            with self.assertRaises(models.RecordingsError):
                models.catalog(
                    {**fixture.page(), "coverEligibleEventIds": eligible},
                    fixture.SCOPE,
                    fixture.CAMERA,
                )

    def test_missing_nullable_fields_and_mismatched_event_times_are_rejected(self):
        for field in ("startedAtMs", "endedAtMs", "durationSeconds", "coverRevision"):
            row = fixture.event()
            del row[field]
            with self.subTest(field=field), self.assertRaises(models.RecordingsError):
                models.event(row, fixture.CAMERA)
        for changes in (
            {"startedAtUtc": "2026-11-04T19:00:00Z"},
            {"startedAtLocal": "2026-11-04T13:00:00-05:00"},
            {"startedAtMs": 2**53 - 1},
        ):
            with (
                self.subTest(changes=changes),
                self.assertRaises(models.RecordingsError),
            ):
                models.event({**fixture.event(), **changes}, fixture.CAMERA)
        for field in (
            "browseId",
            "browseExpiresAtMs",
            "lastAttemptAtMs",
            "lastSuccessfulQueryAtMs",
        ):
            page = fixture.page()
            del page[field]
            with self.subTest(field=field), self.assertRaises(models.RecordingsError):
                models.catalog(page, fixture.SCOPE, fixture.CAMERA)

    def test_capability_absent_disabled_malformed_and_projection(self):
        self.assertIsNone(models.capability(None))
        for changes in (
            {"enabled": False},
            {"version": True},
            {"scopeId": "bad"},
            {"stationWindow": True},
            {"timezone": "Invalid/Zone"},
        ):
            self.assertIsNone(models.capability({**fixture.capability(), **changes}))
        self.assertEqual(
            models.capability(fixture.capability()), {"scopeId": fixture.SCOPE}
        )

    def test_finite_strict_catalog_validation(self):
        for changes in (
            {"startedAtMs": False},
            {"durationSeconds": float("nan")},
            {"durationSeconds": float("inf")},
            {"durationSeconds": True},
            {"eventId": "../event"},
            {"playable": "true"},
            {"sourceStartLocal": "2026-02-31 12:00:00"},
            {"timeStatus": "ambiguous"},
            {"eventType": {"code": 65536}},
        ):
            with (
                self.subTest(changes=changes),
                self.assertRaises(models.RecordingsError),
            ):
                models.catalog(
                    fixture.page([{**fixture.event(), **changes}]),
                    fixture.SCOPE,
                    fixture.CAMERA,
                )
        for changes in (
            {"truncated": 0},
            {"events": [fixture.event()] * 101},
            {"cameraId": "c_" + "Z" * 22},
            {"coverEligibleEventIds": [fixture.OTHER]},
        ):
            with (
                self.subTest(changes=changes),
                self.assertRaises(models.RecordingsError),
            ):
                models.catalog(
                    {**fixture.page(), **changes}, fixture.SCOPE, fixture.CAMERA
                )

    def test_null_time_and_ordering(self):
        unknown = {
            **fixture.event(fixture.OTHER),
            "startedAtMs": None,
            "endedAtMs": None,
            "startedAtUtc": None,
            "startedAtLocal": None,
            "sourceStartLocal": "2026-11-01 01:30:00",
            "timeStatus": "ambiguous",
        }
        result = models.catalog(
            fixture.page([unknown, fixture.event()]), fixture.SCOPE, fixture.CAMERA
        )
        self.assertEqual(
            [row["eventId"] for row in result["events"]], [fixture.EVENT, fixture.OTHER]
        )
        self.assertEqual(result["events"][1]["timeStatus"], "ambiguous")

    def test_camera_count_scope_and_uniqueness(self):
        result = models.cameras(fixture.cameras(), fixture.SCOPE)
        self.assertEqual(len(result["cameras"]), 5)
        grown = [
            *result["cameras"],
            {**result["cameras"][0], "cameraId": "c_" + "Z" * 22},
        ]
        self.assertEqual(
            len(
                models.cameras({**fixture.cameras(), "cameras": grown}, fixture.SCOPE)[
                    "cameras"
                ]
            ),
            6,
        )
        for rows in ([], [result["cameras"][0]] * 5):
            with self.assertRaises(models.RecordingsError):
                models.cameras({**fixture.cameras(), "cameras": rows}, fixture.SCOPE)


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_3o_style_bridge_five_camera_roster_is_accepted(self):
        client = fixture.client()
        roster = fixture.cameras()
        self.assertEqual(len(roster["cameras"]), 5)
        client.rpc.return_value = {"ok": True, "recordings": roster}
        runtime = RecordingRuntime(client)
        self.addCleanup(runtime.close)

        self.assertEqual(await runtime.list_cameras(), roster["cameras"])
        self.assertEqual(
            list(runtime.cameras), [row["cameraId"] for row in roster["cameras"]]
        )
        client.rpc.assert_awaited_once_with("recordings.cameras", timeout=3)

    async def test_scope_replacement_during_rpc_is_a_safe_failure(self):
        client = fixture.client()

        async def reply(*_args: object, **_kwargs: object):
            client.recordings_capability = None
            return {"ok": True, "recordings": fixture.cameras()}

        client.rpc.side_effect = reply
        with self.assertRaises(models.RecordingsError):
            await client.recordings_cameras()

    async def test_exact_ws_commands_fields_and_timeout(self):
        client = fixture.client()
        client.rpc.side_effect = [
            {"ok": True, "recordings": fixture.cameras()},
            {"ok": True, "recordings": fixture.page()},
            {"ok": True, "recordings": fixture.cover("pending")},
        ]
        await client.recordings_cameras()
        await client.recordings_catalog(
            fixture.CAMERA, refresh="if_due", focus_event_id=fixture.EVENT
        )
        await client.recordings_cover(
            fixture.CAMERA, fixture.EVENT, browse_id="B" * 22, request_if_admitted=True
        )
        self.assertEqual(client.rpc.await_args_list[0].args, ("recordings.cameras",))
        self.assertEqual(
            client.rpc.await_args_list[1].kwargs,
            {
                "timeout": 3,
                "cameraId": fixture.CAMERA,
                "refresh": "if_due",
                "focusEventId": fixture.EVENT,
            },
        )
        self.assertEqual(
            client.rpc.await_args_list[2].kwargs,
            {
                "timeout": 3,
                "cameraId": fixture.CAMERA,
                "eventId": fixture.EVENT,
                "browseId": "B" * 22,
                "mode": "request_if_admitted",
            },
        )

    async def test_unsupported_and_rpc_timeout_never_retry(self):
        client = fixture.client()
        client.recordings_capability = None
        with self.assertRaises(models.RecordingsError):
            await client.recordings_cameras()
        client.rpc.assert_not_awaited()
        client.recordings_capability = {"scopeId": fixture.SCOPE}
        client.rpc.side_effect = TimeoutError("synthetic-private-canary")
        with self.assertRaisesRegex(models.RecordingsError, "^Recordings unavailable$"):
            await client.recordings_cameras()
        client.rpc.assert_awaited_once()

    async def test_cover_transfer_fixed_origin_bounded_headers_and_bytes(self):
        client = fixture.client()
        data = fixture.jpeg()
        ready = fixture.cover(data=data)

        async def chunks(size):
            self.assertEqual(size, 65536)
            yield data

        response = Mock(
            status=200,
            headers={
                "Content-Type": "image/jpeg",
                "Content-Length": str(len(data)),
                "X-Eufy-Image-Sha256": ready["sha256"],
                "X-Eufy-Event-Time": "2026-11-04T18:00:00Z",
            },
        )
        response.content.iter_chunked = chunks
        context = Mock(
            __aenter__=AsyncMock(return_value=response),
            __aexit__=AsyncMock(return_value=False),
        )
        client._session.get.return_value = context
        result = await client.fetch_recordings_cover(
            fixture.CAMERA, fixture.EVENT, ready
        )
        self.assertEqual(result, (data, "2026-11-04T18:00:00Z"))
        call = client._session.get.call_args
        self.assertEqual(
            str(call.args[0]),
            f"http://bridge.invalid:3000/recordings/v1/covers/{fixture.CAMERA}/{fixture.EVENT}",
        )
        self.assertFalse(call.kwargs["allow_redirects"])
        self.assertEqual(call.kwargs["timeout"].total, 10)
        self.assertNotIn("headers", call.kwargs)
        for changes in (
            {"Content-Length": "999999"},
            {"X-Eufy-Image-Sha256": "0" * 64},
            {"Content-Type": "text/html"},
            {"X-Eufy-Event-Time": "untrusted"},
        ):
            original = deepcopy(response.headers)
            response.headers.update(changes)
            with self.assertRaises(models.RecordingsError):
                await client.fetch_recordings_cover(
                    fixture.CAMERA, fixture.EVENT, ready
                )
            response.headers = original
        response.status = 302
        with self.assertRaises(models.RecordingsError):
            await client.fetch_recordings_cover(fixture.CAMERA, fixture.EVENT, ready)

    async def test_hello_captures_capability_and_old_hello_removes_it(self):
        client = fixture.client()
        client._closing = True
        # The real receive loop consumes two synthetic TEXT messages.

        class Frames:
            closed = False

            def __aiter__(self):
                async def frames():
                    for payload in (
                        {"event": "hello", "recordings": fixture.capability()},
                        {"event": "hello"},
                    ):
                        yield Mock(
                            type=aiohttp.WSMsgType.TEXT, json=Mock(return_value=payload)
                        )

                return frames()

        client._ws = Frames()
        await client._receive_loop()
        self.assertIsNone(client.recordings_capability)
