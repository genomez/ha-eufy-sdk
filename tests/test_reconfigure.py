# ruff: noqa: ANN201, D100, D101, D102, INP001, PT009, PT027, SLF001

import asyncio
import tempfile
import unittest
from types import MappingProxyType
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

from custom_components.eufy_sdk import config_flow
from custom_components.eufy_sdk.api import (
    EufySdkApiClientCommunicationError,
    EufySdkApiClientError,
)
from custom_components.eufy_sdk.const import DOMAIN
from homeassistant import config_entries, loader
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry, entity_registry, frame


class ReconfigureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.tmp.name)
        frame.async_setup(self.hass)
        loader.async_setup(self.hass)
        device_registry.async_setup(self.hass)
        self.hass.config_entries = config_entries.ConfigEntries(self.hass, {})
        await self.hass.config_entries.async_initialize()
        await device_registry.async_load(self.hass)
        await entity_registry.async_load(self.hass)
        self.entry = await self._entry("old.invalid", 3012)
        self.original = self._record(self.entry)
        self.flow = config_flow.EufySdkFlowHandler()
        self.flow.hass = self.hass
        self.flow.handler = DOMAIN
        self.flow.context = {
            "source": config_entries.SOURCE_RECONFIGURE,
            "entry_id": self.entry.entry_id,
        }
        self.client = Mock()
        self.client.connect = AsyncMock()
        self.client.close = AsyncMock()
        self.client.auth_status = AsyncMock(return_value={"state": "ok"})
        self.client.submit_2fa = AsyncMock(return_value={"state": "ok"})
        self.client.submit_captcha = AsyncMock(return_value={"state": "ok"})
        self.client.retrigger_auth = AsyncMock()
        self.reload_patch = patch.object(
            self.hass.config_entries, "async_schedule_reload"
        )
        self.reload = self.reload_patch.start()
        self.client_patch = patch.object(
            config_flow, "EufySdkApiClient", return_value=self.client
        )
        self.construct = self.client_patch.start()
        self.session_patch = patch.object(config_flow, "async_get_clientsession")
        self.session_patch.start()

    async def asyncTearDown(self):
        self.session_patch.stop()
        self.client_patch.stop()
        self.reload_patch.stop()
        await self.hass.async_block_till_done()
        await self.hass.async_stop(force=True)
        self.tmp.cleanup()

    async def _entry(
        self, host: str, port: int, *, unique_id: str | None = None,
    ) -> config_entries.ConfigEntry:
        entry = config_entries.ConfigEntry(
            version=1,
            minor_version=1,
            domain=DOMAIN,
            title=f"eufy bridge ({host})",
            data={"host": host, "port": port, "retained": {"value": "synthetic"}},
            options={"poll_interval_minutes": 19, "soc_refresh_seconds": 35},
            source=config_entries.SOURCE_USER,
            unique_id=unique_id or f"{host}:{port}",
            discovery_keys=MappingProxyType({}),
            subentries_data=(),
        )
        with patch.object(self.hass.config_entries, "async_setup", new=AsyncMock()):
            await self.hass.config_entries.async_add(entry)
        return entry

    @staticmethod
    def _record(entry: config_entries.ConfigEntry) -> tuple:
        return (
            entry.entry_id, entry.version, entry.minor_version, entry.title,
            dict(entry.data), dict(entry.options), entry.unique_id,
        )

    def _unchanged(self) -> None:
        self.assertEqual(self._record(self.entry), self.original)
        self.reload.assert_not_called()
        self.client.retrigger_auth.assert_not_awaited()

    def _success(
        self, result: dict[str, Any], host: str = "new.invalid", port: int = 4012,
    ) -> None:
        self.assertEqual(result["type"], FlowResultType.ABORT)
        self.assertEqual(result["reason"], "reconfigure_successful")
        self.assertEqual(self.entry.entry_id, self.original[0])
        self.assertEqual((self.entry.version, self.entry.minor_version), (1, 1))
        self.assertEqual(self.entry.data, {
            "host": host, "port": port, "retained": {"value": "synthetic"},
        })
        self.assertEqual(self.entry.options, self.original[5])
        self.assertEqual(self.entry.unique_id, f"{host}:{port}")
        self.assertEqual(self.entry.title, f"eufy bridge ({host})")
        self.assertEqual(len(self.hass.config_entries.async_entries(DOMAIN)), 1)
        self.client.retrigger_auth.assert_not_awaited()
        self.client.list_devices.assert_not_called()
        self.client.list_solix_devices.assert_not_called()

    async def test_defaults_and_schema_do_not_mutate_entry(self):
        result = await self.flow.async_step_reconfigure()
        self.assertEqual(result["type"], FlowResultType.FORM)
        self.assertEqual(result["step_id"], "reconfigure")
        self.assertEqual(result["data_schema"]({}), {
            "host": "old.invalid", "port": 3012.0,
        })
        self.construct.assert_not_called()
        self._unchanged()

    async def test_success_preserves_real_registry_relationships(self):
        dr = device_registry.async_get(self.hass)
        er = entity_registry.async_get(self.hass)
        device = dr.async_get_or_create(
            config_entry_id=self.entry.entry_id,
            identifiers={(DOMAIN, "SYNTHETIC_DEVICE")},
        )
        entity = er.async_get_or_create(
            "sensor", DOMAIN, "SYNTHETIC_ENTITY", config_entry=self.entry,
            device_id=device.id,
        )
        before = (entity.entity_id, entity.unique_id, entity.config_entry_id,
                  entity.device_id, device.identifiers, device.config_entry_id)
        result = await self.flow.async_step_reconfigure({
            "host": " new.invalid ", "port": 4012.0,
        })
        self._success(result)
        self.reload.assert_called_once_with(self.entry.entry_id)
        self.client.close.assert_awaited_once()
        self.assertEqual(before, (
            entity.entity_id, entity.unique_id, entity.config_entry_id,
            entity.device_id, device.identifiers, device.config_entry_id,
        ))

    async def test_same_endpoint_validates_without_reload_or_mutation(self):
        result = await self.flow.async_step_reconfigure({
            "host": "old.invalid", "port": 3012,
        })
        self._success(result, "old.invalid", 3012)
        self._unchanged()
        self.client.connect.assert_awaited_once()

    async def test_existing_update_listener_owns_one_reload(self):
        async def listener(
            hass: HomeAssistant, entry: config_entries.ConfigEntry,
        ) -> None:
            hass.config_entries.async_schedule_reload(entry.entry_id)

        remove = self.entry.add_update_listener(listener)
        try:
            self._success(await self.flow.async_step_reconfigure({
                "host": "new.invalid", "port": 4012,
            }))
            await self.hass.async_block_till_done()
            self.reload.assert_called_once_with(self.entry.entry_id)
        finally:
            remove()

    async def test_empty_host_is_rejected_without_io(self):
        result = await self.flow.async_step_reconfigure({"host": "  ", "port": 4012})
        self.assertEqual(result["errors"], {"host": "invalid_host"})
        self.construct.assert_not_called()
        self._unchanged()

    async def test_connection_failure_is_private_closes_client_without_mutation(self):
        self.client.connect.side_effect = EufySdkApiClientCommunicationError(
            "synthetic-private-marker"
        )
        with self.assertLogs(config_flow.LOGGER, level="WARNING") as logs:
            result = await self.flow.async_step_reconfigure({
                "host": "new.invalid", "port": 4012,
            })
        self.assertEqual(result["errors"], {"base": "cannot_connect"})
        self.assertNotIn("synthetic-private-marker", str(logs.output))
        self.client.close.assert_awaited_once()
        self.assertIsNone(self.flow._client)
        self._unchanged()

    async def test_auth_status_failure_closes_without_mutation(self):
        self.client.auth_status.side_effect = EufySdkApiClientError("synthetic")
        result = await self.flow.async_step_reconfigure({
            "host": "new.invalid", "port": 4012,
        })
        self.assertEqual(result["reason"], "cannot_connect")
        self.client.close.assert_awaited_once()
        self._unchanged()

    async def test_incomplete_auth_never_mutates_or_retriggers(self):
        for state, reason in (
            ("pending", "auth_pending"), ("blocked", "auth_blocked"),
            ("cooldown", "auth_cooldown"), ("unknown", "auth_not_ready"),
        ):
            with self.subTest(state=state):
                self.client.auth_status.return_value = {"state": state}
                result = await self.flow.async_step_reconfigure({
                    "host": "new.invalid", "port": 4012,
                })
                self.assertEqual(result["reason"], reason)
                self._unchanged()

    async def test_collision_by_unique_id_before_io_preserves_both(self):
        other = await self._entry("other.invalid", 4012, unique_id="new.invalid:4012")
        prior = self._record(other)
        result = await self.flow.async_step_reconfigure({
            "host": "new.invalid", "port": 4012,
        })
        self.assertEqual(result["reason"], "already_configured")
        self.assertEqual(self._record(other), prior)
        self.construct.assert_not_called()
        self._unchanged()

    async def test_collision_by_data_even_if_other_unique_id_differs(self):
        await self._entry("new.invalid", 4012, unique_id="synthetic-legacy-id")
        result = await self.flow.async_step_reconfigure({
            "host": "new.invalid", "port": 4012,
        })
        self.assertEqual(result["reason"], "already_configured")
        self.construct.assert_not_called()
        self._unchanged()

    async def test_explicit_twofa_updates_existing_entry_only_after_success(self):
        self.client.auth_status.return_value = {"state": "require_2fa"}
        first = await self.flow.async_step_reconfigure({
            "host": "new.invalid", "port": 4012,
        })
        self.assertEqual(first["step_id"], "twofa")
        self._unchanged()
        self.client.submit_2fa.return_value = {"state": "require_2fa"}
        invalid = await self.flow.async_step_twofa({"code": "WRONG"})
        self.assertEqual(invalid["errors"], {"base": "invalid_2fa"})
        self._unchanged()
        self.client.submit_2fa.return_value = {"state": "ok"}
        self.client.auth_status.return_value = {"state": "ok"}
        self._success(await self.flow.async_step_twofa({"code": "000000"}))

    async def test_captcha_then_twofa_preserves_entry_until_authenticated(self):
        self.client.auth_status.return_value = {"state": "require_captcha"}
        first = await self.flow.async_step_reconfigure({
            "host": "new.invalid", "port": 4012,
        })
        self.assertEqual(first["step_id"], "captcha")
        self._unchanged()
        self.client.submit_captcha.return_value = {"state": "require_captcha"}
        invalid = await self.flow.async_step_captcha({"answer": "WRONG"})
        self.assertEqual(invalid["errors"], {"base": "invalid_captcha"})
        self._unchanged()
        self.client.submit_captcha.return_value = {"state": "require_2fa"}
        self.client.auth_status.return_value = {"state": "require_2fa"}
        twofa = await self.flow.async_step_captcha({"answer": "SYNTHETIC"})
        self.assertEqual(twofa["step_id"], "twofa")
        self._unchanged()
        self.client.auth_status.return_value = {"state": "ok"}
        self._success(await self.flow.async_step_twofa({"code": "000000"}))

    async def test_collision_during_challenge_aborts_without_mutation(self):
        self.client.auth_status.return_value = {"state": "require_2fa"}
        await self.flow.async_step_reconfigure({"host": "new.invalid", "port": 4012})
        other = await self._entry("new.invalid", 4012)
        prior = self._record(other)
        self.client.auth_status.return_value = {"state": "ok"}
        result = await self.flow.async_step_twofa({"code": "000000"})
        self.assertEqual(result["reason"], "already_configured")
        self.assertEqual(self._record(other), prior)
        self.client.close.assert_awaited_once()
        self._unchanged()

    async def test_cancelled_auth_validation_does_not_mutate(self):
        self.client.auth_status.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.flow.async_step_reconfigure({
                "host": "new.invalid", "port": 4012,
            })
        self._unchanged()
        self.client.close.assert_awaited_once()

    async def test_removal_during_challenge_closes_client_without_mutation(self):
        self.client.auth_status.return_value = {"state": "require_2fa"}
        await self.flow.async_step_reconfigure({"host": "new.invalid", "port": 4012})
        self.flow.async_remove()
        await self.hass.async_block_till_done()
        self.client.close.assert_awaited_once()
        self.assertIsNone(self.flow._client)
        self._unchanged()

    async def test_removal_during_connect_closes_late_assigned_socket(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        socket_open = False

        async def connect() -> None:
            nonlocal socket_open
            entered.set()
            await release.wait()
            socket_open = True

        async def close() -> None:
            nonlocal socket_open
            socket_open = False

        self.client.connect.side_effect = connect
        self.client.close.side_effect = close
        task = asyncio.create_task(self.flow.async_step_reconfigure({
            "host": "new.invalid", "port": 4012,
        }))
        await entered.wait()
        self.flow.async_remove()
        await asyncio.sleep(0)
        self.assertEqual(self.client.close.await_count, 1)
        release.set()
        result = await task
        self.assertEqual(result["reason"], "cannot_connect")
        self.assertFalse(socket_open)
        self.assertEqual(self.client.close.await_count, 2)
        self.client.auth_status.assert_not_awaited()
        self._unchanged()

    async def test_removal_during_auth_validation_closes_without_late_update(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def auth() -> dict[str, str]:
            entered.set()
            await release.wait()
            return {"state": "ok"}

        self.client.auth_status.side_effect = auth
        task = asyncio.create_task(self.flow.async_step_reconfigure({
            "host": "new.invalid", "port": 4012,
        }))
        await entered.wait()
        self.flow.async_remove()
        await asyncio.sleep(0)
        release.set()
        self.assertEqual((await task)["reason"], "cannot_connect")
        self._unchanged()

    async def test_challenge_rpc_exception_closes_client_without_mutation(self):
        for state, method, payload in (
            ("require_2fa", "async_step_twofa", {"code": "000000"}),
            ("require_captcha", "async_step_captcha", {"answer": "SYNTHETIC"}),
        ):
            with self.subTest(state=state):
                self.client.close.reset_mock()
                self.client.auth_status.return_value = {"state": state}
                await self.flow.async_step_reconfigure({
                    "host": "new.invalid", "port": 4012,
                })
                self.client.submit_2fa.side_effect = EufySdkApiClientError("synthetic")
                self.client.submit_captcha.side_effect = EufySdkApiClientError(
                    "synthetic"
                )
                with self.assertRaises(EufySdkApiClientError):
                    await getattr(self.flow, method)(payload)
                self.client.close.assert_awaited_once()
                self.assertIsNone(self.flow._client)
                self._unchanged()

    async def test_reconfigure_back_restores_endpoint_data_and_identifiers(self):
        self._success(await self.flow.async_step_reconfigure({
            "host": "new.invalid", "port": 4012,
        }))
        result = await self.flow.async_step_reconfigure({
            "host": "old.invalid", "port": 3012,
        })
        self._success(result, "old.invalid", 3012)
        self.assertEqual(self._record(self.entry), self.original)
