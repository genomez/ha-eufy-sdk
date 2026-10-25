"""Event platform — discrete push events (doorbell, package, sound, …) in real time."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.components.event import (
    DoorbellEventType,
    EventDeviceClass,
    EventEntity,
    EventExtraStoredData,
)
from homeassistant.core import State, callback
from homeassistant.util import dt as dt_util

from .const import CONF_LEGACY_DOORBELL_PRESSED, EVENT_TYPE
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
            entities.append(
                EufySdkDoorbellEvent(
                    coordinator,
                    sn,
                    legacy_pressed=entry.options.get(
                        CONF_LEGACY_DOORBELL_PRESSED, False
                    ),
                )
            )
        if caps & DETECTION_CAPABILITIES:
            entities.append(EufySdkDetectionEvent(coordinator, sn))
    async_add_entities(entities)


@dataclass
class _EufySdkEventExtraStoredData(EventExtraStoredData):
    """Keep the occurrence time even when HA saves an unavailable state."""

    last_event_timestamp: str | None


class _EufySdkBusEvent(EufySdkDeviceEntity, EventEntity):
    """Base: fire this event entity from matching `<DOMAIN>_event` bus messages."""

    @property
    def extra_restore_state_data(self) -> EventExtraStoredData:
        """Save the timestamp alongside HA's event type and attributes."""
        data = super().extra_restore_state_data
        return _EufySdkEventExtraStoredData(
            data.last_event_type, data.last_event_attributes, self.state
        )

    async def async_get_last_state(self) -> State | None:
        """Supply a saved occurrence time to HA's native event restoration."""
        state = await super().async_get_last_state()
        if state is None or dt_util.parse_datetime(state.state) is not None:
            return state
        extra = await self.async_get_last_extra_data()
        if extra is None:
            return state
        timestamp = extra.as_dict().get("last_event_timestamp")
        if not isinstance(timestamp, str):
            return state
        try:
            parsed = dt_util.parse_datetime(timestamp)
        except (ValueError, OverflowError):
            return state
        if parsed is None or parsed.tzinfo is None or parsed.utcoffset() is None:
            return state
        return State(state.entity_id, timestamp, state.attributes)

    async def async_added_to_hass(self) -> None:
        """Subscribe to bridge device events while registered."""
        await super().async_added_to_hass()
        self.async_on_remove(self.hass.bus.async_listen(EVENT_TYPE, self._handle_event))

    @callback
    def _handle_event(self, event: Event) -> None:
        """Translate a bus event for this device into an entity fire."""
        if self.hass is not None and self.hass.is_stopping:
            return
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

    def __init__(
        self,
        coordinator: EufySdkDataUpdateCoordinator,
        sn: str,
        *,
        legacy_pressed: bool = False,
    ) -> None:
        """Bind to a doorbell device."""
        super().__init__(coordinator, sn)
        self._attr_unique_id = f"{sn}_doorbell_event"
        # HA requires doorbell entities to advertise "ring"; legacy mode still
        # fires only "pressed".
        self._fire_type = (
            DOORBELL_EVENT_TYPE if legacy_pressed else DoorbellEventType.RING
        )
        self._attr_event_types = [DoorbellEventType.RING]
        if legacy_pressed:
            self._attr_event_types.append(DOORBELL_EVENT_TYPE)

    def _match(self, bus_event: str | None) -> str | None:
        """Fire on a doorbell press."""
        return self._fire_type if bus_event == DOORBELL_EVENT else None


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
