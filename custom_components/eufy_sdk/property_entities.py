"""Reversible, narrowly scoped cleanup of superseded property registry entries."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .entity import classify

if TYPE_CHECKING:
    from homeassistant.core import Event, HomeAssistant

    from .data import EufySdkConfigEntry

# Only reviewed schema corrections belong here. Missing arbitrary properties,
# absent devices and transient readings never establish that an entity is obsolete.
_CORRECTED_PROPERTIES = frozenset(
    {
        "workingMode",
        "recordDuration",
        "recordInterval",
        "recordAutoStop",
        "battery",
        "batteryHealth",
        "batteryTemperature",
        "batteryPowerStats",
        "solarIntensity",
        "solarConnected24h",
        "charging",
        "testMode",
        "sensorPirSensitivity",
        "locked",
    }
)
_PROPERTY_PLATFORMS = ("sensor", "binary_sensor", "switch", "select", "number")
_V1_MARKER = "property_presentation_v1"
_MARKER = "property_presentation_v2"
_MANAGERS = f"{DOMAIN}_property_presentation_managers"


def _marker(entry: er.RegistryEntry, key: str = _MARKER) -> dict[str, Any]:
    value = entry.options.get(DOMAIN, {}).get(key, {})
    return value if isinstance(value, dict) else {}


def _write_marker(
    registry: er.EntityRegistry, entry: er.RegistryEntry, value: dict | None
) -> None:
    options = dict(entry.options.get(DOMAIN, {}))
    options.pop(_V1_MARKER, None)
    if value is None:
        options.pop(_MARKER, None)
    else:
        options[_MARKER] = value
    registry.async_update_entity_options(entry.entity_id, DOMAIN, options or None)


class _PresentationManager:
    """Track our queued registry mutations separately from explicit user choices."""

    def __init__(self, registry: er.EntityRegistry, entry_id: str) -> None:
        """Keep only in-memory pending mutation receipts; registry owns persistence."""
        self.registry = registry
        self.entry_id = entry_id
        self.pending: dict[str, list[dict[str, Any]]] = {}

    def update(self, entity: er.RegistryEntry, **fields: Any) -> None:
        """Queue the exact old-value signature before a registry update fires."""
        changed = {
            key: value for key, value in fields.items() if getattr(entity, key) != value
        }
        if not changed:
            return
        self.pending.setdefault(entity.entity_id, []).append(
            {key: getattr(entity, key) for key in changed}
        )
        self.registry.async_update_entity(entity.entity_id, **changed)

    def release(self, entity: er.RegistryEntry, *, opt_out: bool = False) -> None:
        """Undo only owned controls, preserving a user's replacement preferences."""
        marker, old_marker = _marker(entity), _marker(entity, _V1_MARKER)
        changes: dict[str, Any] = {}
        if (
            marker.get("owned_disabled") is True
            and entity.disabled_by == er.RegistryEntryDisabler.INTEGRATION
        ):
            changes["disabled_by"] = None
        if (
            old_marker.get("owned") is True
            and entity.hidden_by == er.RegistryEntryHider.INTEGRATION
        ):
            changes["hidden_by"] = None
        _write_marker(self.registry, entity, {"opt_out": True} if opt_out else None)
        self.update(entity, **changes)

    def retire(self, entity: er.RegistryEntry) -> None:
        """Disable a proven obsolete entry, migrating only our prior owned hide."""
        marker, old_marker = _marker(entity), _marker(entity, _V1_MARKER)
        if marker.get("opt_out") is True:
            return
        if marker.get("owned_disabled") is True:
            if entity.disabled_by != er.RegistryEntryDisabler.INTEGRATION:
                _write_marker(self.registry, entity, {"opt_out": True})
            return
        if old_marker:
            if (
                old_marker.get("opt_out") is True
                or entity.hidden_by != er.RegistryEntryHider.INTEGRATION
                or old_marker.get("owned") is not True
                or entity.disabled_by is not None
            ):
                self.release(entity, opt_out=True)
                return
            _write_marker(self.registry, entity, {"owned_disabled": True})
            self.update(
                entity, hidden_by=None, disabled_by=er.RegistryEntryDisabler.INTEGRATION
            )
            return
        if entity.hidden_by is not None or entity.disabled_by is not None:
            return
        _write_marker(self.registry, entity, {"owned_disabled": True})
        self.update(entity, disabled_by=er.RegistryEntryDisabler.INTEGRATION)

    @callback
    def on_update(self, event: Event) -> None:
        """Honor explicit enable/unhide opt-outs without clearing user disables."""
        if event.data.get("action") != "update":
            return
        entity_id = event.data["entity_id"]
        changed = {
            key: value
            for key, value in event.data.get("changes", {}).items()
            if key in ("hidden_by", "disabled_by")
        }
        if not changed:
            return
        queued = self.pending.get(entity_id, [])
        if queued and queued[0] == changed:
            queued.pop(0)
            if not queued:
                self.pending.pop(entity_id, None)
            return
        entity = self.registry.async_get(entity_id)
        if (
            entity is None
            or entity.config_entry_id != self.entry_id
            or entity.platform != DOMAIN
        ):
            return
        if _marker(entity).get("owned_disabled") is not True:
            return
        changed_disable = (
            "disabled_by" in changed
            and entity.disabled_by != er.RegistryEntryDisabler.INTEGRATION
        )
        explicit_unhide = (
            "hidden_by" in changed
            and changed["hidden_by"] is not None
            and entity.hidden_by is None
        )
        if changed_disable or explicit_unhide:
            self.release(entity, opt_out=True)


def _manager(hass: HomeAssistant, entry: EufySdkConfigEntry) -> _PresentationManager:
    managers = hass.data.setdefault(_MANAGERS, {})
    if entry.entry_id not in managers:
        manager = managers[entry.entry_id] = _PresentationManager(
            er.async_get(hass), entry.entry_id
        )
        unsubscribe = hass.bus.async_listen(
            er.EVENT_ENTITY_REGISTRY_UPDATED, manager.on_update
        )

        @callback
        def cleanup() -> None:
            unsubscribe()
            if managers.get(entry.entry_id) is manager:
                managers.pop(entry.entry_id)

        entry.async_on_unload(cleanup)
    return managers[entry.entry_id]


@callback
def async_release_presentation_hides(
    hass: HomeAssistant, entry_id: str, *, restore_v1: bool = False
) -> None:
    """Release owned controls; preserve opt-outs when returning to v1 source."""
    registry = er.async_get(hass)
    manager = hass.data.get(_MANAGERS, {}).get(entry_id) or _PresentationManager(
        registry, entry_id
    )
    for entity in er.async_entries_for_config_entry(registry, entry_id):
        if entity.platform == DOMAIN and (
            _marker(entity) or _marker(entity, _V1_MARKER)
        ):
            current, previous = _marker(entity), _marker(entity, _V1_MARKER)
            opted_out = (
                current.get("opt_out") is True
                or previous.get("opt_out") is True
                or (
                    current.get("owned_disabled") is True
                    and entity.disabled_by != er.RegistryEntryDisabler.INTEGRATION
                )
                or (
                    previous.get("owned") is True
                    and entity.hidden_by != er.RegistryEntryHider.INTEGRATION
                )
            )
            manager.release(entity)
            if restore_v1 and opted_out:
                released = registry.async_get(entity.entity_id)
                options = dict(released.options.get(DOMAIN, {}))
                options[_V1_MARKER] = {"opt_out": True}
                registry.async_update_entity_options(entity.entity_id, DOMAIN, options)


def _presentation_decisions(entry: EufySdkConfigEntry) -> dict[tuple[str, str], bool]:
    """Scope migration to explicit metadata and reviewed capability corrections."""
    decisions: dict[tuple[str, str], bool] = {}
    for serial, specs in entry.runtime_data.properties.items():
        device = entry.runtime_data.coordinator.data.get(serial, {})
        if device.get("error") or not specs:
            continue
        by_name = {
            spec["name"]: spec for spec in specs if isinstance(spec.get("name"), str)
        }
        names = _CORRECTED_PROPERTIES | set(by_name)
        for name in names:
            spec = by_name.get(name)
            obsolete = spec is None or spec.get("unexposed") is True
            # Raw alone is not a retirement signal: a newer SDK can expose a
            # decoded recording-quality property despite its structured wire value.
            for platform in _PROPERTY_PLATFORMS:
                decisions[(platform, f"{serial}_{name}")] = obsolete
        # snoozeTime was a raw string sensor before its typed seconds projection.
        # Retire only that proven old route, keeping the number's stable identity.
        snooze = by_name.get("snoozeTime")
        if snooze is not None and classify(snooze) == "number":
            decisions[("sensor", f"{serial}_snoozeTime")] = True
        # Older camera schemas inferred a lock from display names. The dedicated
        # lock entity does not use a property unique ID and needs its own scope.
        if device.get("codec") == "camera":
            decisions[("lock", f"{serial}_lock")] = "lock" not in device.get(
                "capabilities", []
            )

    return decisions


@callback
def async_reconcile_property_entities(
    hass: HomeAssistant, entry: EufySdkConfigEntry
) -> None:
    """Disable proven obsolete entries without deleting identities or preferences."""
    registry = er.async_get(hass)
    manager = _manager(hass, entry)
    decisions = _presentation_decisions(entry)
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if entity.platform != DOMAIN:
            continue
        obsolete = decisions.get((entity.domain, entity.unique_id))
        if obsolete is True:
            manager.retire(entity)
        elif obsolete is False and (_marker(entity) or _marker(entity, _V1_MARKER)):
            manager.release(entity)
