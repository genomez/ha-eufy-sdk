"""Exercise floodlight safety using synthetic camera evidence and fake time."""

# ruff: noqa: ANN001, ANN002, ANN003, ANN201, ANN202, D102, FBT003
# ruff: noqa: INP001, PT009, PT027, SLF001
import asyncio
import json
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import test_bounded_state_writes as bounded
import test_floodlight_state_hold as fixtures
from aiohttp import WSMsgType
from homeassistant.core import HassJob, HassJobType, HomeAssistant

from custom_components.eufy_sdk.api import (
    CommandReceipt,
    EufySdkApiClientCommunicationError,
    late_contrary_event,
)
from custom_components.eufy_sdk.const import (
    CONF_LIGHT_CORRECTION,
    CONF_LIGHT_CORRECTION_PAUSE,
)
from custom_components.eufy_sdk.light_correction import LightCorrection

CAPABILITY = {"version": 1, "watchWindowMs": 600000, "confirmation": True}


class SafetyFixture:
    """Share real entity callbacks and in-memory storage without external I/O."""

    _entity = fixtures.FloodlightStateHoldTests._entity
    _report = fixtures.FloodlightStateHoldTests._report

    def setup_light(self, *, enabled=True, desired=False):
        entity, coordinator, client, seen = self._entity(initial=not desired)
        self.now = 0.0
        self.sequence = 0
        self.events, self.tasks, self.commands = [], [], []
        self.next_intent = 0
        self.paused = False
        self.states = {}
        self.saved = {}
        self.entity, self.client = entity, client
        coordinator.config_entry.options = {CONF_LIGHT_CORRECTION: enabled}
        coordinator.config_entry.entry_id = "fixture-entry"
        coordinator.data["fixture-device"].update(
            model="T8425", stationSn="fixture-hub"
        )
        coordinator.data["fixture-hub"] = {"model": "T9000"}
        client.connected = True
        client.bounded_state_writes_supported = True
        client.late_contrary_effects_supported = True
        client.get_properties = AsyncMock(
            return_value=[{"name": "light", "boundedStateWrites": True}]
        )

        async def send(_sn, _name, value, **kwargs):
            guard = kwargs.pop("_send_guard", lambda: True)
            self.next_intent += 1
            if not guard():
                return CommandReceipt(
                    "cancelled_before_send",
                    False,
                    "unconfirmed",
                    intent=self.next_intent,
                )
            self.commands.append((self.now, value, kwargs))
            return CommandReceipt(
                "acknowledged", True, "unconfirmed", intent=self.next_intent
            )

        client.set_property.side_effect = send
        client.pause_light_correction = Mock()
        client.confirm_light = AsyncMock(
            side_effect=lambda _sn, intent: {
                "intent": intent,
                "confirmed": False,
                "recoveryBusy": False,
                "observedValue": not desired,
            }
        )
        entity.entity_id = "switch.fixture_light"
        entity.hass.states.get.side_effect = lambda key: (
            SimpleNamespace(state="on" if self.paused else "off")
            if key == "switch.pause"
            else self.states.get(key)
        )
        entity.hass.services.async_call = AsyncMock()

        def later(_hass, seconds, action):
            event = [self.now + seconds, action, False]
            self.events.append(event)
            return lambda: event.__setitem__(2, True)

        def task(coro, *_args, **_kwargs):
            result = asyncio.create_task(coro)
            self.tasks.append(result)
            return result

        def store(_hass, _version, key):
            return SimpleNamespace(
                async_load=AsyncMock(side_effect=lambda: deepcopy(self.saved.get(key))),
                async_delay_save=Mock(
                    side_effect=lambda data, _delay: self.saved.__setitem__(
                        key, deepcopy(data())
                    )
                ),
            )

        entity.hass.async_create_task.side_effect = task
        patches = (
            patch(
                "custom_components.eufy_sdk.light_correction.monotonic",
                lambda: self.now,
            ),
            patch(
                "custom_components.eufy_sdk.light_correction.time",
                lambda: 10000 + self.now + self.sequence / 1000,
            ),
            patch(
                "custom_components.eufy_sdk.light_correction.async_call_later", later
            ),
            patch(
                "custom_components.eufy_sdk.light_correction.Store", side_effect=store
            ),
            patch(
                "custom_components.eufy_sdk.light_correction.persistent_notification.async_create"
            ),
        )
        for item in patches:
            started = item.start()
            self.addCleanup(item.stop)
            if item is patches[-1]:
                self.notification = started
        entity._light_correction = LightCorrection(entity)
        self.addAsyncCleanup(SafetyFixture.close, self)
        return entity, coordinator, client, seen

    async def activate(self, **kwargs):
        result = self.setup_light(**kwargs)
        await result[0]._light_correction.async_added()
        return result

    async def close(self):
        self.entity._light_correction.stop("closed")
        for task in self.tasks:
            if not task.done():
                task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)

    async def drain(self):
        for _ in range(15):
            await asyncio.sleep(0)
        completed = [
            task for task in self.tasks if task.done() and not task.cancelled()
        ]
        if completed:
            await asyncio.gather(*completed)
        self.tasks = [task for task in self.tasks if not task.done()]

    async def advance(self, target):
        controller = self.entity._light_correction
        if controller.enabled and not controller.loaded and not controller.loading:
            await controller.async_added()
        while True:
            due = [
                event for event in self.events if not event[2] and event[0] <= target
            ]
            if not due:
                break
            event = min(due, key=lambda event: event[0])
            self.now, event[2] = event[0], True
            self.dispatch(event[1], None)
            await SafetyFixture.drain(self)
        self.now = target
        await SafetyFixture.drain(self)

    def dispatch(self, action, *args):
        job = HassJob(action)
        self.assertIs(job.job_type, HassJobType.Callback)
        host = SimpleNamespace(
            loop=asyncio.get_running_loop(), _tasks=set(), _background_tasks=set()
        )
        HomeAssistant._async_add_hass_job(host, job, *args)

    def observation(self, value, **overrides):
        self.sequence += 1
        properties = {"light": value}
        properties.update(overrides.pop("telemetry", {}))
        data = {
            "event": "propertiesObserved",
            "source": "rtc",
            "deviceSn": "fixture-device",
            "properties": properties,
            "observedAtMs": (10000 + self.now + self.sequence / 1000) * 1000,
            **overrides,
        }
        self.entity._handle_property_changed(SimpleNamespace(data=data))

    def emit(self, state="contrary", *, intent=None, observed=None, busy=False):
        controller = self.entity._light_correction
        self.entity._handle_property_changed(
            SimpleNamespace(
                data={
                    "event": "lateContraryEffect",
                    "deviceSn": "fixture-device",
                    "property": "light",
                    "intent": controller.sdk_intent if intent is None else intent,
                    "desiredValue": controller.last_command,
                    "observedValue": observed,
                    "ageMs": self.now * 1000,
                    "state": state,
                    "recoveryBusy": busy,
                }
            )
        )

    def motion(self, *, device="fixture-device", kind="motion"):
        self.entity._handle_property_changed(
            SimpleNamespace(
                data={
                    "event": kind,
                    "deviceSn": device,
                }
            )
        )

    def health(self, status="command_degraded", remaining=600000, revision=1):
        self.entity._handle_property_changed(
            SimpleNamespace(
                data={
                    "event": "commandHealth",
                    "sn": "fixture-device",
                    "property": "light",
                    "status": status,
                    "cooldownRemainingMs": remaining,
                    "revision": revision,
                }
            )
        )

    def binding(self, state):
        self.entity.coordinator.config_entry.options["light_safety_intent_entities"] = {
            "fixture-device": "sensor.fixture_intent"
        }
        self.states["sensor.fixture_intent"] = SimpleNamespace(state=state)
        self.entity._light_correction.check_policy()


class LightCorrectionTests(SafetyFixture, unittest.IsolatedAsyncioTestCase):
    """Exercise the new opt-in policy and fresh-report authority."""

    async def test_disabled_has_no_controller_commands_timers_storage_or_notices(self):
        entity, _, client, _ = await self.activate(enabled=False)
        controller = entity._light_correction
        self.observation(True, telemetry={"motion": False})
        self.motion()
        self.emit()
        controller.check_policy()
        await entity.write(False)
        await self.advance(2000)
        self.assertEqual([value for _, value, _ in self.commands], [False])
        self.assertEqual(self.events, [])
        self.assertIsNone(controller.store)
        self.notification.assert_not_called()
        client.confirm_light.assert_not_awaited()

    async def test_scope_requires_exact_floodlight_model_and_parent(self):
        entity, coordinator, _, _ = self.setup_light()
        controller = entity._light_correction
        for model, parent, prop, expected in (
            ("T8425", "T9000", "light", True),
            ("T8426", "T9000", "light", True),
            ("T8425-extra", "T9000", "light", False),
            ("T8426", "T8010", "light", False),
            ("T8425", "T9000", "lightPower", False),
            ("T8600", "T9000", "light", False),
        ):
            with self.subTest(model=model, parent=parent, prop=prop):
                entity.device["model"] = model
                coordinator.data["fixture-hub"]["model"] = parent
                entity._prop = prop
                self.assertIs(controller.enabled, expected)

    async def test_binding_overrides_dashboard_and_non_boolean_states_hold(self):
        entity, _, _, _ = await self.activate()
        self.binding("off")
        await entity.write(True)
        self.assertIs(entity._light_correction.intent(), False)
        await self.advance(120)
        self.assertEqual(len(self.commands), 1)
        for state, expected in (
            ("on", True),
            ("paused", None),
            ("unavailable", None),
            ("unknown", None),
        ):
            with self.subTest(state=state):
                self.binding(state)
                self.assertIs(entity._light_correction.intent(), expected)

    async def test_last_command_survives_restart_without_on_authority(self):
        entity, _, _, _ = await self.activate()
        await entity.write(True)
        old = entity._light_correction
        self.assertTrue(old.on_authorized)
        old.stop("closed")
        entity._light_correction = LightCorrection(entity)
        await entity._light_correction.async_added()
        restored = entity._light_correction
        self.assertTrue(restored.intent())
        self.assertFalse(restored.on_authorized)
        self.assertIsNone(restored.pending)
        self.observation(False)
        await self.advance(2000)
        self.assertEqual([value for _, value, _ in self.commands], [True])

    async def test_property_change_and_late_event_are_not_camera_confirmation(self):
        entity, _, _, _ = await self.activate()
        await entity.write(False)
        self._report(entity, False)
        self.emit("matched", observed=False)
        controller = entity._light_correction
        self.assertIs(controller.pending, False)
        self.assertIsNone(controller.observed)
        self.observation(False)
        self.assertIsNone(controller.pending)

    async def test_optimistic_display_cannot_confirm_off(self):
        entity, _, _, _ = await self.activate()
        self.observation(True)
        await entity.write(False)
        self.assertIs(entity.is_on, False)
        self.assertIs(entity._light_correction.observed, True)
        self.assertIs(entity._light_correction.pending, False)
        await self.advance(60)
        self.assertEqual([value for _, value, _ in self.commands], [False, False])

    async def test_stale_duplicate_future_and_non_rtc_reports_do_not_confirm(self):
        entity, _, _, _ = await self.activate()
        await entity.write(False)
        for change in (
            {"observedAtMs": 10000000},
            {"observedAtMs": 100000000},
            {"source": "cloud"},
            {"observedAtMs": True},
            {"observedAtMs": float("nan")},
        ):
            self.observation(False, **change)
            self.assertIs(entity._light_correction.pending, False)
        self.observation(False)
        stamp = entity._light_correction.observed_at * 1000
        self.observation(True, observedAtMs=stamp)
        self.assertIs(entity._light_correction.observed, False)

    async def test_live_option_enable_and_disable_controls_work(self):
        entity, coordinator, _, _ = await self.activate(enabled=False)
        controller = entity._light_correction
        coordinator.config_entry.options[CONF_LIGHT_CORRECTION] = True
        controller.options_updated()
        await self.drain()
        self.assertTrue(controller.loaded)
        self.assertIsNotNone(controller.timer)
        coordinator.config_entry.options[CONF_LIGHT_CORRECTION] = False
        controller.options_updated()
        self.assertIsNone(controller.timer)
        await self.advance(2000)
        self.assertEqual(self.commands, [])

    async def test_entry_pause_helper_blocks_pending_retry(self):
        entity, coordinator, _, _ = await self.activate()
        coordinator.config_entry.options[CONF_LIGHT_CORRECTION_PAUSE] = "switch.pause"
        await entity.write(False)
        self.paused = True
        entity._light_correction.check_policy()
        await self.advance(120)
        self.assertEqual(len(self.commands), 1)
        self.paused = False
        entity._light_correction.check_policy()
        await self.drain()
        self.assertEqual(len(self.commands), 2)


class LightCorrectionProtocolTests(unittest.IsolatedAsyncioTestCase):
    """Use the actual RPC/WS layer with in-memory bridge replies."""

    client = bounded.BoundedProtocolTests.client

    async def test_original_correction_expiry_bounds_caller_and_wire_deadlines(self):
        client = self.client()
        client._set_late_contrary_capability(CAPABILITY)
        deadline = asyncio.get_running_loop().time() + 5
        with patch(
            "custom_components.eufy_sdk.api.time.time", side_effect=[1000, 1000.4]
        ):
            await client.set_property(
                "fixture",
                "light",
                False,
                bounded=True,
                correction=True,
                correction_expires_at=deadline,
                correction_expires_at_ms=1005000,
            )
        execution = self.frames[-1]["execution"]
        self.assertEqual(execution["correctionExpiresAtMs"], 1005000)
        self.assertLessEqual(execution["callerExpiresAtMs"], 1005000)
        self.assertLessEqual(execution["remainingBudgetMs"], 4600)

    async def test_original_expiry_during_clock_exchange_prevents_dispatch(self):
        client = self.client()
        client._set_late_contrary_capability(CAPABILITY)
        deadline = asyncio.get_running_loop().time() + 5
        with (
            patch(
                "custom_components.eufy_sdk.api.time.time", side_effect=[1000, 1000.4]
            ),
            self.assertRaisesRegex(
                EufySdkApiClientCommunicationError, "expired_before_send"
            ),
        ):
            await client.set_property(
                "fixture",
                "light",
                False,
                bounded=True,
                correction=True,
                correction_expires_at=deadline,
                correction_expires_at_ms=1000200,
            )
        self.assertEqual([frame["cmd"] for frame in self.frames], ["boundedStateClock"])
        self.assertEqual(client._bounded_invocations, {})

    async def test_correction_deadlines_are_paired_finite_and_correction_only(self):
        client = self.client()
        client._set_late_contrary_capability(CAPABILITY)
        future = asyncio.get_running_loop().time() + 5
        for correction, expires, wall in (
            (True, future, None),
            (True, None, 1005000),
            (True, True, 1005000),
            (True, float("nan"), 1005000),
            (True, future, float("inf")),
            (True, future, -1),
            (True, future, 1600001),
            (False, future, 1005000),
            (True, asyncio.get_running_loop().time() - 1, 1005000),
        ):
            with (
                self.subTest(correction=correction, expires=expires, wall=wall),
                patch("custom_components.eufy_sdk.api.time.time", return_value=1000),
                self.assertRaises(EufySdkApiClientCommunicationError),
            ):
                await client.set_property(
                    "fixture",
                    "light",
                    False,
                    bounded=True,
                    correction=correction,
                    correction_expires_at=expires,
                    correction_expires_at_ms=wall,
                )
        self.assertEqual(self.frames, [])
        self.assertEqual(client._bounded_invocations, {})

    async def test_confirmation_reason_is_finite_unconfirmed_and_projected(self):
        client = self.client()
        client._set_late_contrary_capability(CAPABILITY)
        base = {
            "intent": 1,
            "desiredValue": False,
            "confirmed": False,
            "recoveryBusy": False,
            "ageMs": 35000,
            "reason": "read_budget_exhausted",
        }
        client.rpc = AsyncMock(
            return_value={"confirmation": {**base, "private": "discard"}}
        )
        self.assertEqual(await client.confirm_light("fixture", 1), base)
        for invalid in (
            {**base, "reason": "unknown"},
            {**base, "confirmed": True, "observedValue": True},
        ):
            client.rpc.return_value = {"confirmation": invalid}
            with self.assertRaises(EufySdkApiClientCommunicationError):
                await client.confirm_light("fixture", 1)

    async def test_correction_flag_only_in_negotiated_bounded_write(self):
        client = self.client()
        with self.assertRaises(EufySdkApiClientCommunicationError):
            await client.set_property(
                "fixture", "light", False, bounded=True, correction=True
            )
        self.assertEqual(self.frames, [])
        client._set_late_contrary_capability(CAPABILITY)
        await client.set_property(
            "fixture", "light", False, bounded=True, correction=True
        )
        self.assertIs(self.frames[-1]["execution"]["correction"], True)

    async def test_fresh_confirmation_is_read_only_and_projected(self):
        client = self.client()
        client._set_late_contrary_capability(CAPABILITY)
        confirmation = {
            "intent": 1,
            "desiredValue": False,
            "observedValue": True,
            "confirmed": True,
            "recoveryBusy": False,
            "ageMs": 33000,
        }

        async def reply(payload):
            self.frames.append(payload)
            client._pending[payload["id"]].set_result(
                {
                    "ok": True,
                    "confirmation": {**confirmation, "private": "discard"},
                }
            )

        client._ws.send_json.side_effect = reply
        self.assertEqual(await client.confirm_light("fixture", 1), confirmation)
        self.assertEqual(self.frames[0]["cmd"], "device.confirmLight")

    async def test_pause_during_clock_handshake_blocks_device_set_dispatch(self):
        client = self.client()
        client._set_late_contrary_capability(CAPABILITY)
        allowed = True
        previous = client._ws.send_json.side_effect

        async def clock_reply(payload):
            nonlocal allowed
            await previous(payload)
            allowed = False

        client._ws.send_json.side_effect = clock_reply
        with self.assertRaisesRegex(
            EufySdkApiClientCommunicationError, "^light_correction_paused_before_send$"
        ):
            await client.set_property(
                "fixture",
                "light",
                False,
                bounded=True,
                correction=True,
                _send_guard=lambda: allowed,
            )
        self.assertEqual([frame["cmd"] for frame in self.frames], ["boundedStateClock"])
        self.assertEqual(client._correction_requests, {})

    async def test_pause_cancel_keeps_original_rpc_future_for_terminal_receipt(self):
        client = self.client()
        client._set_late_contrary_capability(CAPABILITY)
        previous = client._ws.send_json.side_effect
        submitted = asyncio.Event()
        request_id = None

        async def reply(payload):
            nonlocal request_id
            if payload["cmd"] == "boundedStateClock":
                await previous(payload)
            elif payload["cmd"] == "device.set":
                self.frames.append(payload)
                request_id = payload["id"]
                submitted.set()
            elif payload["cmd"] == "boundedStateCancel":
                self.frames.append(payload)
                self.assertIs(payload["keepObservation"], True)
                self.assertEqual(payload["requestId"], request_id)
                self.assertFalse(client._pending[request_id].done())
                client._pending[request_id].set_result(
                    {
                        "ok": True,
                        "receipt": {
                            "status": "cancelled_before_send",
                            "sendAttempted": False,
                            "effect": "unconfirmed",
                            "intent": 2,
                        },
                    }
                )

        client._ws.send_json.side_effect = reply
        task = asyncio.create_task(
            client.set_property(
                "fixture",
                "light",
                False,
                bounded=True,
                correction=True,
                _send_guard=lambda: True,
            )
        )
        await submitted.wait()
        client.pause_light_correction("fixture")
        receipt = await task
        self.assertEqual(receipt.intent, 2)
        self.assertEqual(receipt.status, "cancelled_before_send")
        self.assertEqual(client._correction_requests, {})
        self.assertEqual(
            len(
                [frame for frame in self.frames if frame["cmd"] == "boundedStateCancel"]
            ),
            1,
        )

    async def test_invalid_confirmation_fails_closed(self):
        client = self.client()
        client._set_late_contrary_capability(CAPABILITY)
        for result in (
            None,
            {},
            {"intent": True},
            {"intent": 2, "desiredValue": False},
        ):
            client.rpc = AsyncMock(return_value={"confirmation": result})
            with self.assertRaises(EufySdkApiClientCommunicationError):
                await client.confirm_light("fixture", 1)

    def test_capability_and_event_validation_fail_closed(self):
        client = self.client()
        for value in (
            None,
            {},
            {**CAPABILITY, "version": True},
            {**CAPABILITY, "watchWindowMs": 120000},
            {**CAPABILITY, "confirmation": False},
        ):
            client._set_late_contrary_capability(value)
            self.assertFalse(client.late_contrary_effects_supported)
        event = {
            "event": "lateContraryEffect",
            "deviceSn": "fixture",
            "property": "light",
            "intent": 1,
            "desiredValue": False,
            "observedValue": True,
            "ageMs": 32000,
            "state": "contrary",
            "recoveryBusy": False,
        }
        self.assertEqual(late_contrary_event({**event, "private": "discard"}), event)
        for key, value in (
            ("intent", True),
            ("intent", 0),
            ("ageMs", float("nan")),
            ("ageMs", 600001),
            ("observedValue", 1),
            ("state", "unknown"),
        ):
            self.assertIsNone(late_contrary_event({**event, key: value}))
        missing_observation = event.copy()
        missing_observation.pop("observedValue")
        self.assertIsNone(late_contrary_event(missing_observation))

    async def test_actual_receive_loop_projects_events_and_emits_disconnect(self):
        client = self.client()
        events = []
        client._on_event = events.append
        hello = {
            "event": "hello",
            "boundedStateWrites": bounded.CAPABILITY,
            "lateContraryEffects": CAPABILITY,
        }
        event = {
            "event": "lateContraryEffect",
            "deviceSn": "fixture",
            "property": "light",
            "intent": 1,
            "desiredValue": False,
            "observedValue": True,
            "ageMs": 32000,
            "state": "contrary",
            "recoveryBusy": False,
        }

        async def frames():
            for value in (
                hello,
                {**event, "private": "discard"},
                {**event, "intent": False},
            ):
                yield SimpleNamespace(
                    type=WSMsgType.TEXT, json=lambda value=value: value
                )

        client._ws.__aiter__ = lambda _: frames()
        client._closing = True
        await client._receive_loop()
        self.assertEqual(events, [hello, event, {"event": "bridgeDisconnected"}])
        self.assertFalse(client.late_contrary_effects_supported)
        self.assertNotIn("discard", json.dumps(events))
