"""Event platform — discrete push events (doorbell, package, sound, …) in real time."""

# Modified 2026-10-17: publish standard doorbell ring events alongside legacy presses.

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.event import (
    DoorbellEventType,
    EventDeviceClass,
    EventEntity,
)
from homeassistant.core import callback

from .const import EVENT_TYPE
from .entity import EufySdkDeviceEntity
from .pushmap import (
    DETECTION_CAPABILITIES,
    DETECTION_EVENTS,
    DOORBELL_EVENT,
    DOORBELL_EVENT_TYPE,
)

if TYPE_CHECKING:
    from homeassistant.core import Event, HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import EufySdkDataUpdateCoordinator
    from .data import EufySdkConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001
    entry: EufySdkConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create a Doorbell event for doorbells and a Detection event for cameras."""
    coordinator = entry.runtime_data.coordinator
    entities: list[EventEntity] = []
    for sn, dev in coordinator.data.items():
        caps = set(dev.get("capabilities", []))
        if "doorbell" in caps:
            entities.append(EufySdkDoorbellEvent(coordinator, sn))
        if caps & DETECTION_CAPABILITIES:
            entities.append(EufySdkDetectionEvent(coordinator, sn))
    async_add_entities(entities)


class _EufySdkBusEvent(EufySdkDeviceEntity, EventEntity):
    """Base: fire this event entity from matching `<DOMAIN>_event` bus messages."""

    async def async_added_to_hass(self) -> None:
        """Subscribe to bridge device events while registered."""
        await super().async_added_to_hass()
        self.async_on_remove(self.hass.bus.async_listen(EVENT_TYPE, self._handle_event))

    @callback
    def _handle_event(self, event: Event) -> None:
        """Translate a bus event for this device into an entity fire."""
        data = event.data
        if data.get("deviceSn") != self._sn:
            return
        attrs = {k: v for k, v in data.items() if k == "thumbnailUrl" and v}
        for event_type in self._event_types_for(data.get("event")):
            self._trigger_event(event_type, attrs)
            self.async_write_ha_state()

    def _event_types_for(self, bus_event: str | None) -> tuple[str, ...]:
        """Return the entity events to publish for one bridge event."""
        event_type = self._match(bus_event)
        return (event_type,) if event_type is not None else ()

    def _match(self, bus_event: str | None) -> str | None:
        """Return the HA event_type for a bridge event name, or None to ignore it."""
        raise NotImplementedError


class EufySdkDoorbellEvent(_EufySdkBusEvent):
    """A doorbell press as an event entity."""

    _attr_translation_key = "doorbell"
    _attr_device_class = EventDeviceClass.DOORBELL

    def __init__(self, coordinator: EufySdkDataUpdateCoordinator, sn: str) -> None:
        """Bind to a doorbell device."""
        super().__init__(coordinator, sn)
        self._attr_unique_id = f"{sn}_doorbell_event"
        self._attr_event_types = [DOORBELL_EVENT_TYPE, DoorbellEventType.RING]

    def _event_types_for(self, bus_event: str | None) -> tuple[str, ...]:
        """Retain legacy triggers, then leave the standard ring as the last event."""
        if self._match(bus_event) is None:
            return ()
        # Each event needs its own state write so existing pressed automations
        # observe it before ring becomes the current event_type.
        return (DOORBELL_EVENT_TYPE, DoorbellEventType.RING)

    def _match(self, bus_event: str | None) -> str | None:
        """Fire on a doorbell press."""
        return DOORBELL_EVENT_TYPE if bus_event == DOORBELL_EVENT else None


class EufySdkDetectionEvent(_EufySdkBusEvent):
    """The device's discrete detection events (pet / vehicle / package / sound / …)."""

    _attr_translation_key = "detection"

    def __init__(self, coordinator: EufySdkDataUpdateCoordinator, sn: str) -> None:
        """Bind to a camera-class device."""
        super().__init__(coordinator, sn)
        self._attr_unique_id = f"{sn}_detection_event"
        self._attr_event_types = sorted(set(DETECTION_EVENTS.values()))

    def _match(self, bus_event: str | None) -> str | None:
        """Map a detection push event to its event_type."""
        return DETECTION_EVENTS.get(bus_event) if bus_event else None
