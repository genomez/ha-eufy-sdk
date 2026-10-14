"""
The mode a HomeBase is actually IN, as opposed to the one it was SET to.

`armingMode` (param 1224) is the guard mode a user chose. Two of its values are not
modes at all but rules: `schedule` (2) hands the hub a weekly timetable and `geo` (47)
hands it to presence. The mode the hub then enforces — the one the eufy app shows as
"current mode" and the old integration exposed as `current_mode` — is a different
number, and it is what an automation or a dashboard usually wants.

Explicit currentMode observations take priority. Schedule and geofence require a
reported effective mode: HA's timezone and presence are not proof of the station's
schedule or location. The pure schedule helper is retained for source comparison,
but is not used to present an inferred device state.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from datetime import datetime


def _as_int(raw: Any) -> int | None:
    """Read a wire integer that may arrive as int or numeric string (never a bool)."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        try:
            return int(raw)
        except ValueError:
            return None
    return None


MODE_SCHEDULE = 2
MODE_GEO = 47

# The SDK's own labels for `armingMode`, so the sensor reads exactly like the select.
MODE_LABELS: dict[int, str] = {
    0: "away",
    1: "home",
    2: "schedule",
    3: "custom1",
    4: "custom2",
    5: "custom3",
    6: "off",
    47: "geo",
    63: "disarmed",
}

SOURCE_PUSH = "reported"
SOURCE_SCHEDULE = "schedule"
SOURCE_SET = "set"

# Minutes in a day; a slot written as ending 23:59 really runs to midnight.
_END_OF_DAY = 24 * 60


def mode_label(raw: Any) -> str | None:
    """Return the SDK label for a wire mode, or None for an unknown one."""
    value = _as_int(raw)
    return None if value is None else MODE_LABELS.get(value)


def _eufy_weekday(at: datetime) -> int:
    """Python counts Monday = 0; the hub's timetable counts Sunday = 0."""
    return (at.weekday() + 1) % 7


def resolve_schedule(schedule: Any, at: datetime) -> int | None:
    """
    Return the `mode_id` the timetable prescribes at `at`, or None if no slot covers it.

    Slots are `{week, start_h, start_m, end_h, end_m, mode_id}`. A slot's end is the
    next slot's start, so it is exclusive — at 20:20 a hub whose slots meet there is
    already in the later one (watched live on a T8030) — except that a day's last slot
    is written as ending 23:59 and has to cover that minute too.
    """
    if not isinstance(schedule, dict):
        return None
    slots = schedule.get("schedules")
    if not isinstance(slots, list):
        return None
    weekday = _eufy_weekday(at)
    minute = at.hour * 60 + at.minute
    for slot in slots:
        if not isinstance(slot, dict) or _as_int(slot.get("week")) != weekday:
            continue
        start = _as_int(slot.get("start_h"))
        end = _as_int(slot.get("end_h"))
        mode = _as_int(slot.get("mode_id"))
        if start is None or end is None or mode is None:
            continue
        start_min = start * 60 + (_as_int(slot.get("start_m")) or 0)
        end_min = end * 60 + (_as_int(slot.get("end_m")) or 0)
        if end_min == _END_OF_DAY - 1:
            end_min = _END_OF_DAY
        if start_min <= minute < end_min:
            return mode
    return None


def current_mode_for(state: dict[str, Any], at: datetime) -> tuple[int | None, str]:
    """
    Return the mode the hub is enforcing, and where that answer came from.

    Use an explicit report first, with no timezone or geofence inference.

    A direct guard mode is a fallback; Schedule/Geofence remain unknown until the
    station reports its effective mode. This does not establish report freshness.
    """
    del at  # No station-timezone assumption.
    pushed = _as_int(state.get("currentMode"))
    if pushed is not None:
        return pushed, SOURCE_PUSH
    set_mode = _as_int(state.get("armingMode"))
    if set_mode in (MODE_SCHEDULE, MODE_GEO):
        return None, "unknown"
    return set_mode, SOURCE_SET
