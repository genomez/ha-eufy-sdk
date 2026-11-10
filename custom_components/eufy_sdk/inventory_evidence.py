"""Inventory freshness is independent of property state and transport health."""

from __future__ import annotations

from typing import Any


def count(raw: Any) -> bool:
    """Accept bounded nonnegative integers, excluding booleans."""
    return type(raw) is int and 0 <= raw <= 2**53 - 1


def inventory_row(raw: Any) -> dict | None:
    """Preserve the original successful observation time of retained rows."""
    if (
        not isinstance(raw, dict)
        or not isinstance(raw.get("quality"), str)
        or raw.get("quality") not in {"fresh", "retained"}
        or "lastSuccessfulObservationMs" not in raw
        or (
            raw["quality"] == "fresh"
            and not count(raw.get("lastSuccessfulObservationMs"))
        )
        or not (
            raw.get("lastSuccessfulObservationMs") is None
            or count(raw["lastSuccessfulObservationMs"])
        )
    ):
        return None
    return {
        "quality": raw["quality"],
        "lastSuccessfulObservationMs": raw.get("lastSuccessfulObservationMs"),
    }


def inventory_evidence(raw: Any, devices: list[dict]) -> dict | None:
    """Malformed or absent evidence degrades to unknown, never fresh."""
    if (
        not isinstance(raw, dict)
        or type(raw.get("version")) is not int
        or raw["version"] != 1
        or not all(
            count(raw.get(key))
            for key in ("generation", "completedAtMs", "attempted", "failed")
        )
        or raw["generation"] < 1
        or raw["failed"] > raw["attempted"]
        or not isinstance(raw.get("quality"), str)
        or raw.get("quality") not in {"complete", "partial", "retained"}
        or not (
            raw.get("lastCompleteSuccessMs") is None
            or count(raw["lastCompleteSuccessMs"])
        )
    ):
        return None
    rows = raw.get("rows")
    return {
        **{
            key: raw.get(key)
            for key in (
                "version",
                "generation",
                "completedAtMs",
                "quality",
                "attempted",
                "failed",
                "lastCompleteSuccessMs",
            )
        },
        "rows": {
            device["sn"]: row
            for device in devices
            if isinstance(device.get("sn"), str)
            and (
                row := inventory_row(
                    rows.get(device["sn"]) if isinstance(rows, dict) else None
                )
            )
            is not None
        },
    }


def inventory_attributes(evidence: Any, serial: str, now_ms: int) -> dict:
    """Clock rollback yields unknown age and cannot make cached data fresh."""
    if not isinstance(evidence, dict) or not isinstance(evidence.get("rows"), dict):
        return {"inventory_quality": "unknown", "inventory_age_seconds": None}
    row = inventory_row(evidence.get("rows", {}).get(serial))
    at = row.get("lastSuccessfulObservationMs") if row else None
    age = (now_ms - at) / 1000 if count(at) and now_ms >= at else None
    return {
        "inventory_quality": row["quality"] if row else "unknown",
        "inventory_age_seconds": age,
        "inventory_refresh_quality": evidence.get("quality", "unknown"),
        "inventory_last_success_ms": at,
    }
