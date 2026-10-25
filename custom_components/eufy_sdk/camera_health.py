"""Read-only HA presentation of the validated camera-health projection."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from time import monotonic
from typing import TYPE_CHECKING, Any

from homeassistant.const import EntityCategory

from .api import camera_health
from .entity import EufySdkDeviceEntity

if TYPE_CHECKING:
    from .coordinator import EufySdkDataUpdateCoordinator

# Receipt bookkeeping that changes on nearly every SDK revision. When only these change,
# attributes refresh at most once per interval, so HA does not record a new state row
# every few seconds. Any other change still publishes immediately.
ROUTINE_HEALTH_FIELDS = frozenset(
    {"revision", "lastHubReportMs", "lastCloudParamMs", "rssi"}
)
ROUTINE_RAW_STATUS_FIELDS = frozenset({"observedMs"})
ROUTINE_REFRESH_SECONDS = 900


def _meaningful(health: dict) -> dict:
    """Return the projection without routine receipt bookkeeping."""
    result = {
        key: value for key, value in health.items() if key not in ROUTINE_HEALTH_FIELDS
    }
    result["rawStatus"] = {
        key: value
        for key, value in health["rawStatus"].items()
        if key not in ROUTINE_RAW_STATUS_FIELDS
    }
    return result


def _utc(value: int | None) -> str | None:
    """Format receipt timestamps without adding clock-driven attributes."""
    return (
        None if value is None else datetime.fromtimestamp(value / 1000, UTC).isoformat()
    )


class EufyCameraHealthEntity(EufySdkDeviceEntity):
    """Share bridge-only availability and a stable diagnostic attribute projection."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: EufySdkDataUpdateCoordinator, sn: str, key: str
    ) -> None:
        """Bind a diagnostic to the existing camera device."""
        super().__init__(coordinator, sn)
        self._attr_unique_id = f"{sn}_{key}"
        self._health: dict | None = None
        self._health_attributes: dict[str, Any] = {}
        self._health_built_at = 0.0

    async def async_added_to_hass(self) -> None:
        """Publish diagnostic unavailability even when no coordinator update follows."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.client.add_disconnect_listener(self.async_write_ha_state)
        )

    @property
    def available(self) -> bool:
        """Use bridge usability alone, independently of child connectivity."""
        return (
            self.coordinator.last_update_success
            and self.client.connected
            and self._sn in (self.coordinator.data or {})
        )

    @property
    def camera_health(self) -> dict | None:
        """Return the coordinator's validated projection."""
        return self.device.get("cameraHealth")

    def health_attributes(self) -> dict[str, Any]:
        """Rebuild on meaningful changes; routine-only changes at most per interval."""
        health = self.camera_health
        if health == self._health:
            return self._health_attributes
        if (
            health is not None
            and self._health is not None
            and _meaningful(health) == _meaningful(self._health)
            and monotonic() - self._health_built_at < ROUTINE_REFRESH_SECONDS
        ):
            return self._health_attributes
        self._health = deepcopy(health)
        self._health_built_at = monotonic()
        if health is None:
            self._health_attributes = {}
            return self._health_attributes
        outcome = health["lastOutcome"]
        self._health_attributes = {
            "basis": health["basis"],
            "evidence": health["evidence"],
            "since": _utc(health["sinceMs"]),
            "last_contact": _utc(health["lastContactMs"]),
            "last_contact_source": health["lastContactSource"],
            "last_hub_offline_reply": _utc(health["lastHubOfflineMs"]),
            "hub_offline_reply_count": health["hubOfflineCount"],
            "last_hub_report": _utc(health["lastHubReportMs"]),
            "last_cloud_param": _utc(health["lastCloudParamMs"]),
            "rssi": health["rssi"]["value"],
            "rssi_valid": health["rssi"]["valid"],
            "rssi_last_valid": _utc(health["rssi"]["lastValidMs"]),
            "raw_status_1131": health["rawStatus"]["p1131"],
            "raw_status_1182": health["rawStatus"]["p1182"],
            "parent_transport": health["parentTransport"],
            "enforcement": health["enforcement"],
            "commands_blocked": health["commandsBlocked"],
            "outcomes": deepcopy(health["outcomes"]),
            "last_outcome": None
            if outcome is None
            else {**outcome, "atMs": _utc(outcome["atMs"])},
            "revision": health["revision"],
        }
        return self._health_attributes


def camera_health_serials(coordinator: EufySdkDataUpdateCoordinator) -> list[str]:
    """Create diagnostics only for snapshots carrying the valid optional contract."""
    return [
        sn
        for sn, device in coordinator.data.items()
        if camera_health(device.get("cameraHealth")) is not None
    ]
