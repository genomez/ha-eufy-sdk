"""Synthetic recordings v1 fixtures, amended 2026-11-05; no captured data."""

from __future__ import annotations

import hashlib
import time
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from homeassistant.config_entries import ConfigEntryState
from PIL import Image

from custom_components.eufy_sdk.api import EufySdkApiClient
from custom_components.eufy_sdk.recordings import RecordingRuntime
from custom_components.eufy_sdk.recordings_models import DISCLOSURE, TIMEZONE

SCOPE = "s_" + "S" * 22
CAMERA = "c_" + "C" * 22
EVENT = "e_" + "E" * 22
OTHER = "e_" + "F" * 22


def capability(*, playback: bool | None = None) -> dict:
    """Mirror recordings.mjs base(); omit playback for a legacy bridge."""
    result = {
        "version": 1,
        "browseOnly": playback is not True,
        "enabled": True,
        "scopeId": SCOPE,
        "timezone": TIMEZONE,
        "stationWindow": 20,
        "pageSize": 100,
        "coverBudget": 4,
    }
    if playback is not None:
        result["playback"] = playback
    return result


def cameras() -> dict:
    """Return five synthetic labels and opaque identities."""
    return {
        "version": 1,
        "scopeId": SCOPE,
        "cameras": [
            {
                "scopeId": SCOPE,
                "cameraId": "c_" + letter * 22,
                "name": f"Synthetic camera {index}",
            }
            for index, letter in enumerate("CDEFG")
        ],
    }


def event(event_id: str = EVENT, *, playable: bool = False) -> dict:
    """Mirror publicRecording(): playable reflects private event authority."""
    return {
        "eventId": event_id,
        "cameraId": CAMERA,
        "startedAtMs": 1793815200000,
        "endedAtMs": 1793815210000,
        "startedAtUtc": "2026-11-04T18:00:00Z",
        "startedAtLocal": "2026-11-04T12:00:00-06:00",
        "sourceStartLocal": "2026-11-04 12:00:00",
        "timezone": TIMEZONE,
        "timeStatus": "valid",
        "durationSeconds": 10,
        "durationBasis": "end_minus_start",
        "eventType": {"code": 7, "label": "Event 7"},
        "detectType": {"code": None, "label": "Unknown"},
        "firstObservedAtMs": 1793815200000,
        "lastObservedAtMs": 1793815200000,
        "stationPresence": "not_in_latest_window",
        "coverState": "not_requested",
        "coverRevision": None,
        "playable": playable,
    }


def page(rows: list | None = None) -> dict:
    """Return a single bounded catalog with a short-lived camera interest."""
    return {
        "version": 1,
        "scopeId": SCOPE,
        "cameraId": CAMERA,
        "revision": "revision_1",
        "events": [event()] if rows is None else rows,
        "truncated": False,
        "historyIncomplete": True,
        "stationWindow": 20,
        "disclosure": DISCLOSURE,
        "lastSuccessfulQueryAtMs": None,
        "lastAttemptAtMs": None,
        "dirty": False,
        "refresh": {"state": "scheduled", "reason": "none", "retryAfterMs": None},
        "browseId": "B" * 22,
        "browseExpiresAtMs": int(time.time() * 1000) + 60000,
        "coverEligibleEventIds": [
            row["eventId"] for row in ([event()] if rows is None else rows)
        ][:4],
    }


def jpeg() -> bytes:
    """Create a tiny synthetic cover entirely in RAM."""
    output = BytesIO()
    Image.new("RGB", (2, 2), (128, 128, 128)).save(output, "JPEG")
    return output.getvalue()


def cover(state: str = "ready", data: bytes | None = None) -> dict:
    """Amended cover reply has no grant, transfer, selector or URL."""
    result = {
        "version": 1,
        "cameraId": CAMERA,
        "eventId": EVENT,
        "state": state,
        "retryAfterMs": None,
    }
    if state == "ready":
        data = jpeg() if data is None else data
        result.update(
            bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            coverRevision="cover_1",
        )
    return result


def client() -> EufySdkApiClient:
    """Use the real API wrappers with a mocked bridge transport."""
    api = EufySdkApiClient("bridge.invalid", 3000, Mock())
    api.recordings_capability = {"scopeId": SCOPE}
    api.rpc = AsyncMock()
    return api


async def runtime(*, playable: bool = False) -> RecordingRuntime:
    """Return a browsed runtime with no actual I/O."""
    api = client()
    api.rpc.side_effect = [
        {"ok": True, "recordings": cameras()},
        {"ok": True, "recordings": page([event(playable=playable)])},
    ]
    result = RecordingRuntime(api)
    await result.list_cameras()
    await result.browse(CAMERA)
    api.rpc.reset_mock(side_effect=True)
    return result


def hass(runtime: RecordingRuntime) -> Mock:
    """Discover one loaded entry, without HA startup or a server."""
    result = Mock()
    result.config_entries.async_entries.return_value = [
        SimpleNamespace(
            state=ConfigEntryState.LOADED,
            runtime_data=SimpleNamespace(recordings=runtime),
        )
    ]
    return result
