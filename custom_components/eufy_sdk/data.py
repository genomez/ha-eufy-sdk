"""Custom types for eufy_sdk."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.loader import Integration

    from .api import EufySdkApiClient
    from .coordinator import EufySdkDataUpdateCoordinator


type EufySdkConfigEntry = ConfigEntry[EufySdkData]


@dataclass
class EufySdkData:
    """Data for the EufySdk integration."""

    client: EufySdkApiClient
    coordinator: EufySdkDataUpdateCoordinator
    integration: Integration
    # Per-device property manifests ({sn: [spec, …]}), fetched once at setup.
    properties: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    # Last locally selected preset wire index ({sn: slot}); not camera position.
    selected_preset: dict[str, int] = field(default_factory=dict)
    # Slots as each camera last reported them ({sn: [PresetSlot, …]}), read over P2P so
    # only present once the camera has been awake to answer. Empty means "not asked
    # yet", never "no presets" — see presets.py.
    preset_slots: dict[str, list[Any]] = field(default_factory=dict)
    # Local snapshot choice per camera; restored by the select platform.
    snapshot_policy: dict[str, str] = field(default_factory=dict)
    # Full entry snapshots distinguish live safety changes from reload options.
    options_snapshot: dict[str, Any] = field(default_factory=dict)
    entry_data_snapshot: dict[str, Any] = field(default_factory=dict)
