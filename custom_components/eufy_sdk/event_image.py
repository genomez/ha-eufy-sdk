"""Validate event metadata supplied with the exact image response."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from homeassistant.util import dt as dt_util

if TYPE_CHECKING:
    from datetime import datetime


def event_time_for_image(
    data: bytes, event_time: str | None, image_digest: str | None
) -> datetime | None:
    """Accept an explicitly zoned event time only when the response digest matches."""
    if not event_time or not image_digest:
        return None
    if hashlib.sha256(data).hexdigest() != image_digest:
        return None
    try:
        value = dt_util.parse_datetime(event_time)
    except (ValueError, TypeError, OverflowError):
        return None
    if value is None or value.tzinfo is None or value.utcoffset() is None:
        return None
    return dt_util.as_utc(value)
