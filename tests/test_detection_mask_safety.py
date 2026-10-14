"""Exercise detection-mask safety through the entity's actual RPC write path."""

# ruff: noqa: INP001, S101, SLF001

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from custom_components.eufy_sdk.property_sync import apply_property_event
from custom_components.eufy_sdk.switch import EufyBitmaskSwitch
from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError

BASE = 0x30000
BIT = 4
INVALID_REPORTS = [
    {"ai_detect_type": BASE | BIT, "private": "synthetic-private-marker"},
    [BASE, BIT],
    '{"private":"synthetic-private-marker","ai_detect_type":196612}',
    "synthetic-private-marker",
    "",
    "--4",
    "²",
    "4.0",
    "NaN",
    "Infinity",
    "9" * 5000,
    float("nan"),
    float("inf"),
    float("-inf"),
]
VALID_REPORTS = [
    0,
    4,
    0x8003,
    0x8007,
    0x30003,
    0x30007,
    (1 << 60) | 7,
    True,
    False,
    32775.9,
    -1,
    -1.9,
    "0",
    "32775",
    " 32775 ",
    "-1",
    "٤",
]


def entity_for(value: object) -> tuple[EufyBitmaskSwitch, SimpleNamespace, Mock]:
    """Bind the real entity and write machinery to synthetic bridge I/O."""
    client = Mock(set_property=AsyncMock())
    coordinator = SimpleNamespace(
        data={"synthetic-camera": {"state": {"aiDetectType": value}}},
        config_entry=SimpleNamespace(runtime_data=SimpleNamespace(client=client)),
        snapshot_generation=0,
        completed_snapshot_generation=0,
        last_update_success=True,
        async_request_refresh=AsyncMock(),
        async_update_listeners=Mock(),
    )
    entity = EufyBitmaskSwitch(
        coordinator,
        "synthetic-camera",
        {"name": "aiDetectType", "type": "number", "kind": "bitfield"},
        {"key": "detect_human", "bit": BIT, "base": BASE},
    )
    entity.hass = Mock()
    entity.async_write_ha_state = Mock()
    return entity, coordinator, client


def report(entity: EufyBitmaskSwitch, value: object) -> None:
    """Deliver an accepted scalar report through the existing reconciliation path."""
    data = {
        "event": "propertyChanged",
        "deviceSn": "synthetic-camera",
        "property": "aiDetectType",
        "value": value,
    }
    assert apply_property_event(entity.coordinator, data)
    entity._handle_property_changed(SimpleNamespace(data=data))


@pytest.mark.parametrize("value", INVALID_REPORTS, ids=range(len(INVALID_REPORTS)))
def test_nonmask_report_reads_unknown(value: object) -> None:
    """Unsupported reports cannot crash the detection entity's state read."""
    entity, _, _ = entity_for(value)
    assert entity.is_on is None


@pytest.mark.parametrize("value", INVALID_REPORTS, ids=range(len(INVALID_REPORTS)))
@pytest.mark.parametrize("turn_on", [True, False])
async def test_nonmask_report_is_unknown_and_never_written(
    value: object,
    *,
    turn_on: bool,
) -> None:
    """Unsupported shapes cannot change the cache, owner, timer, or RPC count."""
    entity, coordinator, client = entity_for(value)
    with (
        patch("custom_components.eufy_sdk.entity.async_call_later") as timer,
        pytest.raises(HomeAssistantError) as error,
    ):
        await (entity.async_turn_on() if turn_on else entity.async_turn_off())
    assert str(error.value) == (
        "Detection setting is not a bitmask; refusing to overwrite it"
    )
    assert error.value.__cause__ is None
    client.set_property.assert_not_awaited()
    timer.assert_not_called()
    entity.async_write_ha_state.assert_not_called()
    assert coordinator.data["synthetic-camera"]["state"]["aiDetectType"] is value
    assert entity._assumed_value is None
    assert entity._write_generation == 0
    assert entity._report_generation == 0


@pytest.mark.parametrize("value", VALID_REPORTS, ids=range(len(VALID_REPORTS)))
@pytest.mark.parametrize("turn_on", [True, False])
async def test_numeric_reports_preserve_unselected_bits_through_rpc(
    value: object,
    *,
    turn_on: bool,
) -> None:
    """Successful writes retain identity, ownership, timers and reported cache."""
    entity, coordinator, client = entity_for(value)
    mask = int(value)
    expected = mask | BIT if turn_on else mask & ~BIT
    assert entity.is_on is bool(mask & BIT)
    with patch(
        "custom_components.eufy_sdk.entity.async_call_later",
        return_value=Mock(),
    ) as timer:
        await (entity.async_turn_on() if turn_on else entity.async_turn_off())
    client.set_property.assert_awaited_once_with(
        "synthetic-camera",
        "aiDetectType",
        expected,
    )
    timer.assert_called_once()
    assert entity.prop_value == expected
    assert entity.is_on is turn_on
    assert entity._optimistic_owner == entity._write_generation == 1
    assert coordinator.data["synthetic-camera"]["state"]["aiDetectType"] is value
    assert entity.unique_id == "synthetic-camera_aiDetectType_4"
    assert entity.device_info["identifiers"] == {("eufy_sdk", "synthetic-camera")}
    assert entity.translation_key == "detect_human"
    assert entity.entity_category == EntityCategory.CONFIG


@pytest.mark.parametrize("turn_on", [True, False])
async def test_absent_report_retains_enable_base_fallback(*, turn_on: bool) -> None:
    """An absent report stays unknown until the successful fallback write."""
    entity, _, client = entity_for(None)
    assert entity.is_on is None
    with patch("custom_components.eufy_sdk.entity.async_call_later"):
        await (entity.async_turn_on() if turn_on else entity.async_turn_off())
    client.set_property.assert_awaited_once_with(
        "synthetic-camera",
        "aiDetectType",
        BASE | BIT if turn_on else BASE,
    )
    assert entity.is_on is turn_on


async def test_failed_rpc_does_not_optimistically_change_detection_state() -> None:
    """A rejected valid write preserves the previous property and has no timer."""
    entity, _, client = entity_for(0x8003)
    client.set_property.side_effect = HomeAssistantError("synthetic RPC failure")
    with (
        patch("custom_components.eufy_sdk.entity.async_call_later") as timer,
        pytest.raises(HomeAssistantError, match="synthetic RPC failure"),
    ):
        await entity.async_turn_on()
    assert entity.is_on is False
    assert entity._assumed_value is None
    timer.assert_not_called()


async def test_report_during_rpc_retains_report_generation_ownership() -> None:
    """The successful scalar write must not obscure a newer scalar report."""
    entity, _, client = entity_for("32775")
    gate = asyncio.Event()

    async def complete_after_report(*_args: object) -> None:
        await gate.wait()

    client.set_property.side_effect = complete_after_report
    with patch("custom_components.eufy_sdk.entity.async_call_later"):
        task = asyncio.create_task(entity.async_turn_off())
        await asyncio.sleep(0)
        report(entity, "262151")
        gate.set()
        await task
    client.set_property.assert_awaited_once_with(
        "synthetic-camera",
        "aiDetectType",
        32771,
    )
    assert entity._report_generation == 1
    assert entity._assumed_value is None
    assert entity.prop_value == "262151"
    assert entity.is_on is True


async def test_nonmask_scalar_report_releases_successful_write_hold() -> None:
    """A later JSON-string report becomes unknown and prevents another write."""
    entity, _, client = entity_for(0x8003)
    with patch(
        "custom_components.eufy_sdk.entity.async_call_later",
        return_value=Mock(),
    ):
        await entity.async_turn_on()
    report(entity, INVALID_REPORTS[2])
    assert entity._assumed_value is None
    assert entity._report_generation == 1
    assert entity.is_on is None
    with pytest.raises(HomeAssistantError):
        await entity.async_turn_off()
    assert client.set_property.await_count == 1


async def test_new_report_other_bits_survive_following_toggle() -> None:
    """Toggle from the latest report after releasing the old optimistic hold."""
    entity, _, client = entity_for(0x8007)
    with patch("custom_components.eufy_sdk.entity.async_call_later"):
        await entity.async_turn_off()
        report(entity, "262147")
        await entity.async_turn_on()
    assert [call.args[2] for call in client.set_property.await_args_list] == [
        0x8003,
        0x40007,
    ]
