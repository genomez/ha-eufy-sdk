# ruff: noqa: PLR2004 - fixed media path grammar
"""Home Assistant recording browser with opt-in saved clip playback."""

from __future__ import annotations

import asyncio
import logging
import math
from contextlib import contextmanager
from datetime import datetime, timedelta
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
from .recordings_models import TIMEZONE, RecordingsError, opaque_id, require

_LOGGER = logging.getLogger(__name__)


def failure_site(err: BaseException) -> str:
    """Name the innermost integration function and line that failed; never values."""
    site = "unknown"
    tb = err.__traceback__
    while tb is not None:
        code = tb.tb_frame.f_code
        if "eufy_sdk" in code.co_filename and code.co_name != "require":
            site = f"{code.co_name}:{tb.tb_lineno}"
        tb = tb.tb_next
    return site


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
        require(parts[4] == "e")
        event = opaque_id(parts[5], "e")
    return scope, camera, event


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


def title(event: dict, *, detail: bool = False, playback: bool = False) -> str:
    """Show station-local time first with fixed English display labels."""
    if event["timeStatus"] == "valid" and event["startedAtLocal"] is not None:
        local = datetime.fromisoformat(event["startedAtLocal"]).astimezone(
            ZoneInfo(TIMEZONE)
        )
        period = "AM" if local.hour < 12 else "PM"
        start = (
            f"{local.hour % 12 or 12}:{local.minute:02d} {period}"
            f" · {MONTHS[local.month - 1]} {local.day}"
        )
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
    if detail and event["timeStatus"] != "valid":
        result += " · Chicago time " + event["timeStatus"].replace("_", " ")
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
        parts.append("checking for events" if state == "scheduled" else "no events yet")
    if page["truncated"]:
        parts.append("newest 100")
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
        """Wait for a complete clip before handing the frontend a signed path."""
        try:
            scope, camera, event_id = parse_identifier(item.identifier)
            require(event_id is not None)
            runtime = find_runtime(self.hass, scope)
            if not runtime.playback_supported():
                reason = "browse_only"
                raise playback_error(reason)
            with viewer_wait():
                ready = await runtime.prepare_clip(camera, event_id)
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
        """Browse one camera or one information folder without prefetching."""
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
            runtime = find_runtime(self.hass, scope)
            page = await runtime.browse(camera, event_id)
            playback = runtime.playback_supported()
            events = [
                event
                for event in page["events"]
                if event.get("recordingAvailable", True)
            ]
            if event_id is not None:
                require(bool(events))
            unknown_events = [
                event for event in events if event["timeStatus"] != "valid"
            ]
            if unknown_time:
                require(bool(unknown_events))
            children = []
            for event in events:
                if (
                    event_id is None
                    and (event["timeStatus"] != "valid") != unknown_time
                ):
                    continue
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
                    title(event, detail=event_id is not None, playback=playable),
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
            unknown_title = f"Time unknown ({len(unknown_events)})"
            if unknown_events and not unknown_time:
                children.append(directory(f"s/{scope}/c/{camera}/u", unknown_title))
            return directory(
                item.identifier,
                unknown_title
                if unknown_time
                else runtime.cameras[camera]["name"].strip()
                + folder_status(page, empty=not children),
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
