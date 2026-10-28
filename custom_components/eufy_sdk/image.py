"""Image platform — the latest detected-event thumbnail, one per camera."""

from __future__ import annotations

import asyncio
import hashlib
from http import HTTPStatus
from typing import TYPE_CHECKING

import aiohttp
from homeassistant.components.image import ImageEntity
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .const import CONF_HOST, CONF_PORT, EVENT_TYPE
from .entity import EufySdkDeviceEntity
from .event_image import event_time_for_image
from .pushmap import EVENT_IMAGE_REFRESH, THUMBNAIL_EVENTS

if TYPE_CHECKING:
    from datetime import datetime

    from homeassistant.core import Event, HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import EufySdkDataUpdateCoordinator
    from .data import EufySdkConfigEntry

# Bridge device events are re-fired on the HA bus under this type (see __init__.py).


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EufySdkConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create a latest-event image for every device the bridge exposes as a camera."""
    coordinator = entry.runtime_data.coordinator
    host = entry.data[CONF_HOST]
    port = entry.data[CONF_PORT]
    async_add_entities(
        EufySdkEventImage(hass, coordinator, sn, host, port)
        for sn, dev in coordinator.data.items()
        if dev.get("stream")
    )


class EufySdkEventImage(EufySdkDeviceEntity, ImageEntity):
    """
    Latest-detection-event thumbnail for one device.

    The SDK downloads and retains each event's thumbnail; the bridge serves the
    retained bytes at `/event-image/<sn>`. This entity fetches from the bridge (never
    the raw cloud URL — that needs the SDK's auth/decode). It refreshes on a
    detection push for the device (events that carry a new thumbnail — not
    telemetry like ptzNotify) and once per completed coordinator snapshot. Health
    and unrelated state updates do not fetch or republish the image. Only changed
    bytes or paired event metadata advance `image_last_updated`; an initial fetch
    also shows an already-retained thumbnail before the first event.
    """

    _attr_translation_key = "last_event"

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: EufySdkDataUpdateCoordinator,
        sn: str,
        host: str,
        port: int,
    ) -> None:
        """Bind to a device serial + the bridge address."""
        EufySdkDeviceEntity.__init__(self, coordinator, sn)
        ImageEntity.__init__(self, hass)
        self._attr_unique_id = f"{sn}_last_event"
        self._url = f"http://{host}:{port}/event-image/{sn}"
        self._image: bytes | None = None
        self._hash: str | None = None
        self._event_time: datetime | None = None
        self._image_snapshot_generation = coordinator.completed_snapshot_generation
        self._image_available = self.available
        # A detection push and the eventImageUpdated nudge both trigger a refresh.
        # Without this the two fetches race: an earlier fetch of the OLD bytes can
        # finish last and overwrite the new image while stamping a fresh time (time
        # moves, picture stale). Serialise so the last write is always the newest fetch.
        self._refresh_lock = asyncio.Lock()

    async def async_added_to_hass(self) -> None:
        """Subscribe to bridge events and pull any already-retained thumbnail."""
        await super().async_added_to_hass()
        self.async_on_remove(self.hass.bus.async_listen(EVENT_TYPE, self._handle_event))
        await self._refresh()

    @callback
    def _handle_event(self, event: Event) -> None:
        """
        Re-pull the thumbnail on a detection or the bridge's image-refresh nudge.

        Fires on a detection event for this device, or on the bridge's
        `eventImageUpdated` nudge once it has a fresh local cover. Telemetry/state
        events the bridge also forwards (ptzNotify, batteryLevel, armingModeChanged,
        …) carry no new thumbnail, so they must not stamp a new "Last event" or spam
        the bridge with refetches. The coordinator poll (`_handle_coordinator_update`)
        still catches any thumbnail no event announced.
        """
        data = event.data
        if data.get("deviceSn") != self._sn:
            return
        ev = data.get("event")
        if ev not in THUMBNAIL_EVENTS and ev != EVENT_IMAGE_REFRESH:
            return
        self.hass.async_create_task(self._refresh())

    @callback
    def _handle_coordinator_update(self) -> None:
        """Check missed images once per snapshot, preserving availability changes."""
        available = self.available
        availability_changed = available != self._image_available
        self._image_available = available
        generation = self.coordinator.completed_snapshot_generation
        if available and (
            generation != self._image_snapshot_generation or availability_changed
        ):
            self._image_snapshot_generation = generation
            self.hass.async_create_task(self._refresh())
        # CoordinatorEntity writes on every notification. Only availability needs
        # a write here; _refresh publishes changed image bytes or paired metadata.
        if availability_changed:
            self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        """Expose the image's event time separately from HA's image refresh clock."""
        return {
            **(super().extra_state_attributes or {}),
            "event_time": self._event_time.isoformat() if self._event_time else None,
        }

    async def _refresh(self) -> None:
        """
        Fetch the retained thumbnail; stamp a new time only when the bytes change.

        Serialised: concurrent refreshes (detection + nudge) must not interleave, or an
        old-bytes fetch that finishes last would clobber the new one.
        """
        async with self._refresh_lock:
            fetched = await self._fetch()
            if fetched is None:
                return
            data, event_time = fetched
            digest = hashlib.sha1(data, usedforsecurity=False).hexdigest()
            if (
                self._event_time is not None
                and event_time is not None
                and event_time < self._event_time
            ):
                return
            if digest == self._hash and event_time == self._event_time:
                return
            self._image = data
            self._hash = digest
            self._event_time = event_time
            self._attr_image_last_updated = dt_util.utcnow()
            self.async_write_ha_state()

    async def _fetch(self) -> tuple[bytes, datetime | None] | None:
        """Fetch the bytes and paired metadata in one response, without cloud access."""
        session = async_get_clientsession(self.hass)
        try:
            resp = await session.get(self._url, timeout=aiohttp.ClientTimeout(total=15))
            if resp.status != HTTPStatus.OK:
                return None
            data = await resp.read()
            return data, event_time_for_image(
                data,
                resp.headers.get("x-eufy-event-time"),
                resp.headers.get("x-eufy-image-sha256"),
            )
        except (aiohttp.ClientError, TimeoutError):
            return None

    async def async_image(self) -> bytes | None:
        """Return the last thumbnail we fetched (HA's image proxy calls this)."""
        return self._image
