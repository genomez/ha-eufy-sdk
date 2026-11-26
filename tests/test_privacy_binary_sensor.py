"""Privacy switches and observations for HomeBase S1 cameras."""

# ruff: noqa: ANN001, ANN201, D103, INP001, S101, SLF001
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.helpers import entity_registry as er
from test_property_entities import Entry, Registry

from custom_components.eufy_sdk.api import EufySdkApiClientError
from custom_components.eufy_sdk.binary_sensor import async_setup_entry as setup_sensors
from custom_components.eufy_sdk.coordinator import EufySdkDataUpdateCoordinator
from custom_components.eufy_sdk.manifest_reads import normalize_snapshot
from custom_components.eufy_sdk.property_entities import (
    async_reconcile_property_entities,
)
from custom_components.eufy_sdk.property_sync import (
    apply_observation_event,
    apply_property_event,
)
from custom_components.eufy_sdk.switch import EufyPrivacySwitch
from custom_components.eufy_sdk.switch import async_setup_entry as setup_switches

RECEIPT_MS = 3_600_000
MAX_AGE_SECONDS = 1800


def snapshot(
    *,
    value=False,
    source="cloud_poll",
    stamp=RECEIPT_MS,
    status="uncertain",
    reason="no_sample_order",
    **device: Any,
):
    return normalize_snapshot(
        {
            "stationSn": "hub",
            "model": "T8416",
            "codec": "camera",
            "state": {"privacy": value},
            "stateEvidence": {
                "privacy": {
                    "version": 1,
                    "source": source,
                    "receivedAtMs": stamp,
                    "status": status,
                    "reason": reason,
                }
            },
            "decodedProperties": {"details": []},
            "decodedState": {},
            **device,
        }
    )


def config_entry(cameras, properties=None, *, snapshot_min=10, cloud_ms=600_000):
    coordinator = Mock(
        data={"hub": {"model": "T9000"}, **cameras},
        solix_devices={},
        update_interval=timedelta(minutes=snapshot_min),
        cloud_poll_interval_ms=cloud_ms,
    )
    return SimpleNamespace(
        entry_id="synthetic-entry",
        async_on_unload=Mock(),
        runtime_data=SimpleNamespace(
            coordinator=coordinator,
            properties=(
                properties
                if properties is not None
                else {
                    key: [{"name": "privacy", "type": "bool", "writable": True}]
                    for key in cameras
                }
            ),
        ),
    )


async def privacy_switches(entry):
    add = Mock()
    await setup_switches(Mock(), entry, add)
    return [
        entity
        for entity in add.call_args.args[0]
        if isinstance(entity, EufyPrivacySwitch)
    ]


def snapshot_coordinator(entry):
    runtime = entry.runtime_data
    previous = runtime.coordinator
    coordinator = EufySdkDataUpdateCoordinator.__new__(EufySdkDataUpdateCoordinator)
    coordinator.data = previous.data
    coordinator.solix_devices = previous.solix_devices
    coordinator.update_interval = previous.update_interval
    coordinator.cloud_poll_interval_ms = previous.cloud_poll_interval_ms
    coordinator.async_update_listeners = Mock()
    coordinator._clear_fast_retry = Mock()
    runtime.coordinator = coordinator
    return coordinator


async def refresh(coordinator, device, *events: dict[str, Any]):
    async def read() -> dict:
        for event in events:
            if event["event"] == "propertiesObserved":
                assert apply_observation_event(coordinator, event)
            else:
                assert apply_property_event(coordinator, event)
        return {"hub": {"model": "T9000"}, "first": device}

    coordinator._read_snapshot = read
    coordinator.data = await coordinator._async_refresh_snapshot()


def privacy_event(*, value):
    return {
        "event": "propertyChanged",
        "deviceSn": "first",
        "property": "privacy",
        "value": value,
    }


async def test_only_t8416_s1_cameras_get_the_station_privacy_switch():
    entry = config_entry(
        {
            "first": snapshot(value=True),
            "second": snapshot(value=False),
            "other_model": snapshot(value=True, model="T8425"),
            "doorbell": snapshot(
                value=False, model="T8214", codec="doorbell", capabilities=["video"]
            ),
            "standalone": snapshot(value=True, stationSn=None),
            "self_parent": snapshot(
                value=True, stationSn="self_parent", sn="self_parent"
            ),
            "other_parent": snapshot(value=True, stationSn="other_hub"),
            "unknown_parent": snapshot(value=True, stationSn="unknown_hub"),
            "other_hub": {"model": "T8030"},
            "hub_child": snapshot(value=True, model="T9000", codec="station"),
            "display": snapshot(value=True, model="T87A0", codec="display"),
            "missing_value": snapshot(state={}),
            "invalid_value": snapshot(value=1),
        },
    )
    with patch("custom_components.eufy_sdk.switch.time", return_value=3601):
        assert {
            entity.unique_id: entity.is_on for entity in await privacy_switches(entry)
        } == {
            "first_privacy": True,
            "second_privacy": False,
            "missing_value_privacy": None,
            "invalid_value_privacy": None,
        }


async def test_privacy_event_invalidates_the_old_pair_until_a_new_snapshot():
    entry = config_entry({"first": snapshot(value=False)})
    coordinator = entry.runtime_data.coordinator
    (entity,) = await privacy_switches(entry)
    with patch("custom_components.eufy_sdk.switch.time", return_value=3601):
        assert entity.is_on is False
        assert apply_property_event(coordinator, privacy_event(value=True))
        assert entity.is_on is None
        assert apply_property_event(coordinator, privacy_event(value=False))
        assert entity.is_on is None
        coordinator.data["first"] = snapshot(value=False, stamp=RECEIPT_MS + 500)
        assert entity.is_on is False
        coordinator.data["first"]["state"]["privacy"] = True
        assert entity.is_on is None


async def test_privacy_event_racing_a_snapshot_cannot_refresh_the_old_pair():
    entry = config_entry({"first": snapshot(value=False)})
    coordinator = snapshot_coordinator(entry)
    (entity,) = await privacy_switches(entry)
    # The final event may match the old snapshot after two changes.
    await refresh(
        coordinator, snapshot(), privacy_event(value=True), privacy_event(value=False)
    )
    with patch("custom_components.eufy_sdk.switch.time", return_value=3601):
        assert entity.is_on is None


async def test_pending_privacy_value_must_match_the_snapshot_before_overlay():
    entry = config_entry({"first": snapshot()})
    coordinator = snapshot_coordinator(entry)
    (entity,) = await privacy_switches(entry)
    await refresh(coordinator, snapshot(value=True), privacy_event(value=False))
    with patch("custom_components.eufy_sdk.switch.time", return_value=3601):
        assert entity.is_on is None
        await refresh(coordinator, snapshot(stateEvidence={}))
        assert entity.is_on is None


@pytest.mark.parametrize("source", ["rtc_readback", "rtc_push"])
async def test_timed_rtc_snapshot_is_a_privacy_observation(source):
    (entity,) = await privacy_switches(config_entry({"first": snapshot(source=source)}))
    with patch("custom_components.eufy_sdk.switch.time", return_value=3601):
        assert entity.is_on is False


async def test_privacy_keeps_the_newest_matching_receipt_until_its_original_expiry():
    entry = config_entry({"first": snapshot()})
    coordinator = snapshot_coordinator(entry)
    (entity,) = await privacy_switches(entry)
    with patch("custom_components.eufy_sdk.switch.time") as clock:
        clock.return_value = 3610
        await refresh(
            coordinator,
            snapshot(source="rtc_readback", stamp=3_610_000),
            privacy_event(value=False),
        )
        assert entity.is_on is False
        await refresh(coordinator, snapshot(stamp=3_605_000))
        clock.return_value = 3605 + MAX_AGE_SECONDS
        assert entity.is_on is False
        await refresh(coordinator, snapshot(stateEvidence={}))
        assert entity.is_on is False
        clock.return_value = 3610 + MAX_AGE_SECONDS
        assert entity.is_on is None


@pytest.mark.parametrize(
    "device",
    [
        snapshot(source="snapshot"),
        snapshot(source="snapshot", stamp=None, status="stale"),
        snapshot(source="snapshot", stamp=None, reason="producer_conflict"),
        snapshot(source="snapshot", stamp=None, value=True),
        snapshot(stamp=None),
        snapshot(reason="producer_conflict"),
        snapshot(source="rtc_push", status="stale"),
        snapshot(source="rtc_readback", status="retained"),
        snapshot(value=True, stateEvidence={}),
        snapshot(state={}, stateEvidence={}),
        snapshot(stationSn="other_hub", stateEvidence={}),
    ],
)
async def test_privacy_drops_prior_receipt_if_snapshot_cannot_keep_the_pair(device):
    entry = config_entry({"first": snapshot()})
    coordinator = snapshot_coordinator(entry)
    (entity,) = await privacy_switches(entry)
    with patch("custom_components.eufy_sdk.switch.time", return_value=3601):
        assert entity.is_on is False
        await refresh(coordinator, device)
        assert entity.is_on is None
        await refresh(coordinator, snapshot(stateEvidence={}))
        assert entity.is_on is None


async def test_privacy_replay_stays_off_across_reported_flicker_intervals():
    entry = config_entry({"first": snapshot()})
    coordinator = snapshot_coordinator(entry)
    (entity,) = await privacy_switches(entry)
    # Replay the reported 22:06:22 to 22:18:32 UTC history with synthetic inputs
    # for the three confirmed code paths. Every device value in the replay is off.
    transitions = [
        (96, snapshot(stateEvidence={}), ()),
        (139, snapshot(stamp=3_739_000), ()),
        (416, snapshot(stamp=3_739_000), (privacy_event(value=False),)),
        (445, snapshot(stamp=3_739_000), ()),
        (477, snapshot(source="rtc_readback", stamp=4_077_000), ()),
        (600, snapshot(stamp=4_200_000), ()),
        (
            718,
            snapshot(stateEvidence={}),
            (
                {
                    "event": "propertiesObserved",
                    "deviceSn": "first",
                    "source": "rtc",
                    "observedAtMs": 4_318_000,
                    "properties": {"privacy": False},
                },
            ),
        ),
        (730, snapshot(source="rtc_push", stamp=4_330_000), ()),
    ]
    with (
        patch("custom_components.eufy_sdk.switch.time") as clock,
        patch("custom_components.eufy_sdk.property_sync.time", clock),
    ):
        clock.return_value = 3600
        readings = [entity.is_on]
        for offset, device, events in transitions:
            clock.return_value = 3600 + offset
            await refresh(coordinator, device, *events)
            readings.append(entity.is_on)
        assert readings == [False] * (len(transitions) + 1)


@pytest.mark.parametrize("value", [False, True])
async def test_privacy_retains_receipt_across_untimed_and_omitted_evidence(value):
    entry = config_entry({"first": snapshot(value=value)})
    coordinator = snapshot_coordinator(entry)
    (entity,) = await privacy_switches(entry)
    untimed = snapshot(value=value, source="snapshot", stamp=None)
    omitted = snapshot(value=value, stateEvidence={})
    with (
        patch("custom_components.eufy_sdk.switch.time") as clock,
        patch("custom_components.eufy_sdk.privacy.time", clock),
    ):
        for elapsed, device in [(20, untimed), (40, omitted), (60, untimed)]:
            clock.return_value = RECEIPT_MS / 1000 + elapsed
            await refresh(coordinator, device.copy())
            assert entity.is_on is value

        clock.return_value = RECEIPT_MS / 1000 + MAX_AGE_SECONDS
        await refresh(coordinator, omitted.copy())
        assert entity.is_on is None


@pytest.mark.parametrize("has_receipt", [True, False])
async def test_privacy_front_porch_replay_handles_gap_between_refreshes(has_receipt):
    untimed = snapshot(source="snapshot", stamp=None)
    entry = config_entry({"first": snapshot() if has_receipt else untimed.copy()})
    coordinator = snapshot_coordinator(entry)
    (entity,) = await privacy_switches(entry)
    before_fresh_refresh = False if has_receipt else None
    start = RECEIPT_MS / 1000
    with (
        patch("custom_components.eufy_sdk.switch.time") as clock,
        patch("custom_components.eufy_sdk.privacy.time", clock),
    ):
        # Model the 260-second HA gap separately from the capture's bridge reads.
        clock.return_value = start + 60
        await refresh(coordinator, untimed)
        assert entity.is_on is before_fresh_refresh
        for elapsed in range(80, 320, 20):
            clock.return_value = start + elapsed
            assert entity.is_on is before_fresh_refresh

        clock.return_value = start + 320
        await refresh(coordinator, snapshot(stamp=int(clock.return_value * 1000)))
        assert entity.is_on is False


@pytest.mark.parametrize(
    "device",
    [
        snapshot(source="snapshot"),
        snapshot(source="rtc_push", stamp=None),
        snapshot(source="legacy_unknown"),
        snapshot(source="command_echo"),
        snapshot(stamp=None),
        snapshot(stamp=True),
        snapshot(stamp=RECEIPT_MS + 2000),
        snapshot(stamp=RECEIPT_MS - 1_800_000),
        snapshot(stateEvidence={}),
    ],
)
async def test_missing_unqualified_or_expired_privacy_reading_is_unknown(device):
    (entity,) = await privacy_switches(config_entry({"first": device}))
    with patch("custom_components.eufy_sdk.switch.time", return_value=3601):
        assert entity.is_on is None


@pytest.mark.parametrize(
    ("snapshot_min", "cloud_ms", "max_age"),
    [(1, 60_000, 180), (10, 600_000, MAX_AGE_SECONDS), (1440, 600_000, 87_600)],
)
async def test_privacy_expires_without_another_snapshot(
    snapshot_min, cloud_ms, max_age
):
    entry = config_entry(
        {"first": snapshot(value=True)}, snapshot_min=snapshot_min, cloud_ms=cloud_ms
    )
    (entity,) = await privacy_switches(entry)
    entity.hass = Mock(is_stopping=False)
    with (
        patch("custom_components.eufy_sdk.switch.time") as clock,
        patch("custom_components.eufy_sdk.switch.async_call_later") as later,
        patch.object(entity, "async_write_ha_state") as publish,
    ):
        clock.return_value = 3600
        await entity.async_added_to_hass()
        assert later.call_args.args[1] == max_age
        first_expiry = later.call_args.args[2]
        first_cancel = later.return_value

        clock.return_value = 4200
        entry.runtime_data.coordinator.data["first"] = snapshot(
            value=True, stamp=4_200_000
        )
        entity._handle_coordinator_update()
        first_cancel.assert_called_once()
        assert later.call_args.args[1] == max_age
        expiry = later.call_args.args[2]
        assert expiry == first_expiry
        publish.reset_mock()

        clock.return_value = 4200 + max_age
        expiry(None)
        publish.assert_called_once()
        assert entity.is_on is None


async def test_future_receipt_is_unknown_then_rechecked_without_a_snapshot():
    skew_seconds = 2
    entry = config_entry(
        {"first": snapshot(value=True, stamp=RECEIPT_MS + skew_seconds * 1000)}
    )
    (entity,) = await privacy_switches(entry)
    entity.hass = Mock(is_stopping=False)
    with (
        patch("custom_components.eufy_sdk.switch.time") as clock,
        patch("custom_components.eufy_sdk.switch.async_call_later") as later,
        patch.object(entity, "async_write_ha_state") as publish,
    ):
        clock.return_value = 3600
        await entity.async_added_to_hass()
        assert entity.is_on is None
        assert later.call_args.args[1] == skew_seconds
        clock.return_value += skew_seconds
        later.call_args.args[2](None)
        publish.assert_called_once()
        assert entity.is_on is True
        assert later.call_args.args[1] == MAX_AGE_SECONDS


@pytest.mark.parametrize("parent", [None, {"model": "T9000", "error": True}])
async def test_attached_camera_creates_no_privacy_switch_with_unknown_parent(parent):
    entry = config_entry(
        {"first": snapshot(model="T8425"), "standalone": snapshot(stationSn=None)},
        {
            key: [{"name": "privacy", "type": "bool", "writable": True}]
            for key in ("first", "standalone")
        },
    )
    data = entry.runtime_data.coordinator.data
    if parent is None:
        del data["hub"]
    else:
        data["hub"] = parent
    add = Mock()
    await setup_switches(Mock(), entry, add)
    assert [entity.unique_id for entity in add.call_args.args[0]] == [
        "standalone_privacy"
    ]
    assert await privacy_switches(entry) == []


@pytest.mark.parametrize("model", ["T8416", "T8425"])
async def test_reconcile_retires_s1_sensors_and_releases_supported_switch(model):
    entry = config_entry({"first": snapshot(value=True, model=model)})
    add = Mock()
    await setup_sensors(Mock(), entry, add)
    assert add.call_args.args[0] == []
    assert len(await privacy_switches(entry)) == (model == "T8416")
    registry = Registry(
        [
            Entry("switch.legacy", "switch", "first_privacy"),
            Entry("binary_sensor.privacy", "binary_sensor", "first_privacy_cloud"),
        ]
    )
    hass = Mock(data={}, bus=Mock())
    with (
        patch.object(er, "async_get", return_value=registry),
        patch.object(
            er,
            "async_entries_for_config_entry",
            side_effect=lambda reg, _: reg.entities.values(),
        ),
    ):
        async_reconcile_property_entities(hass, entry)
    assert (registry.async_get("switch.legacy").disabled_by is None) == (
        model == "T8416"
    )
    assert (
        registry.async_get("binary_sensor.privacy").disabled_by
        is er.RegistryEntryDisabler.INTEGRATION
    )


async def test_switch_uses_device_set_and_changes_only_from_qualified_receipts():
    entry = config_entry({"first": snapshot(state={})})
    coordinator = snapshot_coordinator(entry)
    coordinator.config_entry = entry
    entry.entry_id = None
    coordinator.property_observations = None
    client = AsyncMock()
    entry.runtime_data.client = client
    (entity,) = await privacy_switches(entry)

    await entity.async_turn_on()
    assert entity.is_on is None
    client.set_property.assert_awaited_once_with(
        "first", "privacy", value=True, station_privacy=True
    )

    with (
        patch("custom_components.eufy_sdk.switch.time", return_value=3601),
        patch("custom_components.eufy_sdk.property_sync.time", return_value=3601),
    ):
        report = {
            "event": "propertiesObserved",
            "deviceSn": "first",
            "source": "rtc",
            "observedAtMs": RECEIPT_MS,
            "properties": {"privacy": True},
            "evidence": snapshot(value=True, source="rtc_push")["stateEvidence"],
        }
        await refresh(coordinator, snapshot(state={}), report)
        assert entity.is_on is True
        await entity.async_turn_off()
        client.set_property.assert_awaited_with(
            "first", "privacy", value=False, station_privacy=True
        )
        assert entity.is_on is True


@pytest.mark.parametrize("target", [True, False], ids=["on", "off"])
@pytest.mark.parametrize(
    "report", ["matching", "different", "unknown", "expired", "future"]
)
@pytest.mark.parametrize(
    "error", [None, "privacy_unconfirmed", "command_ack_timeout"]
)
async def test_switch_always_sends_and_raises_every_failure(target, report, error):
    value = not target if report == "different" else target
    stamp = None if report == "unknown" else RECEIPT_MS
    offsets = {"expired": MAX_AGE_SECONDS, "future": -60}
    now = RECEIPT_MS / 1000 + offsets.get(report, 1)
    entry = config_entry(
        {"first": snapshot(value=value, stamp=stamp, source="rtc_push")}
    )
    entry.runtime_data.coordinator.config_entry = entry
    client = AsyncMock()
    if error:
        client.set_property.side_effect = EufySdkApiClientError(error)
    entry.runtime_data.client = client
    (entity,) = await privacy_switches(entry)
    accepted = error is None

    with patch("custom_components.eufy_sdk.switch.time", return_value=now):
        expected_state = None if report in ("unknown", "expired", "future") else value
        assert entity.is_on is expected_state
        turn = entity.async_turn_on if target else entity.async_turn_off
        if accepted:
            await turn()
        else:
            with pytest.raises(EufySdkApiClientError, match=error):
                await turn()

    client.set_property.assert_awaited_once_with(
        "first", "privacy", value=target, station_privacy=True
    )


async def test_camera_on_another_station_keeps_its_privacy_switch():
    entry = config_entry(
        {"other": snapshot(model="T8425", stationSn="base")},
        {"other": [{"name": "privacy", "type": "bool", "writable": True}]},
    )
    entry.runtime_data.coordinator.data["base"] = {"model": "T8030"}
    add = Mock()
    await setup_switches(Mock(), entry, add)
    assert [entity.unique_id for entity in add.call_args.args[0]] == ["other_privacy"]
    assert await privacy_switches(entry) == []
    registry = Registry([Entry("switch.other", "switch", "other_privacy")])
    hass = Mock(data={}, bus=Mock())
    with (
        patch.object(er, "async_get", return_value=registry),
        patch.object(
            er,
            "async_entries_for_config_entry",
            side_effect=lambda reg, _: reg.entities.values(),
        ),
    ):
        async_reconcile_property_entities(hass, entry)
    assert registry.async_get("switch.other").disabled_by is None


async def test_future_receipt_is_not_retained_over_a_later_snapshot():
    entry = config_entry({"first": snapshot()})
    coordinator = snapshot_coordinator(entry)
    (entity,) = await privacy_switches(entry)
    with (
        patch("custom_components.eufy_sdk.switch.time") as clock,
        patch("custom_components.eufy_sdk.privacy.time") as privacy_clock,
    ):
        clock.return_value = privacy_clock.return_value = 3700
        await refresh(coordinator, snapshot(stamp=4_000_000))
        assert entity.is_on is None
        await refresh(coordinator, snapshot(stamp=3_690_000))
        assert entity.is_on is False
        clock.return_value = privacy_clock.return_value = 3690 + MAX_AGE_SECONDS
        assert entity.is_on is None
