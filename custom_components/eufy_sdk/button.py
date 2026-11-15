"""Button platform — device-level actions the bridge exposes (reboot, PTZ)."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError

from .api import EufySdkApiClientError
from .audit_visibility import hidden_default
from .const import DOMAIN
from .entity import EufySdkDeviceEntity, has_capability
from .light_schedule import t9000_attached

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import EufySdkDataUpdateCoordinator
    from .data import EufySdkConfigEntry

# Every PTZ control is keyed by the bridge action it sends, and carries how it presents:
#   {action: {"key": translation key, "icon": …, "category": under Configuration}}
#
# The d-pad, in the order a d-pad reads. Each verb is a no-arg method on the SDK's `ptz`
# surface, so the action name IS the verb. The displayed name comes from the translation
# key rather than living here, so it follows the user's language.
_PTZ_STEPS: dict[str, dict[str, Any]] = {
    "up": {"key": "tilt_up", "icon": "mdi:arrow-up"},
    "down": {"key": "tilt_down", "icon": "mdi:arrow-down"},
    "left": {"key": "pan_left", "icon": "mdi:arrow-left"},
    "right": {"key": "pan_right", "icon": "mdi:arrow-right"},
}
_LOGGER = logging.getLogger(__name__)
_RESPONSE_LIST_RETRY_SECONDS = 30


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EufySdkConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create Reboot, Refresh-Last-Event and the PTZ controls (pan-tilt cameras)."""
    coordinator = entry.runtime_data.coordinator
    entities: list[ButtonEntity] = [
        EufySdkRebootButton(coordinator, sn)
        for sn, dev in coordinator.data.items()
        if dev.get("canReboot")
    ]
    # A "Refresh Last Event" button per camera/doorbell (same set as the event Image
    # entity, gated on `stream`): forces the bridge to pull the newest event cover now —
    # a manual override for when the auto-refresh raced the HomeBase writing the crop.
    entities.extend(
        EufyRefreshEventButton(coordinator, sn)
        for sn, dev in coordinator.data.items()
        if dev.get("stream")
    )
    # The d-pad, gated on the `ptz` capability the bridge
    # reports: a fixed camera has no `ptz` surface, so the bridge would refuse every
    # verb and the buttons would sit there as dead controls.
    for sn, dev in coordinator.data.items():
        if not has_capability(dev, "ptz"):
            continue
        entities.extend(
            EufySdkPtzButton(coordinator, sn, action, meta)
            for action, meta in _PTZ_STEPS.items()
        )
    async_add_entities(entities)
    for sn, dev in coordinator.data.items():
        if has_capability(dev, "doorbell") and t9000_attached(coordinator, sn):
            responses = QuickResponseButtons(coordinator, sn, async_add_entities)
            async_add_entities(
                [EufyQuickResponseRefreshButton(coordinator, sn, responses)]
            )
            entry.async_create_background_task(
                hass, responses.discover(), "Quick response list discovery"
            )


class EufySdkRebootButton(EufySdkDeviceEntity, ButtonEntity):
    """Reboot a HomeBase — a device-level action, not a writable property."""

    _attr_device_class = ButtonDeviceClass.RESTART
    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "reboot"

    def __init__(self, coordinator: EufySdkDataUpdateCoordinator, sn: str) -> None:
        """Bind to a HomeBase serial."""
        super().__init__(coordinator, sn)
        self._attr_unique_id = f"{sn}_reboot"

    async def async_press(self) -> None:
        """Reboot the HomeBase (it drops offline for a minute or two)."""
        client = self.client
        await client.reboot(self._sn)


class EufyRefreshEventButton(EufySdkDeviceEntity, ButtonEntity):
    """Force a 'Last event' image refresh — pull the newest event cover now."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:image-refresh"
    _attr_translation_key = "refresh_last_event"

    def __init__(self, coordinator: EufySdkDataUpdateCoordinator, sn: str) -> None:
        """Bind to a camera/doorbell serial."""
        super().__init__(coordinator, sn)
        self._attr_unique_id = f"{sn}_refresh_last_event"
        self._attr_entity_registry_visible_default = not hidden_default(
            coordinator, sn, "refresh_last_event"
        )

    async def async_press(self) -> None:
        """Ask the bridge to re-pull the newest event cover (nudges the Image)."""
        client = self.client
        await client.refresh_event_image(self._sn)


class EufySdkPtzButton(EufySdkDeviceEntity, ButtonEntity):
    """
    One pan-tilt step, in the direction this button carries.

    Wait for the bridge's command acknowledgement. A missing acknowledgement does
    not prove the camera stayed still, and an acknowledgement does not confirm its
    final position; movement notifications arrive separately as `ptzNotify` events.
    """

    def __init__(
        self,
        coordinator: EufySdkDataUpdateCoordinator,
        sn: str,
        action: str,
        meta: dict[str, Any],
    ) -> None:
        """Bind to a camera serial and the bridge action this button sends."""
        super().__init__(coordinator, sn)
        self._action = action
        self._attr_unique_id = f"{sn}_ptz_{action.replace('.', '_')}"
        self._attr_translation_key = meta["key"]
        self._attr_icon = meta["icon"]
        self._attr_entity_category = meta.get("category")

    @property
    def _args(self) -> tuple:
        """Positional arguments for the action — a movement step takes none."""
        return ()

    async def async_press(self) -> None:
        """Send this button's PTZ action to the bridge."""
        client = self.client
        try:
            await client.action(self._sn, self._action, *self._args)
        except EufySdkApiClientError as err:
            if str(err) != "command_ack_timeout":
                raise
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="ptz_ack_timeout",
            ) from err


class QuickResponseButtons:
    """Own an on-demand response catalogue; refresh never plays audio."""

    def __init__(
        self,
        coordinator: EufySdkDataUpdateCoordinator,
        sn: str,
        add_entities: AddEntitiesCallback,
    ) -> None:
        """Keep the catalogue local to this platform lifecycle."""
        self.coordinator = coordinator
        self.sn = sn
        self.add_entities = add_entities
        self.buttons = {}
        self.responses = {}

    async def discover(self) -> None:
        """Query off the startup path, retrying only LIST once after a delay."""
        for attempt in range(2):
            try:
                await self.refresh()
            except EufySdkApiClientError:
                if attempt == 0:
                    await asyncio.sleep(_RESPONSE_LIST_RETRY_SECONDS)
                else:
                    _LOGGER.warning(
                        "Quick response list unavailable; use Refresh quick responses"
                    )
            else:
                return

    async def refresh(self) -> None:
        """Replace the catalogue once, adding new IDs and disabling removed IDs."""
        rows = await self.coordinator.config_entry.runtime_data.client.quick_responses(
            self.sn
        )
        self.responses = {
            row["voiceId"]: row["name"]
            for row in rows
            if isinstance(row, dict)
            and type(row.get("voiceId")) is int
            and row["voiceId"] >= 0
            and isinstance(row.get("name"), str)
        }
        added = []
        for voice_id, name in self.responses.items():
            if voice_id not in self.buttons:
                button = self.buttons[voice_id] = EufyQuickResponseButton(
                    self.coordinator, self.sn, voice_id, self
                )
                added.append(button)
            self.buttons[voice_id].update_name(name)
        if added:
            self.add_entities(added)
        for button in self.buttons.values():
            if button.hass is not None:
                button.async_write_ha_state()


class EufyQuickResponseButton(EufySdkDeviceEntity, ButtonEntity):
    """An explicit, non-restoring, non-retrying doorbell audio action."""

    _attr_icon = "mdi:account-voice"

    def __init__(
        self,
        coordinator: EufySdkDataUpdateCoordinator,
        sn: str,
        voice_id: int,
        catalogue: QuickResponseButtons,
    ) -> None:
        """Bind a stable voice ID; constructing this entity performs no I/O."""
        super().__init__(coordinator, sn)
        self.voice_id = voice_id
        self.catalogue = catalogue
        self._attr_unique_id = f"{sn}_quick_response_{voice_id}"

    def update_name(self, name: str) -> None:
        """Adopt the SDK name without changing the voice ID identity."""
        self._attr_name = name

    @property
    def available(self) -> bool:
        """Removed responses cannot be pressed through stale entity references."""
        return super().available and self.voice_id in self.catalogue.responses

    async def async_press(self) -> None:
        """Only this explicit press can play; failure propagates without retry."""
        if self.voice_id not in self.catalogue.responses:
            message = "Quick response unavailable"
            raise HomeAssistantError(message)
        try:
            await self.client.play_quick_response(self._sn, self.voice_id)
        except EufySdkApiClientError:
            message = "Quick response could not be confirmed; it may still play"
            raise HomeAssistantError(message) from None


class EufyQuickResponseRefreshButton(EufySdkDeviceEntity, ButtonEntity):
    """Refresh response names and IDs on demand without a polling timer."""

    _attr_name = "Refresh quick responses"
    _attr_icon = "mdi:refresh"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: EufySdkDataUpdateCoordinator,
        sn: str,
        catalogue: QuickResponseButtons,
    ) -> None:
        """Bind the manual catalogue refresh action."""
        super().__init__(coordinator, sn)
        self.catalogue = catalogue
        self._attr_unique_id = f"{sn}_refresh_quick_responses"

    async def async_press(self) -> None:
        """Query response names once without playing a response."""
        try:
            await self.catalogue.refresh()
        except EufySdkApiClientError:
            message = "Quick response list unavailable; try refreshing later"
            raise HomeAssistantError(message) from None
