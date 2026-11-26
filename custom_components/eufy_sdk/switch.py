"""Switch platform — writable booleans, plus per-bit switches for known bitfields."""

from __future__ import annotations

from time import time
from typing import TYPE_CHECKING, Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.restore_state import (
    ExtraStoredData,
    RestoredExtraData,
    RestoreEntity,
)

from .bespoke import BITFIELD_SWITCHES
from .entity import (
    EufySdkDeviceEntity,
    EufySdkPropertyEntity,
    EufySolixEntity,
    classify,
    has_capability,
    is_setting,
    solix_devices_with,
)
from .light import LIGHT_OWNED_PROPS
from .light_schedule import t9000_attached
from .lock import LOCK_OWNED_PROPS
from .privacy import (
    PRIVACY_REPORT_KEY,
    is_attached_camera,
    is_s1_privacy_camera,
    offers_s1_privacy,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import EufySdkDataUpdateCoordinator
    from .data import EufySdkConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001
    entry: EufySdkConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create a switch per writable bool, and per-bit switches for known bitfields."""
    coordinator = entry.runtime_data.coordinator
    entities: list[SwitchEntity] = []
    for sn in coordinator.data:
        is_smart_light = has_capability(coordinator.data[sn], "smart_light")
        is_lock = has_capability(coordinator.data[sn], "lock")
        for spec in entry.runtime_data.properties.get(sn, []):
            # The light platform owns lightPower/lightBrightness for smart_light — don't
            # also surface them as a bare switch/number (would double the control).
            if is_smart_light and spec["name"] in LIGHT_OWNED_PROPS:
                continue
            # The lock platform owns the 'locked' — don't also surface it as a switch.
            if is_lock and spec["name"] in LOCK_OWNED_PROPS:
                continue
            if (
                spec["name"] == "privacy"
                and is_attached_camera(coordinator.data[sn])
                and t9000_attached(coordinator, sn) is not False
            ):
                if offers_s1_privacy(coordinator, sn, spec):
                    entities.append(EufyPrivacySwitch(coordinator, sn))
                continue
            kind = classify(spec, t9000=t9000_attached(coordinator, sn) is True)
            if kind == "switch":
                switch_type = (
                    EufySdkWriteOnlySwitch
                    if spec.get("writeOnly") and spec["name"] != "light"
                    else EufySdkSwitch
                )
                entities.append(switch_type(coordinator, sn, spec))
            elif kind == "bitfield" and spec["name"] in BITFIELD_SWITCHES:
                bf = BITFIELD_SWITCHES[spec["name"]]
                entities.extend(
                    EufyBitmaskSwitch(
                        coordinator,
                        sn,
                        spec,
                        {"key": key, "bit": bit, "base": bf["base"]},
                    )
                    for key, bit in bf["bits"].items()
                )

    # Anker Solix (separate account): a Solarbank's ambient light — a standalone switch
    # that reflects the device state (ambientLightOn, decoded from the ff09 telemetry).
    entities.extend(
        EufySolixLightSwitch(coordinator, sn)
        for sn, _ in solix_devices_with(coordinator, "battery")
    )

    async_add_entities(entities)


class EufyPrivacySwitch(EufySdkDeviceEntity, SwitchEntity):
    """Control station privacy and display its time-bounded receipt."""

    _attr_name = "Privacy"

    def __init__(self, coordinator: EufySdkDataUpdateCoordinator, sn: str) -> None:
        """Bind the station privacy switch to its camera."""
        super().__init__(coordinator, sn)
        self._attr_unique_id = f"{sn}_privacy"
        self._cancel_expiry = None

    def _report_state(self) -> tuple[bool | None, float | None]:
        if not is_s1_privacy_camera(self.coordinator, self._sn):
            return None, None
        report = self.device.get(PRIVACY_REPORT_KEY)
        if report is None:
            return None, None
        value, received_at_ms = report
        if self.device.get("state", {}).get("privacy") is not value:
            return None, None
        # Allow one missed SDK cloud poll and one HA snapshot interval to sample it.
        interval = self.coordinator.update_interval
        snapshot_seconds = interval.total_seconds() if interval else 0
        max_age = snapshot_seconds + 2 * self.coordinator.cloud_poll_interval_ms / 1000
        age = time() - received_at_ms / 1000
        if age < 0:
            return None, min(-age, max_age)
        if age < max_age:
            return value, max_age - age
        return None, None

    @property
    def is_on(self) -> bool | None:
        """Return the reported privacy state while the receipt is recent."""
        return self._report_state()[0]

    async def async_turn_on(self, **_: Any) -> None:
        """Request privacy on and wait for the station report."""
        await self.client.set_property(
            self._sn, "privacy", value=True, station_privacy=True
        )

    async def async_turn_off(self, **_: Any) -> None:
        """Request privacy off and wait for the station report."""
        await self.client.set_property(
            self._sn, "privacy", value=False, station_privacy=True
        )

    async def async_added_to_hass(self) -> None:
        """Schedule expiration for the initial observation."""
        await super().async_added_to_hass()
        self.async_on_remove(self._cancel_expiration)
        self._schedule_expiration()

    @callback
    def _cancel_expiration(self) -> None:
        if self._cancel_expiry is not None:
            self._cancel_expiry()
            self._cancel_expiry = None

    @callback
    def _schedule_expiration(self) -> None:
        self._cancel_expiration()
        _, delay = self._report_state()
        if delay is not None:
            self._cancel_expiry = async_call_later(
                self.hass, delay, self._expire_report
            )

    @callback
    def _expire_report(self, _now: object) -> None:
        self._cancel_expiry = None
        self._handle_coordinator_update()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._schedule_expiration()
        super()._handle_coordinator_update()


class EufySolixLightSwitch(EufySolixEntity, SwitchEntity):
    """
    A Solarbank's ambient light as a switch — reflects the DEVICE state.

    Toggling issues an encrypted `set_device_attrs` write through the bridge. The state
    is read back from the `ff09` telemetry: the SDK decodes `ambientLightOn` (1/0) from
    tag `0xba` bit 0x20 (inverted), live-confirmed on an AE103 across app + HA toggles.
    So a change made outside HA (the app / physical button) is reflected within a few
    seconds via `solixReading` events, and this is NOT assumed-state. The value seeds
    from the bridge's `solix.devices` snapshot and updates on live events. To avoid a UI
    flicker while the ~seconds-late telemetry catches up, a toggle we issue sets the
    shown state optimistically; the next reading confirms it. Device is `solix:<sn>`.
    """

    _attr_name = "Ambient Light"
    _attr_icon = "mdi:led-strip-variant"

    def __init__(self, coordinator: EufySdkDataUpdateCoordinator, sn: str) -> None:
        """Bind to a Solix Solarbank's ambient light."""
        super().__init__(coordinator, sn, "ambient_light", watch=("ambientLightOn",))

    @property
    def is_on(self) -> bool | None:
        """The light's state from telemetry (None until a reading arrives)."""
        v = self.solix_value("ambientLightOn")
        return None if v is None else bool(v)

    async def async_turn_on(self, **_: Any) -> None:
        """Turn the ambient light on (optimistic; telemetry confirms shortly)."""
        await self.client.set_solix_light(self._sn, on=True)
        self._solix_values["ambientLightOn"] = True
        self.async_write_ha_state()

    async def async_turn_off(self, **_: Any) -> None:
        """Turn the ambient light off (optimistic; telemetry confirms shortly)."""
        await self.client.set_solix_light(self._sn, on=False)
        self._solix_values["ambientLightOn"] = False
        self.async_write_ha_state()


class EufySdkSwitch(EufySdkPropertyEntity, SwitchEntity):
    """A writable boolean property as a switch."""

    # Floodlight receipts change on every report. Scripts read the live values;
    # keeping them out of the recorder lets attribute rows deduplicate.
    _unrecorded_attributes = frozenset(
        {"device_observed_at", "device_observed_value", "device_observation_source"}
    )

    def __init__(
        self,
        coordinator: EufySdkDataUpdateCoordinator,
        sn: str,
        spec: dict[str, Any],
    ) -> None:
        """Put a setting toggle under Configuration; leave a primary control up top."""
        super().__init__(coordinator, sn, spec)
        if is_setting(self._prop):
            self._attr_entity_category = EntityCategory.CONFIG

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """
        Expose floodlight receipts separately from optimistic state.

        device_observed_at remains available to scripts confirming floodlight commands.
        """
        attributes = {}
        if self._prop == "light":
            observations = self.coordinator.property_observations or {}
            receipt = observations.get(self._sn, {}).get(self._prop)
            if receipt and type(receipt["value"]) is bool:
                attributes.update(
                    {
                        "device_observed_at": receipt["observed_at"],
                        "device_observed_value": receipt["value"],
                        "device_observation_source": "rtc",
                    }
                )
        if self._command_profile is not None:
            attributes["command_profile"] = self._command_profile
        if self._command_receipt is not None:
            attributes.update(
                {
                    "command_status": self._command_receipt.status,
                    "command_effect": self._command_receipt.effect,
                    "command_send_attempted": self._command_receipt.send_attempted,
                }
            )
            if self._command_receipt.rejection_code is not None:
                attributes["command_rejection_code"] = (
                    self._command_receipt.rejection_code
                )
            if self._command_receipt.response_latency_ms is not None:
                attributes["command_response_latency_ms"] = (
                    self._command_receipt.response_latency_ms
                )
            if self._command_receipt.superseded:
                attributes["command_superseded"] = True
        if self._light_correction is not None:
            attributes.update(self._light_correction.attributes)
        return attributes

    @property
    def is_on(self) -> bool | None:
        """On when the property's live value is truthy."""
        v = self.prop_value
        return None if v is None else bool(v)

    async def async_turn_on(self, **_: Any) -> None:
        """Set the property true."""
        await self.write(value=True)

    async def async_turn_off(self, **_: Any) -> None:
        """Set the property false."""
        await self.write(value=False)


class EufySdkWriteOnlySwitch(EufySdkSwitch, RestoreEntity):
    """Remember successful HA writes when the device cannot report a value."""

    _attr_assumed_state = True

    @property
    def prop_value(self) -> bool | None:
        """Stay unknown until HA writes or restores a successfully written value."""
        return self._assumed_value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Mark written values so legacy reported states cannot seed restoration."""
        attributes = super().extra_state_attributes
        if type(self._assumed_value) is bool:
            attributes["last_written_value"] = self._assumed_value
        return attributes

    @property
    def extra_restore_state_data(self) -> ExtraStoredData | None:
        """
        Persist the written value apart from state attributes.

        HA drops attributes while an entity is unavailable, so a value kept only
        there would be lost by a save made during a bridge outage.
        """
        if type(self._assumed_value) is bool:
            return RestoredExtraData({"last_written_value": self._assumed_value})
        return None

    async def write(self, value: Any) -> None:
        """
        Keep the newest write that succeeded, even when a later one fails.

        The shared write path forgets a successful write once a newer write has
        started; with no read-back, a failure of that newer write would otherwise
        leave the value from before both writes.
        """
        seq = getattr(self, "_write_seq", 0) + 1
        self._write_seq = seq
        lifecycle = self._lifecycle_generation
        await super().write(value)
        if (
            type(value) is bool
            and seq > getattr(self, "_recorded_seq", 0)
            and not self._removed
            and lifecycle == self._lifecycle_generation
        ):
            self._recorded_seq = seq
            self._assumed_value = value
            self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Restore only a remembered HA write, preserving any newer write."""
        await super().async_added_to_hass()
        extra = await self.async_get_last_extra_data()
        value = extra.as_dict().get("last_written_value") if extra else None
        if type(value) is not bool:
            last = await self.async_get_last_state()
            value = last.attributes.get("last_written_value") if last else None
        # Only a successful write outranks the restored value; a failed one must not.
        if (
            type(value) is bool
            and getattr(self, "_recorded_seq", 0) == 0
            and not self._removed
        ):
            self._assumed_value = value

    @callback
    def _property_event_filter(self, _data: dict[str, Any]) -> bool:
        """Ignore reports for a property whose value cannot be read back."""
        return False


class EufyBitmaskSwitch(EufySdkPropertyEntity, SwitchEntity):
    """One bit of a bitfield property as a switch (writes back the whole mask)."""

    # Each bit is its own control ("Detect human"), not the parent property, so the
    # generic property label must not be applied over the per-bit name.
    _named_by_translation = True

    def __init__(
        self,
        coordinator: EufySdkDataUpdateCoordinator,
        sn: str,
        spec: dict[str, Any],
        bitdef: dict[str, Any],
    ) -> None:
        """Bind to a single bit of the parent bitfield property ({key, bit, base})."""
        super().__init__(coordinator, sn, spec)
        self._bit: int = bitdef["bit"]
        self._base: int = bitdef["base"]
        self._attr_unique_id = f"{sn}_{self._prop}_{self._bit}"
        self._attr_translation_key = bitdef["key"]
        self._attr_entity_category = EntityCategory.CONFIG

    def _mask(self) -> int:
        """Use a reported mask, falling back only when the value is absent."""
        v = self.prop_value
        if v is None:
            return self._base
        mask = _as_mask(v)
        if mask is None:
            msg = "Detection setting is not a bitmask; refusing to overwrite it"
            raise HomeAssistantError(msg)
        return mask

    @property
    def is_on(self) -> bool | None:
        """On when this bit is set; unknown for a value that is not a bitmask."""
        mask = _as_mask(self.prop_value)
        return None if mask is None else bool(mask & self._bit)

    async def async_turn_on(self, **_: Any) -> None:
        """Set this bit using the current mask or existing fallback."""
        await self.write(self._mask() | self._bit)

    async def async_turn_off(self, **_: Any) -> None:
        """Clear this bit using the current mask or existing fallback."""
        await self.write(self._mask() & ~self._bit)


def _as_mask(value: Any) -> int | None:
    """Read numeric masks consistently without admitting structured values."""
    if isinstance(value, (int, float)) or (
        isinstance(value, str) and value.strip().lstrip("-").isdigit()
    ):
        try:
            return int(value)
        except (ValueError, OverflowError):
            return None
    return None
