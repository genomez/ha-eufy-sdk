"""Read privacy reports for HomeBase S1 cameras."""

from __future__ import annotations

from time import time
from typing import TYPE_CHECKING, Any

from .light_schedule import t9000_attached

PRIVACY_REPORT_KEY = "_privacy_report"

if TYPE_CHECKING:
    from .coordinator import EufySdkDataUpdateCoordinator


def is_attached_camera(device: dict[str, Any]) -> bool:
    """Identify a camera with an explicit parent, even without the parent row."""
    capabilities = device.get("capabilities") or ()
    return (
        not device.get("error")
        and bool(device.get("stationSn"))
        and device["stationSn"] != device.get("sn")
        and (
            device.get("codec") == "camera"
            or "camera" in capabilities
            or "video" in capabilities
        )
    )


def is_homebase_s1_camera(
    coordinator: EufySdkDataUpdateCoordinator, serial: str
) -> bool:
    """Require a known HomeBase S1 parent for camera privacy."""
    return is_attached_camera(coordinator.data.get(serial, {})) and (
        t9000_attached(coordinator, serial) is True
    )


def is_s1_privacy_camera(
    coordinator: EufySdkDataUpdateCoordinator, serial: str
) -> bool:
    """Identify the camera and station models with a confirmed privacy control."""
    return (
        is_homebase_s1_camera(coordinator, serial)
        and coordinator.data[serial].get("model") == "T8416"
        and coordinator.data[coordinator.data[serial]["stationSn"]].get("model")
        == "T9000"
    )


def offers_s1_privacy(
    coordinator: EufySdkDataUpdateCoordinator, serial: str, spec: dict[str, Any]
) -> bool:
    """Require the bridge to advertise a writable, readable privacy boolean."""
    return (
        is_s1_privacy_camera(coordinator, serial)
        and spec.get("name") == "privacy"
        and spec.get("type") == "bool"
        and spec.get("writable") is True
        and not spec.get("writeOnly")
        and not spec.get("unexposed")
    )


def privacy_report(device: dict[str, Any]) -> tuple[bool, int] | None:
    """Pair a decoded privacy boolean with its SDK receipt."""
    state = device.get("state")
    evidence = device.get("stateEvidence")
    if not isinstance(state, dict) or not isinstance(evidence, dict):
        return None
    return privacy_value_receipt(state.get("privacy"), evidence.get("privacy"))


def privacy_value_receipt(value: Any, evidence: Any) -> tuple[bool, int] | None:
    """Validate a privacy value and its SDK receipt."""
    # The SDK projects these sources only when the sampled 6250 value matches
    # the model. An uncertain receipt can lack producer order or read correlation.
    if (
        type(value) is not bool
        or not isinstance(evidence, dict)
        or evidence.get("version") != 1
        or evidence.get("source") not in ("cloud_poll", "rtc_push", "rtc_readback")
        or evidence.get("status") not in ("current", "uncertain")
        or evidence.get("reason") == "producer_conflict"
    ):
        return None
    receipt = evidence.get("receivedAtMs")
    return (value, receipt) if type(receipt) is int and receipt > 0 else None


def _allows_privacy_retention(device: dict[str, Any]) -> bool:
    evidence = device.get("stateEvidence", {})
    if not isinstance(evidence, dict):
        return False
    if "privacy" not in evidence:
        return True
    observation = evidence["privacy"]
    return (
        isinstance(observation, dict)
        and observation.get("version") == 1
        and observation.get("source") == "snapshot"
        and observation.get("receivedAtMs") is None
        and observation.get("status") in ("current", "uncertain")
        and observation.get("reason") != "producer_conflict"
    )


def retain_privacy_report(device: dict[str, Any], previous: dict[str, Any]) -> None:
    """Keep the newest matching receipt without changing its expiry."""
    report = previous.get(PRIVACY_REPORT_KEY)
    if (
        report is None
        or PRIVACY_REPORT_KEY not in device
        or device.get("stationSn") != previous.get("stationSn")
        or device["state"].get("privacy") is not report[0]
        or previous.get("state", {}).get("privacy") is not report[0]
        # A receipt stamped ahead of this clock must not outlive a later snapshot.
        or report[1] > time() * 1000
    ):
        return
    incoming = device[PRIVACY_REPORT_KEY]
    if incoming is None:
        # Untimed snapshots can confirm a match without supplying a new receipt.
        if not _allows_privacy_retention(device):
            return
    elif incoming[1] >= report[1]:
        return
    device[PRIVACY_REPORT_KEY] = report
