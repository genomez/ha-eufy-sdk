"""
Stored presets — reading them from the camera, and naming them.

Presets are the saved positions; moving the camera by hand is the PTZ d-pad, and
lives in the button platform. The slots belong to the camera, not to us: how many
there are and how they are numbered varies by model, and only the camera knows
which ones hold a position. The select reads options over P2P when supported and
moves to the selected position; reading and labelling live here.

Reading is a P2P request/reply, so it answers only while the camera is awake. A
sleeping battery camera times out instead of returning an empty list, which is
why every read here is best-effort and callers fall back to four unverified slots.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .const import LOGGER, PRESET_SLOTS

if TYPE_CHECKING:
    from .api import EufySdkApiClient
    from .data import EufySdkConfigEntry

# Moving to a stored position is `preset.preview`, NOT `preset.goto`. Verified on
# hardware: `goto` (wire 6032) reports success and the camera does not move. The
# SDK's own docs explain why — that frame is byte-identical to the save frame, and
# the camera reads it as "store here". `preview` (6035) is a distinct command, the
# one the app uses to swing the camera onto a preset, and it does move it.
# Saving is deliberately absent: presets are created in the eufy app, where you can
# frame the shot while watching it. The SDK does expose a save verb, but it was never
# seen to work here, and a control that quietly does nothing is worse than none.
ACTION_GOTO = "preset.preview"


@dataclass(frozen=True)
class PresetSlot:
    """One stored-position slot, as the camera reports it."""

    index: int
    occupied: bool | None
    is_default: bool
    reported: bool = False

    @property
    def label(self) -> str:
        """Render the plain one-based app number, leaving wire indices unchanged."""
        return str(self.index + 1)


def fallback_slots() -> list[PresetSlot]:
    """
    Build bare numbered slots, for when the camera could not be asked.

    Occupancy is unknown: numbered choices are not evidence of saved positions.
    """
    return [
        PresetSlot(i, occupied=None, is_default=i == 0) for i in range(PRESET_SLOTS)
    ]


def parse_slots(result: Any) -> list[PresetSlot]:
    """
    Turn a `preset.list` reply into slots.

    Each entry carries the SDK's normalised `id` plus the camera's own `raw`
    reply, whose fields vary by model — commonly `index`, `enable` and
    `isdefault`. A returned slot is confirmed even without an occupancy flag.
    """
    slots: list[PresetSlot] = []
    for entry in result or []:
        raw = entry.get("raw") or {}
        index = entry.get("id", raw.get("index"))
        if index is None:
            continue
        slots.append(
            PresetSlot(
                index=int(index),
                occupied=(raw["enable"] != 0 if "enable" in raw else None),
                is_default=int(index) == 0 or bool(raw.get("isdefault", 0)),
                reported=True,
            )
        )
    return sorted(slots, key=lambda s: s.index)


def list_supported(device: dict[str, Any]) -> bool:
    """Whether the bound SDK manifest advertises the preset read method."""
    manifest = device.get("decodedProperties") or {}
    if manifest.get("bound") is not True:
        return False
    return _advertises_action(manifest, "preset.list")


def movement_supported(device: dict[str, Any]) -> bool:
    """Require preset movement in bound manifests; retain older PTZ fallbacks."""
    if "ptz" not in device.get("capabilities", []):
        return False
    manifest = device.get("decodedProperties") or {}
    if manifest.get("bound") is not True:
        return True
    return _advertises_action(manifest, ACTION_GOTO)


def _advertises_action(manifest: dict[str, Any], name: str) -> bool:
    """Find an action on the bound PTZ surface."""
    for capability in manifest.get("details", []):
        if capability.get("accessor") != "ptz":
            continue
        return name in capability.get("undescribedActions", []) or any(
            action.get("name") == name for action in capability.get("actions", [])
        )
    return False


async def async_read_slots(
    client: EufySdkApiClient, sn: str
) -> list[PresetSlot] | None:
    """Ask the camera for its slots. None when it could not answer."""
    try:
        return parse_slots(await client.preset_slots(sn))
    except Exception:  # noqa: BLE001 — any failure means "ask again later"
        LOGGER.debug("could not read presets [presets_unavailable]")
        return None


async def async_refresh_slots(entry: EufySdkConfigEntry, sn: str) -> list[PresetSlot]:
    """Re-read the slots into runtime data, keeping what we had on failure."""
    runtime = entry.runtime_data
    slots = await async_read_slots(runtime.client, sn)
    if slots is not None:
        runtime.preset_slots[sn] = slots
    return runtime.preset_slots.get(sn) or fallback_slots()


def slots_for(entry: EufySdkConfigEntry, sn: str) -> list[PresetSlot]:
    """Return the slots known for this camera, or bare numbers until a read lands."""
    return entry.runtime_data.preset_slots.get(sn) or fallback_slots()


def remember(entry: EufySdkConfigEntry, sn: str, slots: list[PresetSlot]) -> None:
    """Record slots restored from a previous run, so a cold start is not blind."""
    entry.runtime_data.preset_slots[sn] = slots


def as_dicts(slots: list[PresetSlot]) -> list[dict[str, Any]]:
    """Render slots for the entity state, so a restart can restore them."""
    return [
        {
            "index": s.index,
            "occupied": s.occupied,
            "default": s.is_default,
            "observed": s.occupied is not None,
            "reported": s.reported,
        }
        for s in slots
    ]


def from_dicts(raw: Any) -> list[PresetSlot]:
    """Restore reported/observed choices, discarding old synthetic fallback lists."""
    if not isinstance(raw, list):
        return []
    slots = []
    for item in raw:
        if not isinstance(item, dict) or "index" not in item:
            continue
        if not (
            item.get("reported") is True
            or (item.get("observed") is True and isinstance(item.get("occupied"), bool))
            or item.get("occupied") is False
        ):
            continue
        slots.append(
            PresetSlot(
                index=int(item["index"]),
                occupied=(
                    item["occupied"]
                    if isinstance(item.get("occupied"), bool)
                    and (item.get("observed") is True or item["occupied"] is False)
                    else None
                ),
                is_default=bool(item.get("default", False)),
                reported=item.get("reported") is True,
            )
        )
    return slots
