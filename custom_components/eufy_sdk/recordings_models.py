# ruff: noqa: PLR2004, FBT001 - protocol bounds and assertion helper
"""Strict, privacy-preserving projections of the recordings v1 contract."""

from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

TIMEZONE = "America/Chicago"
DISCLOSURE = "latest 20 station-wide; history incomplete"
COVER_CAP = 524288
ERROR = "Recordings unavailable"
MAX_TIME_MS = 253402300799999


class RecordingsError(ValueError):
    """Reject wire data without retaining its contents."""

    def __init__(self) -> None:
        """Use a fixed message for every validation failure."""
        super().__init__(ERROR)


def require(condition: bool) -> None:
    """Fail with a safe, fixed message."""
    if not condition:
        raise RecordingsError


def opaque_id(value: Any, prefix: str) -> str:
    """Validate an opaque, domain-specific identity."""
    require(
        isinstance(value, str)
        and re.fullmatch(prefix + r"_[A-Za-z0-9_-]{22}", value) is not None
    )
    return value


def token(value: Any) -> str:
    """Validate a bounded opaque revision or interest token."""
    require(
        isinstance(value, str)
        and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) is not None
    )
    return value


def number(value: Any, *, nullable: bool = False, maximum: int = 2**53 - 1) -> Any:
    """Validate nonnegative finite safe integers, excluding booleans."""
    require(
        (nullable and value is None) or (type(value) is int and 0 <= value <= maximum)
    )
    return value


def fields(value: dict, required: tuple[str, ...]) -> None:
    """Distinguish explicit null from missing wire fields."""
    require(all(key in value for key in required))


def no_selectors(value: dict) -> None:
    """Reject forbidden transport and source selectors even as extra fields."""
    forbidden = {
        "url",
        "sourceurl",
        "bridgeurl",
        "path",
        "thumbpath",
        "videopath",
        "selector",
        "coverselector",
        "transfer",
        "transferid",
    }
    require(
        all(
            isinstance(key, str) and key.lower().replace("_", "") not in forbidden
            for key in value
        )
    )


def time_ms(value: Any, *, nullable: bool = False) -> int | None:
    """Keep calendar timestamps within the supported display range."""
    return number(value, nullable=nullable, maximum=MAX_TIME_MS)


def choice(value: Any, options: tuple) -> Any:
    """Accept only a fixed scalar enum."""
    require(isinstance(value, str) and value in options)
    return value


def flag(value: Any) -> bool:
    """Require a JSON boolean."""
    require(type(value) is bool)
    return value


def timestamp(value: Any, *, local: bool = False) -> str | None:
    """Validate bounded RFC3339, or the explicitly unzoned source syntax."""
    if value is None:
        return None
    pattern = (
        r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}"
        if local
        else r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?(?:Z|[+-]\d{2}:\d{2})"
    )
    require(isinstance(value, str) and re.fullmatch(pattern, value) is not None)
    try:
        datetime.fromisoformat(value)
    except ValueError:
        raise RecordingsError from None
    return value


def capability(value: Any) -> dict | None:
    """Treat absent, disabled or malformed additive capability as unsupported."""
    try:
        require(isinstance(value, dict))
        require(type(value.get("version")) is int and value["version"] == 1)
        playback = value.get("playback") is True
        require(value.get("enabled") is True)
        require(value.get("browseOnly") is (not playback))
        require(value.get("timezone") == TIMEZONE)
        for key, expected in (
            ("stationWindow", 20),
            ("pageSize", 100),
        ):
            require(type(value.get(key)) is int and value[key] == expected)
        require(
            type(value.get("coverBudget")) is int and value["coverBudget"] in (4, 20)
        )
        result = {"scopeId": opaque_id(value.get("scopeId"), "s")}
        # This flag is advertised only when the bridge playback switch is on.
        if playback:
            result["playback"] = True
            if value.get("progressivePlayback") is True:
                result["progressivePlayback"] = True
    except RecordingsError:
        return None
    else:
        return result


def envelope(value: Any) -> dict:
    """Require the additive payload version."""
    require(
        isinstance(value, dict)
        and type(value.get("version")) is int
        and value["version"] == 1
    )
    no_selectors(value)
    return value


def playback(value: Any) -> dict:
    """Project a bounded, contiguous immutable segment inventory without URLs."""
    data = envelope(value).get("playback")
    require(isinstance(data, dict))
    no_selectors(data)
    result = {
        "sessionId": opaque_id(data.get("sessionId"), "p"),
        "generation": token(data.get("generation")),
        "cameraId": opaque_id(data.get("cameraId"), "c"),
        "eventId": opaque_id(data.get("eventId"), "e"),
        "hardExpiresAtMs": time_ms(data.get("hardExpiresAtMs")),
        "capture": choice(
            data.get("capture"), ("queued", "pulling", "verified_eof", "failed")
        ),
        "delivery": choice(
            data.get("delivery"), ("warming", "playable", "complete", "unavailable")
        ),
        "cache": choice(data.get("cache"), ("absent", "finalizing", "ready", "failed")),
        "cleanup": choice(data.get("cleanup"), ("pending", "settled", "uncertain")),
        "clip": clip({"clip": data.get("clip")}),
    }
    require(re.fullmatch(r"[a-f0-9-]{36}", result["generation"]) is not None)
    reason = data.get("reason")
    choice(
        reason,
        (
            "none",
            "live_view",
            "busy",
            "owner_unavailable",
            "not_found",
            "too_long",
            "storage",
            "convert_failed",
            "station_error",
            "cleanup_uncertain",
            "experiment_only",
            "disabled",
        ),
    )
    result["reason"] = reason
    segments = data.get("segments")
    require(isinstance(segments, list) and len(segments) <= 3600)
    result["segments"] = []
    start = 0
    for index, asset in enumerate(segments):
        require(isinstance(asset, dict))
        no_selectors(asset)
        sequence = number(asset.get("sequence"), maximum=3599)
        require(sequence == index and number(asset.get("startMs")) == start)
        duration = number(asset.get("durationMs"), maximum=2000)
        size = number(asset.get("bytes"), maximum=16 * 1024**2)
        require(duration > 0 and size > 0)
        result["segments"].append(
            {
                "sequence": sequence,
                "startMs": start,
                "durationMs": duration,
                "bytes": size,
            }
        )
        start += duration
    require(start <= 3_600_000)
    if result["delivery"] == "playable":
        require(start >= 6000)
    if result["delivery"] == "complete":
        require(start > 0 and result["capture"] == "verified_eof")
    return result


def cameras(value: Any, scope: str) -> dict:
    """Project exactly five enrolled camera folders."""
    data = envelope(value)
    require(data.get("scopeId") == scope)
    rows = data.get("cameras")
    require(isinstance(rows, list) and 1 <= len(rows) <= 2000)
    result = []
    for row in rows:
        require(isinstance(row, dict) and row.get("scopeId") == scope)
        no_selectors(row)
        name = row.get("name")
        require(
            isinstance(name, str)
            and 1 <= len(name) <= 128
            and all(ord(c) >= 32 for c in name)
        )
        result.append(
            {
                "cameraId": opaque_id(row.get("cameraId"), "c"),
                "scopeId": scope,
                "name": name,
            }
        )
    require(len({row["cameraId"] for row in result}) == len(result))
    return {"version": 1, "scopeId": scope, "cameras": result}


def kind(value: Any) -> dict:
    """Construct display labels locally; never echo arbitrary vendor strings."""
    require(isinstance(value, dict))
    no_selectors(value)
    fields(value, ("code", "label"))
    require(isinstance(value["label"], str) and len(value["label"]) <= 128)
    code = number(value.get("code"), nullable=True, maximum=65535)
    return {"code": code, "label": "Unknown" if code is None else f"Event {code}"}


def event(value: Any, camera: str) -> dict:
    """Allowlist an event without retaining selectors or vendor strings."""
    require(isinstance(value, dict) and value.get("cameraId") == camera)
    no_selectors(value)
    require(value.get("timezone") == TIMEZONE)
    fields(value, ("startedAtMs", "endedAtMs", "durationSeconds"))
    result = {
        "eventId": opaque_id(value.get("eventId"), "e"),
        "cameraId": camera,
        "playable": flag(value.get("playable")),
        "recordingAvailable": flag(value.get("recordingAvailable", True)),
        "timezone": TIMEZONE,
    }
    for key in ("startedAtMs", "endedAtMs"):
        result[key] = time_ms(value.get(key), nullable=True)
    for key in ("firstObservedAtMs", "lastObservedAtMs"):
        result[key] = time_ms(value.get(key))
    for key in ("startedAtUtc", "startedAtLocal", "sourceStartLocal"):
        require(key in value)
        result[key] = timestamp(value[key], local=key == "sourceStartLocal")
    result["timeStatus"] = choice(
        value.get("timeStatus"), ("valid", "unknown_format", "ambiguous", "invalid")
    )
    if result["timeStatus"] != "valid":
        require(
            all(
                result[key] is None
                for key in (
                    "startedAtMs",
                    "endedAtMs",
                    "startedAtUtc",
                    "startedAtLocal",
                )
            )
        )
    else:
        require(
            all(
                result[key] is not None
                for key in ("startedAtMs", "startedAtUtc", "startedAtLocal")
            )
        )
        utc = datetime.fromisoformat(result["startedAtUtc"])
        local = datetime.fromisoformat(result["startedAtLocal"])
        require(result["startedAtUtc"].endswith("Z"))
        require(utc.year >= 1970)
        require(int(utc.timestamp() * 1000) == result["startedAtMs"])
        chicago = utc.astimezone(ZoneInfo(TIMEZONE))
        require(local == utc and local.utcoffset() == chicago.utcoffset())
        require(local.replace(tzinfo=None) == chicago.replace(tzinfo=None))
        if result["sourceStartLocal"] is not None:
            require(result["sourceStartLocal"] == chicago.strftime("%Y-%m-%d %H:%M:%S"))
    if result["timeStatus"] == "unknown_format":
        require(result["sourceStartLocal"] is None)
    duration = value.get("durationSeconds")
    require(
        duration is None
        or (
            type(duration) in (int, float)
            and math.isfinite(duration)
            and 0 <= duration <= 86400
        )
    )
    result["durationSeconds"] = duration
    result["durationBasis"] = choice(
        value.get("durationBasis"),
        ("end_minus_start", "qualified_duration_field", "unknown"),
    )
    for key in ("eventType", "detectType"):
        result[key] = kind(value.get(key))
    result["stationPresence"] = choice(
        value.get("stationPresence"), ("seen_in_latest_window", "not_in_latest_window")
    )
    result["coverState"] = choice(
        value.get("coverState"),
        ("not_requested", "pending", "cached", "unavailable", "evicted"),
    )
    require("coverRevision" in value)
    result["coverRevision"] = (
        token(value["coverRevision"]) if value["coverRevision"] is not None else None
    )
    return result


def catalog(value: Any, scope: str, camera: str) -> dict:
    """Project the amended single-page, bounded catalog."""
    data = envelope(value)
    fields(
        data,
        ("lastSuccessfulQueryAtMs", "lastAttemptAtMs", "browseId", "browseExpiresAtMs"),
    )
    require(data.get("scopeId") == scope and data.get("cameraId") == camera)
    require(
        data.get("historyIncomplete") is True
        and type(data.get("stationWindow")) is int
        and data["stationWindow"] == 20
        and data.get("disclosure") == DISCLOSURE
    )
    rows = data.get("events")
    require(isinstance(rows, list) and len(rows) <= 100)
    rows = [event(row, camera) for row in rows]
    ids = {row["eventId"] for row in rows}
    require(len(ids) == len(rows))
    eligible = data.get("coverEligibleEventIds")
    require(isinstance(eligible, list) and len(eligible) <= 20)
    require(all(opaque_id(item, "e") in ids for item in eligible))
    require(len(set(eligible)) == len(eligible))
    require((data["browseId"] is None) == (data["browseExpiresAtMs"] is None))
    if data["browseId"] is not None:
        require(
            isinstance(data["browseId"], str)
            and re.fullmatch(r"[A-Za-z0-9_-]{22}", data["browseId"]) is not None
        )
    refresh = data.get("refresh")
    require(isinstance(refresh, dict))
    fields(refresh, ("retryAfterMs",))
    refresh = {
        "state": choice(
            refresh.get("state"),
            ("idle", "scheduled", "deferred", "rate_limited", "failed"),
        ),
        "reason": choice(
            refresh.get("reason"),
            (
                "none",
                "busy",
                "playback",
                "renewal",
                "refresh_hold",
                "offline",
                "scope_invalid",
                "rate_limited",
                "timeout",
                "invalid_reply",
                "store_unavailable",
            ),
        ),
        "retryAfterMs": number(refresh.get("retryAfterMs"), nullable=True),
    }
    rows.sort(
        key=lambda row: (
            row["startedAtMs"] is None,
            -(
                row["startedAtMs"]
                if row["startedAtMs"] is not None
                else row["firstObservedAtMs"]
            ),
            row["eventId"],
        )
    )
    return {
        "version": 1,
        "scopeId": scope,
        "cameraId": camera,
        "revision": token(data.get("revision")),
        "events": rows,
        "truncated": flag(data.get("truncated")),
        "historyIncomplete": True,
        "stationWindow": 20,
        "disclosure": DISCLOSURE,
        "refresh": refresh,
        "dirty": flag(data.get("dirty")),
        "lastSuccessfulQueryAtMs": time_ms(
            data.get("lastSuccessfulQueryAtMs"), nullable=True
        ),
        "lastAttemptAtMs": time_ms(data.get("lastAttemptAtMs"), nullable=True),
        "browseId": token(data["browseId"])
        if data.get("browseId") is not None
        else None,
        "browseExpiresAtMs": time_ms(data.get("browseExpiresAtMs"), nullable=True),
        "coverEligibleEventIds": list(eligible),
    }


def cover(value: Any, camera: str, event_id: str) -> dict:
    """Validate ready metadata before using the fixed cache-only HTTP route."""
    data = envelope(value)
    fields(data, ("retryAfterMs",))
    require(data.get("cameraId") == camera and data.get("eventId") == event_id)
    result = {
        "version": 1,
        "cameraId": camera,
        "eventId": event_id,
        "state": choice(
            data.get("state"),
            ("ready", "pending", "deferred", "unavailable", "evicted"),
        ),
        "retryAfterMs": number(data.get("retryAfterMs"), nullable=True),
    }
    if result["state"] == "ready":
        require(
            isinstance(data.get("sha256"), str)
            and re.fullmatch(r"[0-9a-f]{64}", data["sha256"]) is not None
        )
        result.update(
            bytes=number(data.get("bytes"), maximum=COVER_CAP),
            sha256=data["sha256"],
            coverRevision=token(data.get("coverRevision")),
        )
        require(result["bytes"] > 0)
    return result


def clip(value: Any) -> dict:
    """Project clip status without retaining transport selectors or wire text."""
    require(isinstance(value, dict))
    no_selectors(value)
    row = value.get("clip")
    require(isinstance(row, dict))
    no_selectors(row)
    fields(row, ("state", "reason", "progress", "durationMs", "bytes", "expiresAtMs"))
    progress = row["progress"]
    require(isinstance(progress, dict))
    no_selectors(progress)
    fields(progress, ("pulledMs", "expectedMs"))
    return {
        "state": choice(
            row["state"],
            (
                "idle",
                "queued",
                "pulling",
                "converting",
                "ready",
                "failed",
                "refused",
                "cancelled",
            ),
        ),
        "reason": choice(
            row["reason"],
            (
                "none",
                "live_view",
                "busy",
                "owner_unavailable",
                "not_found",
                "too_long",
                "storage",
                "convert_failed",
                "station_error",
                "cleanup_uncertain",
                "disabled",
            ),
        ),
        "progress": {
            "pulledMs": number(progress["pulledMs"], maximum=86400000),
            "expectedMs": number(
                progress["expectedMs"], nullable=True, maximum=86400000
            ),
        },
        "durationMs": number(row["durationMs"], nullable=True, maximum=86400000),
        "bytes": number(row["bytes"], nullable=True),
        "expiresAtMs": time_ms(row["expiresAtMs"], nullable=True),
    }


def diagnostics(value: Any) -> dict:
    """Project only counts, display names, fixed statuses and masked time shapes."""
    require(isinstance(value, dict))
    cameras = value.get("rowsKeptPerCamera")
    require(isinstance(cameras, list) and len(cameras) <= 2000)
    kept = []
    for row in cameras:
        require(isinstance(row, dict))
        name = row.get("name")
        require(
            isinstance(name, str)
            and 1 <= len(name) <= 128
            and all(ord(c) >= 32 and not 127 <= ord(c) <= 159 for c in name)
        )
        kept.append({"name": name.strip(), "rows": number(row.get("rows"))})
    rejected = value.get("rejected")
    times = value.get("timeStatus")
    covers = value.get("coverCache")
    require(all(isinstance(row, dict) for row in (rejected, times, covers)))
    shapes = value.get("unknownStartTimeShapes")
    require(isinstance(shapes, list) and len(shapes) <= 32)
    masked = []
    for row in shapes:
        require(isinstance(row, dict))
        shape = row.get("shape")
        require(
            isinstance(shape, str)
            and re.fullmatch(r"[9a :/.+?\-]{0,32}", shape) is not None
        )
        masked.append({"shape": shape, "count": number(row.get("count"))})
    return {
        "rowsKeptPerCamera": kept,
        "rejected": {
            key: number(rejected.get(key))
            for key in ("not_in_scope", "bad_record_id", "bad_start_time", "other")
        },
        "identity_conflict": number(value.get("identity_conflict")),
        "timeStatus": {
            key: number(times.get(key))
            for key in ("valid", "unknown_format", "ambiguous", "invalid")
        },
        "pagingStatus": choice(
            value.get("pagingStatus"), ("unverified", "working", "unsupported_stopped")
        ),
        "lastPageCount": number(value.get("lastPageCount"), maximum=20),
        "lastCatalogTime": time_ms(value.get("lastCatalogTime"), nullable=True),
        "coverCache": {
            key: number(covers.get(key)) for key in ("files", "bytes", "pending")
        },
        "unknownStartTimeShapes": masked,
    }
