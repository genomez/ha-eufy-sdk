"""Shared configured phone delivery and retry spacing."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

NOTICE_RETRY_DELAYS = (60, 120, 300, 600)
PHONE_FAILURE_SENTENCE = (
    "Phone delivery failed; check the notification service in the integration options."
)


def notice_retry_delay(attempts: int) -> int:
    """Return the existing capped notification retry delay."""
    return NOTICE_RETRY_DELAYS[min(max(0, attempts - 1), len(NOTICE_RETRY_DELAYS) - 1)]


async def deliver_phone_notice(  # noqa: PLR0913 - fixed delivery envelope
    hass: HomeAssistant,
    service: str,
    title: str,
    message: str,
    tag: str,
    *,
    problem: bool,
) -> None:
    """Deliver one configured notification attempt with the shared phone metadata."""
    domain, action = service.split(".", 1)
    data = {"tag": tag, "channel": "Eufy"}
    if problem:
        data.update({"importance": "high", "sticky": True})
    await hass.services.async_call(
        domain,
        action,
        {"title": title, "message": message, "data": data},
        blocking=True,
    )
