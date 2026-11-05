# ruff: noqa: PLR2004 - fixed media path grammar
"""Browse-only Home Assistant media source for bridge recording metadata."""

from __future__ import annotations

import math
from datetime import datetime
from typing import TYPE_CHECKING
from urllib.parse import quote
from zoneinfo import ZoneInfo

from homeassistant.components.media_player import BrowseError, MediaClass
from homeassistant.components.media_source import BrowseMediaSource, MediaSource
from homeassistant.components.media_source.error import Unresolvable
from homeassistant.config_entries import ConfigEntryState

from .const import DOMAIN
from .recordings_models import TIMEZONE, RecordingsError, opaque_id, require

if TYPE_CHECKING:
    from homeassistant.components.media_source import MediaSourceItem, PlayMedia
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


def title(event: dict, *, detail: bool = False) -> str:
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
    if detail:
        result += " · Browse only — video unavailable"
    if detail and event["stationPresence"] == "not_in_latest_window":
        result += " · May no longer be on HomeBase"
    return result


def duration_text(duration: float) -> str:
    """Return seconds under a minute, otherwise whole minutes plus seconds."""
    seconds = round(duration)
    if seconds < 60:
        return f"{duration:g} s" if duration < 60 else "1 min"
    minutes, rest = divmod(seconds, 60)
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


class EufyRecordingMediaSource(MediaSource):
    """Render metadata only, with authenticated relative thumbnail URLs."""

    name = "Eufy"

    def __init__(self, hass: HomeAssistant) -> None:
        """Use already loaded integration entries."""
        super().__init__(DOMAIN)
        self.hass = hass

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:  # noqa: ARG002 - HA provider signature
        """Reject even forged direct resolve calls without transport activity."""
        message = "Browse only — video unavailable"
        raise Unresolvable(
            message, translation_domain=DOMAIN, translation_key="recordings_browse_only"
        )

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
            unknown_events = [
                event for event in page["events"] if event["timeStatus"] != "valid"
            ]
            if unknown_time:
                require(bool(unknown_events))
            children = []
            for event in page["events"]:
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
                row = directory(
                    identifier,
                    title(event, detail=event_id is not None),
                    children=[] if event_id else None,
                    thumbnail=thumbnail,
                )
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
        except (RecordingsError, KeyError, ValueError):
            message = "Recordings unavailable"
            raise BrowseError(
                message,
                translation_domain=DOMAIN,
                translation_key="recordings_unavailable",
            ) from None
