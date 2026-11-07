# ruff: noqa: PLR2004 - fixed playback session and media limits
"""Entry-owned HLS sessions with bounded idle fallback for the stock media dialog."""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from uuid import uuid4

from .recordings_models import RecordingsError, require


def _idle_deadline(session: dict) -> float:
    """Retain immutable ENDLIST history until the signed presentation expires."""
    if session["reply"]["delivery"] == "complete":
        return session["deadline"]
    return min(session["deadline"], time.monotonic() + 30)


class ProgressiveRuntime:
    """Keep signed history stable and never renew a revoked entry generation."""

    def init_playback(self) -> None:
        """Allocate a small local session table and best-effort release tasks."""
        self.playbacks = {}
        self.playback_releases = set()
        self.playback_opening = 0

    def reset_playback(self) -> None:
        """Release leases without making synchronous unload wait for transport."""
        for session in self.playbacks.values():
            try:
                task = asyncio.get_running_loop().create_task(
                    self._release_playback(session["reply"])
                )
            except RuntimeError:
                continue
            self.playback_releases.add(task)
            task.add_done_callback(self.playback_releases.discard)
        self.playbacks.clear()

    async def _release_playback(self, reply: dict) -> None:
        """Bridge expiry remains authoritative if the connection is already gone."""
        with suppress(RecordingsError, TimeoutError):
            await self.client.recordings_playback_command("release", reply)

    def progressive_supported(self) -> bool:
        """Absent additive capability preserves the completed MP4 pipeline."""
        return (
            self.playback_supported()
            and (self.client.recordings_capability or {}).get("progressivePlayback")
            is True
        )

    async def open_playback(self, camera: str, event_id: str) -> dict | None:
        """Return a real buffered presentation, or None for complete-file fallback."""
        for identity, session in list(self.playbacks.items()):
            if time.monotonic() >= min(session["deadline"], session["idle"]):
                self.playbacks.pop(identity, None)
                await self._release_playback(session["reply"])
        require(
            self.playback_opening < 8
            and len(self.playbacks) + self.playback_opening < 32
        )
        self.playback_opening += 1
        try:
            return await self._open_playback(camera, event_id)
        finally:
            self.playback_opening -= 1

    async def _open_playback(self, camera: str, event_id: str) -> dict | None:
        """Keep each pending admission inside the existing runtime's finite bounds."""
        require(self.progressive_supported())
        page = await self.browse(camera, event_id)
        self.validate_clip(camera, event_id)
        duration = page["events"][0]["durationSeconds"]
        if duration is None or not 0 < duration <= 3600:
            return None
        scope, generation = self.scope, self.generation
        require(len(self.playbacks) < 32)
        reply = await self.client.recordings_playback_open(
            camera, event_id, uuid4().hex
        )
        session = {
            "reply": reply,
            "scope": scope,
            "generation": generation,
            "deadline": time.monotonic()
            + max(0, min(4500, reply["hardExpiresAtMs"] / 1000 - time.time())),
            "idle": time.monotonic() + 180,
            "signed": {},
            "lock": asyncio.Lock(),
        }
        handed_off = False
        try:
            async with asyncio.timeout(150 + duration * 3 + 120):
                while True:
                    self.validate_playback(session)
                    if reply["clip"]["state"] == "ready":
                        return {"format": "mp4", "clip": reply["clip"]}
                    if reply["delivery"] in ("playable", "complete"):
                        session["idle"] = _idle_deadline(session)
                        self.playbacks[reply["sessionId"]] = session
                        handed_off = True
                        return {"format": "hls", "session": session}
                    require(reply["delivery"] != "unavailable")
                    await asyncio.sleep(0.5)
                    reply = await self.client.recordings_playback_command(
                        "renew", reply
                    )
                    session["reply"] = reply
                    session["idle"] = _idle_deadline(session)
        finally:
            if not handed_off:
                await self._release_playback(session["reply"])

    def validate_playback(self, session: dict) -> None:
        """Check enrollment, scope, local generation, hard lifetime and idle expiry."""
        require(
            self.progressive_supported()
            and self.available_scope() == session["scope"]
            and self.generation == session["generation"]
            and time.monotonic() < min(session["deadline"], session["idle"])
        )
        reply = session["reply"]
        self.validate_clip(reply["cameraId"], reply["eventId"])

    async def playback_session(
        self, camera: str, event_id: str, session_id: str, generation: str
    ) -> dict:
        """Renew only an existing finite lease on authenticated HTTP activity."""
        session = self.playbacks.get(session_id)
        require(session is not None)
        self.validate_playback(session)
        old = session["reply"]
        require(
            old["cameraId"] == camera
            and old["eventId"] == event_id
            and old["generation"] == generation
        )
        async with session["lock"]:
            if time.monotonic() - session.get("checked", 0) >= 0.5:
                reply = await self.client.recordings_playback_command(
                    "renew", session["reply"]
                )
                self.validate_playback(session)
                require(reply["segments"][: len(old["segments"])] == old["segments"])
                require(reply["delivery"] in ("playable", "complete"))
                if old["delivery"] == "complete":
                    require(
                        reply["delivery"] == "complete"
                        and reply["segments"] == old["segments"]
                    )
                session["reply"] = reply
                session["checked"] = time.monotonic()
        session["idle"] = _idle_deadline(session)
        return session
