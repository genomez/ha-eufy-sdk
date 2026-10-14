# ruff: noqa: ANN201, D100, D101, D102, INP001, PT009, SLF001
import asyncio
import hashlib
import unittest
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock, patch

from custom_components.eufy_sdk.event_image import event_time_for_image
from custom_components.eufy_sdk.image import EufySdkEventImage


class EventTimeTests(unittest.TestCase):
    def test_time_is_paired_with_exact_bytes_and_timezone(self):
        data = b"synthetic image"
        digest = hashlib.sha256(data).hexdigest()
        expected = datetime(2026, 9, 28, 18, 0, tzinfo=UTC)
        self.assertEqual(
            event_time_for_image(data, "2026-09-28T13:00:00-05:00", digest), expected
        )
        self.assertEqual(
            event_time_for_image(data, "2026-09-28T18:00:00Z", digest), expected
        )
        for value in (None, "", "invalid", "2026-09-28T13:00:00", "1790000000"):
            self.assertIsNone(event_time_for_image(data, value, digest))
        self.assertIsNone(
            event_time_for_image(b"different image", "2026-09-28T18:00:00Z", digest)
        )
        self.assertIsNone(event_time_for_image(data, "2026-09-28T18:00:00Z", None))


class ImageEntityTests(unittest.IsolatedAsyncioTestCase):
    def entity(self):
        entity = object.__new__(EufySdkEventImage)
        entity._image = None
        entity._hash = None
        entity._event_time = None
        entity._refresh_lock = asyncio.Lock()
        entity.async_write_ha_state = Mock()
        return entity

    async def test_http_fetch_keeps_bytes_and_their_headers_together(self):
        entity = self.entity()
        entity.hass = Mock()
        entity._url = "http://bridge.invalid/event-image/camera-a"
        data = b"synthetic JPEG response"
        response = Mock(
            status=200,
            headers={
                "x-eufy-event-time": "2026-09-28T18:00:00Z",
                "x-eufy-image-sha256": hashlib.sha256(data).hexdigest(),
            },
            read=AsyncMock(return_value=data),
        )
        session = Mock(get=AsyncMock(return_value=response))
        with patch(
            "custom_components.eufy_sdk.image.async_get_clientsession",
            return_value=session,
        ):
            fetched = await entity._fetch()
        self.assertEqual(fetched, (data, datetime(2026, 9, 28, 18, tzinfo=UTC)))

    async def test_restore_uses_paired_time_and_never_receipt_clock(self):
        entity = self.entity()
        event_time = datetime(2026, 9, 28, 12, tzinfo=UTC)
        entity._fetch = AsyncMock(return_value=(b"old image", event_time))
        await entity._refresh()
        self.assertEqual(
            entity.extra_state_attributes["event_time"], event_time.isoformat()
        )
        self.assertEqual(await entity.async_image(), b"old image")
        await entity._refresh()
        entity.async_write_ha_state.assert_called_once()

    async def test_same_bytes_receive_metadata_and_new_unknown_bytes_clear_it(self):
        entity = self.entity()
        event_time = datetime(2026, 9, 28, 12, tzinfo=UTC)
        entity._fetch = AsyncMock(
            side_effect=[(b"same", None), (b"same", event_time), (b"new", None)]
        )
        await entity._refresh()
        self.assertIsNone(entity.extra_state_attributes["event_time"])
        await entity._refresh()
        self.assertEqual(
            entity.extra_state_attributes["event_time"], event_time.isoformat()
        )
        await entity._refresh()
        self.assertIsNone(entity.extra_state_attributes["event_time"])
        self.assertEqual(await entity.async_image(), b"new")

    async def test_delayed_older_response_and_failed_fetch_preserve_new_pair(self):
        entity = self.entity()
        newest = datetime(2026, 9, 28, 12, tzinfo=UTC)
        older = datetime(2026, 9, 28, 11, tzinfo=UTC)
        entity._fetch = AsyncMock(side_effect=[(b"new", newest), (b"old", older), None])
        for _ in range(3):
            await entity._refresh()
        self.assertEqual(await entity.async_image(), b"new")
        self.assertEqual(
            entity.extra_state_attributes["event_time"], newest.isoformat()
        )
        entity.async_write_ha_state.assert_called_once()

    async def test_concurrent_refreshes_remain_serialized(self):
        entity = self.entity()
        active = 0
        peak = 0
        calls = 0

        async def fetch() -> tuple[bytes, None]:
            nonlocal active, peak, calls
            active += 1
            calls += 1
            peak = max(peak, active)
            image = str(calls).encode()
            await asyncio.sleep(0)
            active -= 1
            return image, None

        entity._fetch = fetch
        await asyncio.gather(entity._refresh(), entity._refresh())
        self.assertEqual(peak, 1)
        self.assertEqual(await entity.async_image(), b"2")


if __name__ == "__main__":
    unittest.main()
