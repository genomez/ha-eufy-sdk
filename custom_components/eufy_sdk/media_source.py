# ruff: noqa: PLR2004 - fixed media path grammar
"""Home Assistant recording browser with opt-in saved clip playback."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING
from urllib.parse import quote
from zoneinfo import ZoneInfo

from homeassistant.components.http.auth import async_sign_path
from homeassistant.components.media_player import BrowseError, MediaClass
from homeassistant.components.media_source import (
    BrowseMediaSource,
    MediaSource,
    PlayMedia,
)
from homeassistant.components.media_source.error import Unresolvable
from homeassistant.components.websocket_api import current_connection
from homeassistant.config_entries import ConfigEntryState

from .const import DOMAIN
from .recordings import ClipUnavailableError
from .recordings_models import RecordingsError, opaque_id, require

_LOGGER = logging.getLogger(__name__)


# Shared field validators: report the caller's line (it names the field), then helper.
_VALIDATOR_HELPERS = frozenset(
    {
        "require",
        "number",
        "opaque_id",
        "token",
        "time_ms",
        "choice",
        "flag",
        "fields",
        "timestamp",
    }
)


def failure_site(err: BaseException) -> str:
    """Name the innermost integration function and line that failed; never values."""
    site = "unknown"
    helper = None
    tb = err.__traceback__
    while tb is not None:
        code = tb.tb_frame.f_code
        if "eufy_sdk" in code.co_filename and code.co_name != "require":
            if code.co_name in _VALIDATOR_HELPERS:
                helper = code.co_name
            else:
                site = f"{code.co_name}:{tb.tb_lineno}"
                helper = None
        tb = tb.tb_next
    return f"{site} via {helper}" if helper else site


if TYPE_CHECKING:
    from collections.abc import Iterator

    from homeassistant.components.media_source import MediaSourceItem
    from homeassistant.components.websocket_api.connection import MessageHandler
    from homeassistant.core import HomeAssistant

    from .recordings import RecordingRuntime

MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def folder_id(scope: str, camera: str) -> str:
    """Build a stable full opaque camera media identifier."""
    return (
        f"media-source://{DOMAIN}/s/{opaque_id(scope, 's')}/c/{opaque_id(camera, 'c')}"
    )


def navigation_path(scope: str, camera: str) -> str:
    """Encode HA's empty content type and complete media URI tuple."""
    return "/media-browser/browser/" + quote("," + folder_id(scope, camera), safe="")


def parse_identifier(identifier: str) -> tuple[str, str, str | None]:
    """Reject external URLs, cursors, names, queries and forged paths."""
    parts = identifier.split("/")
    require(len(parts) in (4, 5, 6) and parts[0] == "s" and parts[2] == "c")
    scope, camera = opaque_id(parts[1], "s"), opaque_id(parts[3], "c")
    event = None
    if len(parts) == 5:
        # The fixed category needs no identity beyond the opaque camera path.
        require(parts[4] == "u")
    if len(parts) == 6:
        if parts[4] == "d":
            require(date.fromisoformat(parts[5]).isoformat() == parts[5])
        else:
            require(parts[4] == "e")
            event = opaque_id(parts[5], "e")
    return scope, camera, event


def event_day(event: dict, time_zone: str) -> date | None:
    """Group valid timestamps by Home Assistant calendar date, including DST."""
    if event["timeStatus"] != "valid" or event["startedAtUtc"] is None:
        return None
    return (
        datetime.fromisoformat(event["startedAtUtc"])
        .astimezone(ZoneInfo(time_zone))
        .date()
    )


def day_title(day: date, today: date) -> str:
    """Use calendar days rather than elapsed hours for relative labels."""
    if day == today:
        return "Today"
    if day == today - timedelta(days=1):
        return "Yesterday"
    return f"{WEEKDAYS[day.weekday()]}, {MONTHS[day.month - 1]} {day.day}"


def runtimes(hass: HomeAssistant) -> list[RecordingRuntime]:
    """Discover loaded entries without creating connections."""
    result = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is not ConfigEntryState.LOADED:
            continue
        runtime = getattr(getattr(entry, "runtime_data", None), "recordings", None)
        if runtime is not None and runtime.available_scope() is not None:
            result.append(runtime)
    return result


def find_runtime(hass: HomeAssistant, scope: str) -> RecordingRuntime:
    """Refuse ambiguous scopes rather than crossing bridge boundaries."""
    matches = [
        runtime for runtime in runtimes(hass) if runtime.available_scope() == scope
    ]
    require(len(matches) == 1)
    return matches[0]


def title(
    event: dict, *, time_zone: str, detail: bool = False, playback: bool = False
) -> str:
    """Show Home Assistant local time first with fixed English display labels."""
    if event["timeStatus"] == "valid" and event["startedAtUtc"] is not None:
        local = datetime.fromisoformat(event["startedAtUtc"]).astimezone(
            ZoneInfo(time_zone)
        )
        period = "AM" if local.hour < 12 else "PM"
        start = f"{local.hour % 12 or 12}:{local.minute:02d} {period}"
        if detail:
            start += f" · {MONTHS[local.month - 1]} {local.day}"
    else:
        start = "Time unknown"
    duration = event["durationSeconds"]
    result = start
    if (
        type(duration) in (int, float)
        and math.isfinite(duration)
        and 0 <= duration <= 86400
    ):
        result += " · " + duration_text(duration)
    else:
        result += " · duration unknown"
    if detail and event["timeStatus"] != "valid":
        result += " · local time " + event["timeStatus"].replace("_", " ")
    if detail and event["startedAtUtc"] is not None:
        result += " · UTC " + event["startedAtUtc"]
    if detail and not playback:
        result += " · Browse only — video unavailable"
    if detail and event["stationPresence"] == "not_in_latest_window":
        result += " · May no longer be on HomeBase"
    return result


def duration_text(duration: float) -> str:
    """Return seconds under a minute, otherwise whole minutes plus seconds."""
    if duration < 60:
        return f"{duration:g} s"
    minutes, rest = divmod(round(duration), 60)
    return f"{minutes} min" + (f" {rest} s" if rest else "")


def directory(
    identifier: str,
    name: str,
    *,
    children: list | None = None,
    thumbnail: str | None = None,
) -> BrowseMediaSource:
    """Every level remains a non-playable information directory."""
    return BrowseMediaSource(
        domain=DOMAIN,
        identifier=identifier,
        media_class=MediaClass.DIRECTORY,
        media_content_type="",
        title=name,
        can_play=False,
        can_expand=True,
        children=children,
        thumbnail=thumbnail,
    )


def folder_status(page: dict, *, empty: bool) -> str:
    """Return a short camera-folder title suffix only when something is off."""
    state = page["refresh"]["state"]
    parts = []
    if state in ("deferred", "failed"):
        parts.append("showing saved list")
    if empty:
        parts.append("no playable recordings")
        if state == "scheduled":
            parts.append("checking for events")
    if page["truncated"]:
        parts.append("catalog limited to newest 100")
    if page["stationWindow"] != 20:
        parts.append(page["disclosure"])
    return "".join(" · " + part for part in parts)


async def async_get_media_source(hass: HomeAssistant) -> EufyRecordingMediaSource:
    """Return the provider; unsupported entries are hidden from its root."""
    return EufyRecordingMediaSource(hass)


@contextmanager
def viewer_wait() -> Iterator[None]:
    """Bind a pending frontend resolve to its connection and next navigation."""
    connection = current_connection.get()
    if connection is None:
        # Service callers retain normal task cancellation and the bounded wait.
        yield
        return
    task = asyncio.current_task()
    require(task is not None)
    subscription = object()
    original_handlers = connection.handlers
    handlers = original_handlers.copy()

    def restore_handlers() -> None:
        if connection.handlers is handlers:
            connection.handlers = original_handlers

    def cancel() -> None:
        # Never cancel the shared bridge job: another connection may need it.
        task.cancel()

    def navigation_handler(handler: MessageHandler) -> MessageHandler:
        def navigate(hass, active, message) -> None:  # noqa: ANN001 - HA handler signature
            cancel()
            restore_handlers()
            if task.done():
                handler(hass, active, message)
                return

            async def after_cleanup() -> None:
                # Let the cancelled wait release its admission slot first, so a
                # replacement resolve is not refused while the pool looks full.
                await asyncio.wait({task})
                handler(hass, active, message)

            hass.async_create_task(after_cleanup(), eager_start=False)

        return navigate

    # HA keeps async websocket responders alive after disconnect, and navigation
    # to another provider never calls this media source. Intercept only the next
    # media browse/resolve on this connection, preserving HA's command schemas.
    for command in ("media_source/browse_media", "media_source/resolve_media"):
        if command in handlers:
            handler, schema = handlers[command]
            handlers[command] = (navigation_handler(handler), schema)
    connection.handlers = handlers
    connection.subscriptions[subscription] = cancel
    try:
        yield
    finally:
        connection.subscriptions.pop(subscription, None)
        restore_handlers()


class EufyRecordingMediaSource(MediaSource):
    """Render metadata and resolve saved clips through authenticated HA URLs."""

    name = "Eufy"

    def __init__(self, hass: HomeAssistant) -> None:
        """Use already loaded integration entries."""
        super().__init__(DOMAIN)
        self.hass = hass

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        """Resolve qualified buffered HLS or the existing completed MP4 path."""
        try:
            scope, camera, event_id = parse_identifier(item.identifier)
            require(event_id is not None)
            runtime = find_runtime(self.hass, scope)
            if not runtime.playback_supported():
                reason = "browse_only"
                raise playback_error(reason)
            with viewer_wait():
                playback = (
                    await runtime.open_playback(camera, event_id)
                    if runtime.progressive_supported()
                    else None
                )
                if playback is not None and playback["format"] == "hls":
                    session = playback["session"]
                    reply = session["reply"]
                    path = (
                        f"/api/{DOMAIN}/recordings/{scope}/{camera}/{event_id}/"
                        f"playback/{reply['sessionId']}/{reply['generation']}/index.m3u8"
                    )
                    lifetime = max(1, reply["hardExpiresAtMs"] / 1000 - time.time())
                    # The stock play dialog selects its HLS player only for this
                    # exact type; the playlist response keeps the registered type.
                    return PlayMedia(
                        async_sign_path(self.hass, path, timedelta(seconds=lifetime)),
                        "application/x-mpegURL",
                    )
                ready = (
                    playback["clip"]
                    if playback is not None
                    else await runtime.prepare_clip(camera, event_id)
                )
            # Permit seeking/reloading for at least 15 minutes, or clip length
            # plus five minutes for longer recordings. Bridge expiry still wins.
            lifetime = max(900, (ready["durationMs"] or 0) / 1000 + 300)
            path = f"/api/{DOMAIN}/recordings/{scope}/{camera}/{event_id}/clip.mp4"
            return PlayMedia(
                async_sign_path(self.hass, path, timedelta(seconds=lifetime)),
                "video/mp4",
            )
        except ClipUnavailableError as err:
            _LOGGER.warning(
                "Recording playback failed: %s at %s",
                err.reason
                if err.reason
                in (
                    "none",
                    "disabled",
                    "browse_only",
                    "live_view",
                    "not_found",
                    "busy",
                    "owner_unavailable",
                    "too_long",
                    "timeout",
                    "failed",
                    "storage",
                    "convert_failed",
                    "station_error",
                    "cleanup_uncertain",
                )
                else "failed",
                failure_site(err),
            )
            raise playback_error(err.reason) from None
        except TimeoutError as err:
            _LOGGER.warning(
                "Recording playback failed: TimeoutError at %s",
                failure_site(err),
            )
            reason = "timeout"
            raise playback_error(reason) from None
        except (RecordingsError, KeyError, ValueError) as err:
            _LOGGER.warning(
                "Recording playback failed: %s at %s",
                type(err).__name__,
                failure_site(err),
            )
            reason = "failed"
            raise playback_error(reason) from None

    async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
        """Derive day folders and clips from one camera catalog page."""
        try:
            if not item.identifier:
                children = []
                for runtime in runtimes(self.hass):
                    for camera in await runtime.list_cameras():
                        identifier = f"s/{runtime.scope}/c/{camera['cameraId']}"
                        children.append(directory(identifier, camera["name"].strip()))
                return directory(
                    "",
                    "Eufy" if children else "Recordings unavailable",
                    children=children,
                )
            scope, camera, event_id = parse_identifier(item.identifier)
            unknown_time = item.identifier.endswith("/u")
            parts = item.identifier.split("/")
            day = date.fromisoformat(parts[5]) if parts[4:5] == ["d"] else None
            runtime = find_runtime(self.hass, scope)
            page = await runtime.browse(camera, event_id)
            playback = runtime.playback_supported()
            # The v1 catalog has no playable-only filter or page cursor.
            # Keep its truncation metadata and focused-event behavior intact.
            events = [
                event
                for event in page["events"]
                if event.get("recordingAvailable", True)
                and (event_id is not None or (playback and event["playable"]))
            ]
            require(event_id is None or bool(events))
            time_zone = self.hass.config.time_zone
            today = datetime.now(ZoneInfo(time_zone)).date()
            if event_id is None:
                groups = {}
                for event in events:
                    groups.setdefault(event_day(event, time_zone), []).append(event)
                if day is None and not unknown_time:
                    base = f"s/{scope}/c/{camera}"
                    children = [
                        directory(f"{base}/d/{key.isoformat()}", day_title(key, today))
                        for key in sorted(
                            (key for key in groups if key is not None), reverse=True
                        )
                    ]
                    if None in groups:
                        children.append(directory(f"{base}/u", "Time unknown"))
                    return directory(
                        item.identifier,
                        runtime.cameras[camera]["name"].strip()
                        + folder_status(page, empty=not children),
                        children=children,
                    )
                events = groups.get(day, [])
                require(bool(events))
            children = []
            for event in events:
                identifier = f"s/{scope}/c/{camera}/e/{event['eventId']}"
                thumbnail = (
                    f"/api/{DOMAIN}/recordings/{scope}/{camera}/"
                    f"{event['eventId']}/cover"
                )
                if event["coverRevision"] is not None:
                    thumbnail += "?v=" + event["coverRevision"]
                playable = playback and event["playable"]
                row = directory(
                    identifier,
                    title(
                        event,
                        time_zone=time_zone,
                        detail=event_id is not None,
                        playback=playable,
                    ),
                    children=[] if event_id else None,
                    thumbnail=thumbnail,
                )
                if playable:
                    row.can_play = True
                    row.can_expand = False
                    row.media_class = MediaClass.VIDEO
                    row.media_content_type = "video/mp4"
                if event_id:
                    return row
                children.append(row)
            return directory(
                item.identifier,
                "Time unknown" if unknown_time else day_title(day, today),
                children=children,
            )
        except (RecordingsError, KeyError, ValueError) as err:
            _LOGGER.warning(
                "Recordings browse failed: %s at %s",
                type(err).__name__,
                failure_site(err),
            )
            message = "Recordings unavailable"
            raise BrowseError(
                message,
                translation_domain=DOMAIN,
                translation_key="recordings_unavailable",
            ) from None


def playback_error(reason: str) -> Unresolvable:
    """Map fixed protocol reasons to short, translatable messages."""
    if reason == "disabled":
        reason = "browse_only"
    messages = {
        "browse_only": "Browse only — video unavailable",
        "live_view": "Live view is on — try again after it ends",
        "not_found": "Recording is no longer on the HomeBase",
        "busy": "HomeBase is busy — try again shortly",
        "owner_unavailable": "HomeBase is unavailable — try again shortly",
        "too_long": "Recording is too long to prepare",
        "timeout": "Video is still preparing — try again shortly",
        "failed": "Couldn't prepare the video",
    }
    key = reason if reason in messages else "failed"
    return Unresolvable(
        messages[key],
        translation_domain=DOMAIN,
        translation_key="recordings_"
        + ("browse_only" if key == "browse_only" else "clip_" + key),
    )
