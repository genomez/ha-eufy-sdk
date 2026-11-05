# ruff: noqa: PLR2004 - fixed resource bounds from the v1 contract
"""Entry-owned browse interests and bounded ephemeral cover delivery."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import TYPE_CHECKING

from .recordings_models import RecordingsError, opaque_id, require, token

if TYPE_CHECKING:
    from .api import EufySdkApiClient

WAIT_SECONDS = 10
CACHE_SECONDS = 60
CACHE_BYTES = 16 * 1024 * 1024


class RecordingRuntime:
    """Never poll, prefetch, publish entities, or persist recording metadata."""

    def __init__(self, client: EufySdkApiClient) -> None:
        """Bind one entry to the existing WS lifetime."""
        self.client = client
        self.closed = False
        self.scope: str | None = None
        self.cameras: dict[str, dict] = {}
        self.pages: dict[str, dict] = {}
        self.events: dict[str, OrderedDict[str, None]] = {}
        self.interests: dict[str, tuple[str, float, frozenset[str]]] = {}
        self.cache: OrderedDict[tuple[str, str], tuple[float, bytes, str | None]] = (
            OrderedDict()
        )
        self.jobs: dict[tuple[str, str], asyncio.Task] = {}
        self.wakeups: dict[tuple[str, str], asyncio.Event] = {}
        self.admitted: set[tuple[str, str]] = set()
        self.waiters = 0
        self.generation = 0
        self._remove_listener = client.add_disconnect_listener(self.reset)

    def reset(self) -> None:
        """Invalidate every old completion and release all waiters."""
        self.generation += 1
        for job in self.jobs.values():
            job.cancel()
        self.jobs.clear()
        self.wakeups.clear()
        self.interests.clear()
        self.cache.clear()
        self.pages.clear()
        self.events.clear()
        self.cameras.clear()
        self.admitted.clear()
        self.scope = None

    def close(self) -> None:
        """Retire this entry before transport shutdown."""
        if self.closed:
            return
        self.closed = True
        self.reset()
        self._remove_listener()

    def available_scope(self) -> str | None:
        """Hide unsupported/disabled bridges and clear replaced scopes."""
        capability = self.client.recordings_capability
        scope = capability.get("scopeId") if isinstance(capability, dict) else None
        if self.closed:
            return None
        if self.scope != scope:
            self.reset()
            self.scope = scope
        return scope

    async def list_cameras(self) -> list[dict]:
        """Read cached enrollment; no recording or inventory refresh."""
        scope = self.available_scope()
        require(scope is not None)
        generation = self.generation
        result = await self.client.recordings_cameras()
        require(scope == self.available_scope() and generation == self.generation)
        if self.cameras and self.cameras.keys() != {
            row["cameraId"] for row in result["cameras"]
        }:
            self.reset()
            self.scope = scope
        self.cameras = {row["cameraId"]: row for row in result["cameras"]}
        return list(self.cameras.values())

    async def browse(self, camera: str, event_id: str | None = None) -> dict:
        """Make exactly one metadata RPC for an authenticated folder browse."""
        require(self.available_scope() is not None)
        if camera not in self.cameras:
            await self.list_cameras()
        require(camera in self.cameras)
        generation = self.generation
        scope = self.scope
        page = await self.client.recordings_catalog(
            camera, refresh="if_due", focus_event_id=event_id
        )
        require(scope == self.available_scope() and generation == self.generation)
        if event_id is None:
            self.pages[camera] = page
            self.events[camera] = OrderedDict(
                (row["eventId"], None) for row in page["events"]
            )
        else:
            require(
                len(page["events"]) == 1 and page["events"][0]["eventId"] == event_id
            )
            known = self.events.setdefault(camera, OrderedDict())
            known[event_id] = None
            known.move_to_end(event_id)
            while len(known) > 100:
                known.popitem(last=False)
        browse_id, expires = page["browseId"], page["browseExpiresAtMs"]
        if browse_id is not None and expires is not None:
            previous = self.interests.get(camera)
            if previous is None or previous[0] != browse_id:
                self.admitted = {key for key in self.admitted if key[0] != camera}
            self.interests[camera] = (
                browse_id,
                time.monotonic() + min(60, max(0, expires / 1000 - time.time())),
                frozenset(page["coverEligibleEventIds"]),
            )
        return page

    def changed(self, value: dict) -> None:
        """Wake existing requests only; never fetch on push notifications."""
        if self.available_scope() is None or value.get("scopeId") != self.scope:
            return
        try:
            require(type(value.get("version")) is int and value["version"] == 1)
            token(value.get("revision"))
            camera = value.get("cameraId")
            if camera is not None:
                opaque_id(camera, "c")
            require(value.get("reason") in ("catalog", "cover", "eviction", "disabled"))
        except RecordingsError:
            return
        if value["reason"] == "disabled":
            self.client.recordings_capability = None
            self.reset()
            return
        for key, wakeup in self.wakeups.items():
            if camera is None or key[0] == camera:
                wakeup.set()

    async def cover(
        self, camera: str, event_id: str
    ) -> tuple[bytes, str | None] | None:
        """Deduplicate authenticated proxy calls with fixed memory/work bounds."""
        require(self.available_scope() is not None and camera in self.cameras)
        require(event_id in self.events.get(camera, {}))
        require(self.waiters < 8)
        key = (camera, event_id)
        cached = self.cache.get(key)
        if cached is not None and time.monotonic() - cached[0] < CACHE_SECONDS:
            self.cache.move_to_end(key)
            return cached[1:]
        self.cache.pop(key, None)
        if key not in self.jobs:
            require(len(self.jobs) < 4)
            self.jobs[key] = asyncio.create_task(self._cover(key, self.generation))

            def finished(job: asyncio.Task) -> None:
                if self.jobs.get(key) is job:
                    self.jobs.pop(key, None)

            self.jobs[key].add_done_callback(finished)
        job = self.jobs[key]
        self.waiters += 1
        try:
            return await asyncio.shield(job)
        except asyncio.CancelledError:
            if self.closed or job.cancelled():
                return None
            raise
        finally:
            self.waiters -= 1
            if job.done() and self.jobs.get(key) is job:
                self.jobs.pop(key, None)

    async def _cover(
        self, key: tuple[str, str], generation: int
    ) -> tuple[bytes, str | None] | None:
        """Wait within one total deadline; completion cannot cross lifetimes."""
        camera, event_id = key
        scope = self.scope
        wakeup = asyncio.Event()
        self.wakeups[key] = wakeup
        try:
            async with asyncio.timeout(WAIT_SECONDS):
                interest = self.interests.get(camera)
                admit = (
                    interest is not None
                    and interest[1] > time.monotonic()
                    and event_id in interest[2]
                    and key not in self.admitted
                )
                if admit:
                    self.admitted.add(key)
                reply = await self.client.recordings_cover(
                    camera,
                    event_id,
                    browse_id=interest[0] if admit else None,
                    request_if_admitted=admit,
                )
                while reply["state"] == "pending":
                    await wakeup.wait()
                    wakeup.clear()
                    reply = await self.client.recordings_cover(camera, event_id)
                if reply["state"] != "ready":
                    return None
                result = await self.client.fetch_recordings_cover(
                    camera, event_id, reply
                )
                require(
                    scope == self.available_scope() and generation == self.generation
                )
                self.cache[key] = (time.monotonic(), *result)
                self.cache.move_to_end(key)
                while (
                    len(self.cache) > 32
                    or sum(len(row[1]) for row in self.cache.values()) > CACHE_BYTES
                ):
                    self.cache.popitem(last=False)
                return result
        except (RecordingsError, TimeoutError):
            return None
        finally:
            if self.wakeups.get(key) is wakeup:
                self.wakeups.pop(key, None)
