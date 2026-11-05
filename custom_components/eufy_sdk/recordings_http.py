"""Authenticated, bounded Home Assistant recording cover proxy."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.helpers.http import KEY_HASS

from .media_source import find_runtime
from .recordings_models import RecordingsError, opaque_id

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

HEADERS = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}


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
        headers = {**HEADERS, "X-Eufy-Image-Sha256": hashlib.sha256(data).hexdigest()}
        if event_time is not None:
            headers["X-Eufy-Event-Time"] = event_time
        return web.Response(body=data, content_type="image/jpeg", headers=headers)


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
        hass.data[key] = True
