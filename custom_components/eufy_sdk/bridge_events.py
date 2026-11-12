"""Deliver bridge messages internally without recorder-visible bus traffic."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)

from .const import DOMAIN, EVENT_TYPE

if TYPE_CHECKING:
    from collections.abc import Callable

_BRIDGE_SIGNAL = f"{DOMAIN}_bridge_message"


@callback
def async_listen_bridge_event(
    hass: HomeAssistant,
    listener: Callable[[Event], None],
    *,
    event_filter: Callable[[dict[str, Any]], bool] | None = None,
) -> Callable[[], None]:
    """Subscribe with the same event data and optional filter as the public bus."""

    @callback
    def handle_event(event: Event) -> None:
        if event_filter is None or event_filter(event.data):
            listener(event)

    return async_dispatcher_connect(hass, _BRIDGE_SIGNAL, handle_event)


@callback
def async_dispatch_bridge_event(hass: HomeAssistant, data: dict[str, Any]) -> None:
    """Schedule internal consumers independently of the public event allowlist."""
    async_dispatcher_send(hass, _BRIDGE_SIGNAL, Event(EVENT_TYPE, data))
