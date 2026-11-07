"""Authenticated Home Assistant recording cover and streaming clip proxies."""

from __future__ import annotations

import asyncio
import hashlib
import re
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.helpers.http import KEY_HASS

from .media_source import find_runtime
from .recordings_models import RecordingsError, opaque_id, require

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

HEADERS = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
RANGE_HEADER_LIMIT = 128


class EufyRecordingCoverView(HomeAssistantView):
    """HA middleware authenticates every request, including RAM hits."""

    url = "/api/eufy_sdk/recordings/{scope_id}/{camera_id}/{event_id}/cover"
    name = "api:eufy_sdk:recordings:cover"
    requires_auth = True

    def __init__(self, placeholder: bytes) -> None:
        """Load the neutral bundled PNG once at registration."""
        self.placeholder = placeholder

    async def get(
        self, request: web.Request, scope_id: str, camera_id: str, event_id: str
    ) -> web.Response:
        """Accept only opaque route identities, never URLs or selectors."""
        try:
            opaque_id(scope_id, "s")
            opaque_id(camera_id, "c")
            opaque_id(event_id, "e")
            runtime = find_runtime(request.app[KEY_HASS], scope_id)
            result = await runtime.cover(camera_id, event_id)
        except RecordingsError:
            raise web.HTTPNotFound from None
        if result is None:
            return web.Response(
                body=self.placeholder,
                content_type="image/png",
                headers={**HEADERS, "Retry-After": "60"},
            )
        data, event_time = result
        digest = hashlib.sha256(data).hexdigest()
        etag = f'"{digest}"'
        headers = {
            **HEADERS,
            "Cache-Control": "private, max-age=86400",
            "ETag": etag,
            "X-Eufy-Image-Sha256": digest,
        }
        if event_time is not None:
            headers["X-Eufy-Event-Time"] = event_time
        validators = request.headers.get("If-None-Match", "").split(",")
        if any(value.strip().removeprefix("W/") in (etag, "*") for value in validators):
            return web.Response(status=HTTPStatus.NOT_MODIFIED, headers=headers)
        return web.Response(body=data, content_type="image/jpeg", headers=headers)


def clip_headers(status: int, headers: dict) -> dict[str, str]:
    """Forward only validated media framing, never bridge URLs or auth headers."""
    require(headers.get("Content-Encoding", "identity") == "identity")
    result = {**HEADERS, "Accept-Ranges": "bytes"}
    if status == HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE:
        content_range = headers.get("Content-Range", "")
        require(re.fullmatch(r"bytes \*/[0-9]{1,16}", content_range) is not None)
        return {**result, "Content-Range": content_range}
    require(status in (HTTPStatus.OK, HTTPStatus.PARTIAL_CONTENT))
    require(headers.get("Content-Type", "").split(";")[0] == "video/mp4")
    length = headers.get("Content-Length", "")
    require(re.fullmatch(r"[0-9]{1,16}", length) is not None)
    require(headers.get("Accept-Ranges", "bytes") == "bytes")
    result.update({"Content-Type": "video/mp4", "Content-Length": length})
    if status == HTTPStatus.PARTIAL_CONTENT:
        content_range = headers.get("Content-Range", "")
        match = re.fullmatch(
            r"bytes ([0-9]{1,16})-([0-9]{1,16})/([0-9]{1,16})", content_range
        )
        require(match is not None)
        start, end, total = (int(part) for part in match.groups())
        require(start <= end < total and end - start + 1 == int(length))
        result["Content-Range"] = content_range
    return result


class EufyRecordingClipView(HomeAssistantView):
    """HA auth middleware accepts bearer headers or path-bound signed requests."""

    url = "/api/eufy_sdk/recordings/{scope_id}/{camera_id}/{event_id}/clip.mp4"
    name = "api:eufy_sdk:recordings:clip"
    requires_auth = True

    async def get(
        self, request: web.Request, scope_id: str, camera_id: str, event_id: str
    ) -> web.StreamResponse:
        """Proxy in 64 KiB chunks with backpressure, without caching a file."""
        response = None
        try:
            try:
                opaque_id(scope_id, "s")
                opaque_id(camera_id, "c")
                opaque_id(event_id, "e")
                runtime = find_runtime(request.app[KEY_HASS], scope_id)
                runtime.validate_clip(camera_id, event_id)
            except RecordingsError:
                raise web.HTTPNotFound(headers=HEADERS) from None
            generation = runtime.generation
            byte_range = request.headers.get("Range")
            if byte_range is not None and (
                len(byte_range) > RANGE_HEADER_LIMIT
                or re.fullmatch(r"bytes=(?:[0-9]+-[0-9]*|-[0-9]+)", byte_range) is None
            ):
                raise web.HTTPRequestRangeNotSatisfiable(headers=HEADERS)
            async with runtime.client.open_recordings_clip(
                camera_id, event_id, byte_range
            ) as upstream:
                require(
                    generation == runtime.generation
                    and runtime.available_scope() == scope_id
                )
                runtime.validate_clip(camera_id, event_id)
                if upstream.status == HTTPStatus.NOT_FOUND:
                    raise web.HTTPNotFound(headers=HEADERS)
                headers = clip_headers(upstream.status, upstream.headers)
                response = web.StreamResponse(status=upstream.status, headers=headers)
                await response.prepare(request)
                if upstream.status != HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE:
                    sent = 0
                    length = int(headers["Content-Length"])
                    async for chunk in upstream.content.iter_chunked(65536):
                        require(
                            generation == runtime.generation
                            and runtime.available_scope() == scope_id
                        )
                        runtime.validate_clip(camera_id, event_id)
                        sent += len(chunk)
                        require(sent <= length)
                        await response.write(chunk)
                    require(sent == length)
                await response.write_eof()
                return response
        except RecordingsError:
            if response is not None and response.prepared:
                # Do not finish a truncated payload as if it were a complete MP4.
                response.force_close()
                return response
            raise web.HTTPBadGateway(
                text="Video unavailable", headers=HEADERS
            ) from None


async def async_register_view(hass: HomeAssistant) -> None:
    """Register one shared route per HA process."""
    key = "eufy_sdk_recordings_view"
    lock = hass.data.setdefault(key + "_lock", asyncio.Lock())
    async with lock:
        if key in hass.data:
            return
        placeholder = await hass.async_add_executor_job(
            Path(__file__).with_name("recordings_placeholder.png").read_bytes
        )
        hass.http.register_view(EufyRecordingCoverView(placeholder))
        hass.http.register_view(EufyRecordingClipView())
        hass.data[key] = True
