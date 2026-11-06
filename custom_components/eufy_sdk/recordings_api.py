# ruff: noqa: PLR2004 - HTTP success status and protocol bounds
"""Recordings RPC and fixed-origin cover and clip transport."""

from __future__ import annotations

import asyncio
import hashlib
import re
from contextlib import asynccontextmanager
from io import BytesIO
from typing import TYPE_CHECKING

import aiohttp
from PIL import Image, UnidentifiedImageError
from yarl import URL

from . import recordings_models as models

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from typing import Any


class RecordingsApi:
    """Add recording wrappers to the existing bridge connection."""

    recordings_capability: dict | None = None

    async def _recordings_rpc(self, command: str, **args: Any) -> dict:
        """Never retry, reconnect, or propagate bridge exception content."""
        models.require(self.recordings_capability is not None)
        capability = self.recordings_capability
        try:
            async with asyncio.timeout(3):
                reply = await self.rpc(command, timeout=3, **args)
            models.require(self.recordings_capability == capability)
            models.require(isinstance(reply, dict) and reply.get("ok") is True)
            return reply.get("recordings")
        except Exception:  # noqa: BLE001 - suppress untrusted bridge errors
            raise models.RecordingsError from None

    async def recordings_diagnostics(self) -> dict:
        """Read sanitized cached counters without admitting any station work."""
        return models.diagnostics(await self._recordings_rpc("recordings.diagnostics"))

    async def recordings_cameras(self) -> dict:
        """Read the enrolled camera list without refreshing inventory."""
        payload = await self._recordings_rpc("recordings.cameras")
        return models.cameras(payload, self.recordings_capability["scopeId"])

    async def recordings_catalog(
        self,
        camera_id: str,
        *,
        refresh: str = "cache_only",
        focus_event_id: str | None = None,
    ) -> dict:
        """Request one bounded catalog; no cursor or station pagination."""
        models.opaque_id(camera_id, "c")
        models.choice(refresh, ("cache_only", "if_due"))
        args = {"cameraId": camera_id, "refresh": refresh}
        if focus_event_id is not None:
            args["focusEventId"] = models.opaque_id(focus_event_id, "e")
        payload = await self._recordings_rpc("recordings.catalog", **args)
        return models.catalog(payload, self.recordings_capability["scopeId"], camera_id)

    async def recordings_cover(
        self,
        camera_id: str,
        event_id: str,
        *,
        browse_id: str | None = None,
        request_if_admitted: bool = False,
    ) -> dict:
        """Admit cover work only under an existing in-memory interest."""
        models.opaque_id(camera_id, "c")
        models.opaque_id(event_id, "e")
        if browse_id is not None:
            models.token(browse_id)
        payload = await self._recordings_rpc(
            "recordings.cover",
            cameraId=camera_id,
            eventId=event_id,
            browseId=browse_id,
            mode="request_if_admitted" if request_if_admitted else "cache_only",
        )
        return models.cover(payload, camera_id, event_id)

    async def _recordings_clip(
        self, command: str, camera_id: str, event_id: str
    ) -> dict:
        """Keep playback opt-in and use only validated opaque identities."""
        models.require((self.recordings_capability or {}).get("playback") is True)
        payload = await self._recordings_rpc(
            command,
            cameraId=models.opaque_id(camera_id, "c"),
            eventId=models.opaque_id(event_id, "e"),
        )
        return models.clip(payload)

    async def recordings_clip_prepare(self, camera_id: str, event_id: str) -> dict:
        """Idempotently request a saved clip under the bridge's live-view policy."""
        return await self._recordings_clip(
            "recordings.clip_prepare", camera_id, event_id
        )

    async def recordings_clip_status(self, camera_id: str, event_id: str) -> dict:
        """Read clip progress without starting a replacement job."""
        return await self._recordings_clip(
            "recordings.clip_status", camera_id, event_id
        )

    async def recordings_clip_cancel(self, camera_id: str, event_id: str) -> dict:
        """Expose explicit cancellation, never use it when a viewer disconnects."""
        return await self._recordings_clip(
            "recordings.clip_cancel", camera_id, event_id
        )

    @asynccontextmanager
    async def open_recordings_clip(
        self, camera_id: str, event_id: str, byte_range: str | None = None
    ) -> AsyncIterator[aiohttp.ClientResponse]:
        """Open a fixed-origin stream under the same session/auth policy as covers."""
        models.require((self.recordings_capability or {}).get("playback") is True)
        models.opaque_id(camera_id, "c")
        models.opaque_id(event_id, "e")
        if byte_range is not None:
            models.require(
                len(byte_range) <= 128
                and re.fullmatch(r"bytes=(?:[0-9]+-[0-9]*|-[0-9]+)", byte_range)
                is not None
            )
        origin = URL(self._url).with_scheme("http").origin()
        url = origin / "recordings" / "v1" / "clips" / camera_id / (event_id + ".mp4")
        try:
            async with self._session.get(
                url,
                headers={"Range": byte_range} if byte_range is not None else {},
                allow_redirects=False,
                auto_decompress=False,
                timeout=aiohttp.ClientTimeout(
                    total=None, sock_connect=10, sock_read=30
                ),
            ) as response:
                yield response
        except (aiohttp.ClientError, OSError, TimeoutError):
            raise models.RecordingsError from None

    async def fetch_recordings_cover(
        self, camera_id: str, event_id: str, ready: dict
    ) -> tuple[bytes, str | None]:
        """Fetch only ready bytes, never response URLs or transfer grants."""
        ready = models.cover(
            ready, models.opaque_id(camera_id, "c"), models.opaque_id(event_id, "e")
        )
        models.require(ready["state"] == "ready")
        origin = URL(self._url).with_scheme("http").origin()
        url = origin / "recordings" / "v1" / "covers" / camera_id / event_id
        try:
            async with self._session.get(
                url, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                models.require(response.status == 200)
                headers = response.headers
                models.require(
                    headers.get("Content-Type", "").split(";")[0] == "image/jpeg"
                )
                models.require(headers.get("Content-Length") == str(ready["bytes"]))
                models.require(headers.get("X-Eufy-Image-Sha256") == ready["sha256"])
                data = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    data.extend(chunk)
                    models.require(
                        len(data) <= models.COVER_CAP and len(data) <= ready["bytes"]
                    )
                models.require(
                    len(data) == ready["bytes"]
                    and data[:2] == b"\xff\xd8"
                    and data[-2:] == b"\xff\xd9"
                )
                models.require(hashlib.sha256(data).hexdigest() == ready["sha256"])
                with Image.open(BytesIO(data)) as image:
                    models.require(image.format == "JPEG")
                    models.require(image.width * image.height <= 16777216)
                    image.verify()
                event_time = models.timestamp(headers.get("X-Eufy-Event-Time"))
                models.require(event_time is None or event_time.endswith("Z"))
                return bytes(data), event_time
        except (
            aiohttp.ClientError,
            OSError,
            TimeoutError,
            ValueError,
            UnidentifiedImageError,
            Image.DecompressionBombError,
            SyntaxError,
        ):
            raise models.RecordingsError from None
