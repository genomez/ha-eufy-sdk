"""
Siren platform — the `siren` capability's dedicated trigger()/stop() actions.

Like `lock`, an alarm is not a writable bool: the SDK exposes it as momentary
methods, so this bypasses `switch.py`'s property routing and calls `client.action()`.

Which devices get one: devices whose bound SDK siren manifest installs both a
start action (`trigger` or standalone `test`) and `stop`. A capability label alone
does not prove that an attached camera has a routable alarm. The three families
do NOT share a command surface, and the split runs right through turn_on:

  * HomeBase / attached camera — `trigger(seconds)` plus `stop`. No sounding state is
    reported, so sounding state remains unknown until a device report establishes it.
    A command acknowledgement or elapsed duration is not a state report.
  * standalone siren — `test` plus `stop`; the SDK installs no `trigger` for it, and
    `test` takes no duration. Here the device reports `siren` (RING_STATUS), so the
    state is read rather than assumed — including alarms nobody in HA started.
"""

# Modified for genomez S1 experimental source preview, 2026-10-17.
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.siren import SirenEntity, SirenEntityFeature
from homeassistant.exceptions import HomeAssistantError

from .entity import EufySdkDeviceEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import EufySdkDataUpdateCoordinator
    from .data import EufySdkConfigEntry

# `siren.turn_on` may omit a duration, but the SDK rejects anything that is not a
# positive whole number of seconds — so a call without one gets this bounded default
# rather than a sound nobody asked to stop.
DEFAULT_DURATION_SECS = 30


def is_standalone_siren(record: dict | None) -> bool:
    """
    Whether this `siren` device is the standalone family (`test`/`stop`, no `trigger`).

    The codec is a sufficient discriminator because the capability's own detection
    already did the work: a sensor-codec device earns `siren` ONLY through the
    standalone evidence (reported alarm volume plus ring status or alarm timeout),
    while the other two families are a HomeBase (station codec) and a camera attached
    to one. So `codec == "sensor"` and "standalone" cannot come apart here.
    """
    return (record or {}).get("codec") == "sensor"


def has_siren_actions(record: dict | None) -> bool:  # noqa: PLR0911
    """Require installed start/stop methods from the bound SDK siren manifest."""
    if not isinstance(record, dict):
        return False
    capabilities = record.get("capabilities")
    if not isinstance(capabilities, list) or "siren" not in capabilities:
        return False
    manifest = (record or {}).get("decodedProperties")
    if not isinstance(manifest, dict) or manifest.get("bound") is not True:
        return False
    details = manifest.get("details")
    if not isinstance(details, list) or any(
        not isinstance(item, dict) for item in details
    ):
        return False
    required = {"test" if is_standalone_siren(record) else "trigger", "stop"}
    # A duplicate or mismatched namespace is ambiguous: never combine methods
    # across descriptors or partially accept a malformed siren action surface.
    sirens = [
        item
        for item in details
        if isinstance(item, dict)
        and (item.get("accessor") == "siren" or item.get("capability") == "siren")
    ]
    if len(sirens) != 1:
        return False
    capability = sirens[0]
    if capability.get("accessor") != "siren" or capability.get("capability") != "siren":
        return False
    actions = capability.get("actions")
    undescribed = capability.get("undescribedActions")
    if not isinstance(actions, list) or not isinstance(undescribed, list):
        return False
    if any(
        not isinstance(action, dict)
        or not isinstance(action.get("name"), str)
        or not action["name"]
        for action in actions
    ) or any(not isinstance(name, str) or not name for name in undescribed):
        return False
    names = {action["name"] for action in actions} | set(undescribed)
    return required <= names


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001
    entry: EufySdkConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create a Siren entity only where the SDK installs start and stop actions."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        EufySdkSiren(coordinator, sn)
        for sn, dev in coordinator.data.items()
        if has_siren_actions(dev)
    )


class EufySdkSiren(EufySdkDeviceEntity, SirenEntity):
    """A eufy alarm output, driven by the SDK's `trigger`/`test`/`stop` actions."""

    _attr_name = "Siren"

    def __init__(self, coordinator: EufySdkDataUpdateCoordinator, sn: str) -> None:
        """Bind to an alarm-capable device serial and pin its command family."""
        super().__init__(coordinator, sn)
        self._attr_unique_id = f"{sn}_siren"
        # The family is a property of the hardware, not of the moment — a device does
        # not change codec — so it is read once here rather than on every call.
        self._standalone = is_standalone_siren(coordinator.data.get(sn))
        # DURATION is advertised only where a duration wire exists. Claiming it on a
        # standalone siren would let HA accept a `duration` that `test` silently drops.
        self._attr_supported_features = (
            SirenEntityFeature.TURN_ON | SirenEntityFeature.TURN_OFF
        )
        if not self._standalone:
            self._attr_supported_features |= SirenEntityFeature.DURATION

    @property
    def available(self) -> bool:
        """Withdraw operation when a refreshed manifest removes siren methods."""
        return super().available and has_siren_actions(self.device)

    @property
    def is_on(self) -> bool | None:
        """
        The device-reported sounding state, or unknown without a report.

        Command acceptance and a requested timeout do not establish whether an
        alarm sounded or stopped. HomeBase and attached-camera commands therefore
        leave this unknown unless the bridge supplies an actual state report.

        Where it IS reported, it is the device's own RING_STATUS: an alarm started from
        the eufy app, a keypad or a station rule shows up here too, not just ours.
        """
        state = self.device.get("state")
        reported = state.get("siren") if isinstance(state, dict) else None
        return reported if isinstance(reported, bool) else None

    def _require_siren_actions(self) -> None:
        """Refuse commands after the SDK withdraws the verified siren surface."""
        if not has_siren_actions(self.device):
            message = "Siren actions are unavailable"
            raise HomeAssistantError(message)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Sound the alarm — `trigger(duration)`, or `test` on a standalone siren."""
        self._require_siren_actions()
        client = self.client
        if self._standalone:
            # `test` is the standalone family's only "make noise now" wire, and it
            # carries no duration: the device stops itself after its own configured
            # alarm timeout. Nothing is assumed here — RING_STATUS reports the truth,
            # and a hold guessing at a duration the device owns would only fight it.
            await client.action(self._sn, "test")
            return
        duration = int(kwargs.get("duration") or DEFAULT_DURATION_SECS)
        await client.action(self._sn, "trigger", duration)

    async def async_turn_off(self, **_: Any) -> None:
        """Silence a sounding alarm — `stop` is installed for every siren family."""
        self._require_siren_actions()
        client = self.client
        await client.action(self._sn, "stop")
