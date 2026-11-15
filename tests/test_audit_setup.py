"""Exercise real entry setup, default-hidden registration and admin release."""

# ruff: noqa: ANN201, D101, D102, INP001, PT009, PT027

import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.exceptions import HomeAssistantError, Unauthorized
from homeassistant.helpers import entity_registry as er

import custom_components.eufy_sdk as integration
from custom_components.eufy_sdk.audit_visibility import MARKER
from custom_components.eufy_sdk.const import CONF_HOST, CONF_PORT
from custom_components.eufy_sdk.property_entities import _presentation_decisions
from custom_components.eufy_sdk.select import EufySdkSelect
from custom_components.eufy_sdk.sensor import _is_sensor
from tests.test_property_entities import Entry, Registry


class AuditSetupTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_setup_owns_new_hidden_rows_and_admin_service_releases_them(
        self,
    ):
        hass = Mock(data={}, http=None)
        entry = Mock(entry_id="synthetic-entry", domain="eufy_sdk", options={})
        entry.data = {CONF_HOST: "bridge", CONF_PORT: 3000}
        coordinator = Mock(data={"station": {"model": "T9000-family", "state": {}}})
        coordinator.async_config_entry_first_refresh = AsyncMock()
        client = Mock(
            set_poll_ms=AsyncMock(),
            get_properties=AsyncMock(
                return_value=[
                    {
                        "name": "hubAlarmTone",
                        "type": "enum",
                        "kind": "enum",
                        "writable": True,
                        "enumValues": {"0": "Default"},
                    }
                ]
            ),
        )
        registry = Registry([])
        created = []

        async def forward(config_entry: Any, _platforms: Any) -> None:
            self.assertEqual(registry.entities, {})
            entity = EufySdkSelect(
                coordinator,
                "station",
                config_entry.runtime_data.properties["station"][0],
            )
            self.assertFalse(entity.entity_registry_visible_default)
            row = Entry(
                entity_id="select.tone",
                domain="select",
                unique_id=entity.unique_id,
                hidden_by=er.RegistryEntryHider.INTEGRATION,
            )
            registry.entities[row.entity_id] = row
            created.append(row)

        hass.config_entries.async_forward_entry_setups = AsyncMock(side_effect=forward)
        with (
            patch.object(
                integration, "EufySdkDataUpdateCoordinator", return_value=coordinator
            ),
            patch.object(integration, "EufySdkApiClient", return_value=client),
            patch.object(integration, "async_get_clientsession", return_value=Mock()),
            patch.object(
                integration, "async_get_loaded_integration", return_value=Mock()
            ),
            patch.object(er, "async_get", return_value=registry),
            patch.object(
                er,
                "async_entries_for_config_entry",
                side_effect=lambda _reg, _id: list(registry.entities.values()),
            ),
        ):
            self.assertTrue(await integration.async_setup_entry(hass, entry))
            self.assertEqual(len(created), 1)
            self.assertTrue(
                registry.async_get("select.tone").options["eufy_sdk"][MARKER]["owned"]
            )
            registration = next(
                call
                for call in hass.services.async_register.call_args_list
                if call.args[1] == "release_audit_hides"
            )
            handler = registration.args[2]
            hass.auth.async_get_user = AsyncMock(
                return_value=SimpleNamespace(is_admin=False)
            )
            call = SimpleNamespace(
                data={"entry_id": entry.entry_id}, context=SimpleNamespace(user_id=True)
            )
            with self.assertRaises(Unauthorized):
                await handler(call)
            self.assertEqual(
                registry.async_get("select.tone").hidden_by,
                er.RegistryEntryHider.INTEGRATION,
            )
            hass.auth.async_get_user = AsyncMock(
                return_value=SimpleNamespace(is_admin=True)
            )
            hass.async_run_hass_job = lambda job, service_call: job.target(service_call)
            await handler(call)
            self.assertIsNone(registry.async_get("select.tone").hidden_by)
            self.assertTrue(
                registry.async_get("select.tone").options["eufy_sdk"][MARKER][
                    "released"
                ]
            )
            with self.assertRaisesRegex(HomeAssistantError, "not loaded"):
                await handler(
                    SimpleNamespace(data={"entry_id": "unloaded"}, context=call.context)
                )
            for cleanup in entry.async_on_unload.call_args_list:
                cleanup.args[0]()
            hass.services.async_remove.assert_any_call(
                "eufy_sdk", "release_audit_hides"
            )

    def test_duplicate_ringtone_read_is_limited_to_attached_t9000(self):
        spec = {
            "name": "ringtoneVolume",
            "type": "number",
            "kind": "percent",
            "writable": True,
        }
        for model in ("T9000-family", "T8030"):
            coordinator = SimpleNamespace(
                data={"camera": {"stationSn": "station"}, "station": {"model": model}}
            )
            entry = SimpleNamespace(
                runtime_data=SimpleNamespace(
                    coordinator=coordinator, properties={"camera": [spec]}
                )
            )
            self.assertEqual(
                _is_sensor(spec, t9000=model.startswith("T9000")),
                model.startswith("T9000"),
            )
            decisions = _presentation_decisions(entry)
            self.assertFalse(decisions[("sensor", "camera_ringtoneVolume")])
