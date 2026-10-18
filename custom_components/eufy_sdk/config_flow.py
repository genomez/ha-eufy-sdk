"""Config flow for eufy_sdk — connect to the bridge, then drive 2FA/captcha to login."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    EufySdkApiClient,
    EufySdkApiClientCommunicationError,
    EufySdkApiClientError,
)
from .const import (
    CONF_HOST,
    CONF_LEGACY_DOORBELL_PRESSED,
    CONF_LIGHT_CORRECTION,
    CONF_LIGHT_CORRECTION_PAUSE,
    CONF_POLL_INTERVAL,
    CONF_PORT,
    CONF_SOC_REFRESH,
    DEFAULT_POLL_INTERVAL_MIN,
    DEFAULT_PORT,
    DEFAULT_SOC_REFRESH_SEC,
    DOMAIN,
    LOGGER,
)


class EufySdkFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    """Ask for the bridge address, then walk the user through login (2FA / captcha)."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,  # noqa: ARG004
    ) -> EufySdkOptionsFlow:
        """Return the options flow (poll interval)."""
        return EufySdkOptionsFlow()

    def __init__(self) -> None:
        """Hold the in-flight bridge connection across steps."""
        self._client: EufySdkApiClient | None = None
        self._host: str = ""
        self._port: int = DEFAULT_PORT
        self._reconfigure_removed = False

    @callback
    def async_remove(self) -> None:
        """Retire a reconfigure connection when HA removes its flow."""
        if self.source != config_entries.SOURCE_RECONFIGURE:
            return
        self._reconfigure_removed = True
        if self._client is not None:
            client, self._client = self._client, None
            self.hass.async_create_task(client.close())

    async def _close_reconfigure_client(self) -> None:
        """Close a failed or cancelled reconfigure connection."""
        if self.source == config_entries.SOURCE_RECONFIGURE and self._client:
            client, self._client = self._client, None
            await client.close()

    def _reconfigure_endpoint_in_use(self) -> bool:
        """Reject another entry's endpoint without changing either entry."""
        current = self._get_reconfigure_entry()
        endpoint = f"{self._host}:{self._port}"
        return any(
            entry.entry_id != current.entry_id
            and (
                entry.unique_id == endpoint
                or (
                    entry.data.get(CONF_HOST) == self._host
                    and entry.data.get(CONF_PORT) == self._port
                )
            )
            for entry in self._async_current_entries()
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Validate a replacement bridge endpoint before updating the entry."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            self._host = str(user_input[CONF_HOST]).strip()
            self._port = int(user_input[CONF_PORT])
            if not self._host:
                errors[CONF_HOST] = "invalid_host"
            elif self._reconfigure_endpoint_in_use():
                return self.async_abort(reason="already_configured")
            else:
                client = self._client = EufySdkApiClient(
                    self._host, self._port, async_get_clientsession(self.hass)
                )
                try:
                    await client.connect()
                except EufySdkApiClientCommunicationError:
                    await client.close()
                    self._client = None
                    LOGGER.warning("bridge reconfigure failed [bridge_unreachable]")
                    errors["base"] = "cannot_connect"
                except BaseException:
                    await client.close()
                    self._client = None
                    raise
                else:
                    # Removal can close the client before connect assigns its
                    # socket. Close again after connect to retire that late socket.
                    if self._reconfigure_removed:
                        await client.close()
                        self._client = None
                        return self.async_abort(reason="cannot_connect")
                    try:
                        return await self._continue_auth()
                    except BaseException:
                        await self._close_reconfigure_client()
                        raise

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_HOST,
                        default=(user_input or entry.data)[CONF_HOST],
                    ): selector.TextSelector(),
                    vol.Required(
                        CONF_PORT,
                        default=(user_input or entry.data)[CONF_PORT],
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=1, max=65535, mode=selector.NumberSelectorMode.BOX
                        )
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self,
        entry_data: dict[str, Any],
    ) -> config_entries.ConfigFlowResult:
        """
        Re-drive login when the bridge loses auth after setup.

        The coordinator raises `ConfigEntryAuthFailed` when the bridge auth is non-`ok`
        (session expired → a fresh 2FA/captcha is needed). HA starts a reauth flow and
        calls this; we reconnect and reuse the `twofa` / `captcha` steps so the user can
        complete the challenge from inside HA.
        """
        self._host = entry_data[CONF_HOST]
        self._port = int(entry_data[CONF_PORT])
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self,
        user_input: dict[str, Any] | None = None,  # noqa: ARG002 — a bare confirm, no fields
    ) -> config_entries.ConfigFlowResult:
        """Reconnect to the bridge, then route to its current auth challenge."""
        try:
            self._client = EufySdkApiClient(
                self._host, self._port, async_get_clientsession(self.hass)
            )
            await self._client.connect()
        except EufySdkApiClientCommunicationError:
            LOGGER.warning("bridge reconnect for reauth failed [bridge_unreachable]")
            return self.async_show_form(
                step_id="reauth_confirm",
                errors={"base": "cannot_connect"},
                description_placeholders={"host": self._host},
            )
        return await self._continue_auth()

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Step 1: the bridge's host + port."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._host = str(user_input[CONF_HOST]).strip()
            # NumberSelector hands back a float (3012.0); int-ify it for the WS URL.
            self._port = int(user_input[CONF_PORT])
            await self.async_set_unique_id(f"{self._host}:{self._port}")
            self._abort_if_unique_id_configured()
            try:
                self._client = EufySdkApiClient(
                    self._host, self._port, async_get_clientsession(self.hass)
                )
                await self._client.connect()
            except EufySdkApiClientCommunicationError:
                LOGGER.warning("bridge connect failed [bridge_unreachable]")
                errors["base"] = "cannot_connect"
            else:
                return await self._continue_auth()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_HOST,
                        default=(user_input or {}).get(CONF_HOST, vol.UNDEFINED),
                    ): selector.TextSelector(),
                    vol.Required(
                        CONF_PORT,
                        default=(user_input or {}).get(CONF_PORT, DEFAULT_PORT),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=1, max=65535, mode=selector.NumberSelectorMode.BOX
                        ),
                    ),
                },
            ),
            errors=errors,
        )

    async def _continue_auth(  # noqa: PLR0911 - explicit auth/ownership outcomes
        self,
    ) -> config_entries.ConfigFlowResult:
        """Route to the right step for the bridge's current auth state."""
        if self._client is None:
            return self.async_abort(reason="cannot_connect")
        client = self._client
        try:
            auth = await client.auth_status()
        except EufySdkApiClientError:
            LOGGER.error("auth.status failed [auth_status_unavailable]")
            await client.close()
            self._client = None
            return self.async_abort(reason="cannot_connect")

        except BaseException:
            await self._close_reconfigure_client()
            raise

        if (
            self.source == config_entries.SOURCE_RECONFIGURE
            and self._reconfigure_removed
        ):
            await client.close()
            return self.async_abort(reason="cannot_connect")

        state = auth.get("state")
        if state == "ok":
            await client.close()  # the coordinator opens its own connection
            self._client = None
            if self.source == config_entries.SOURCE_RECONFIGURE:
                if self._reconfigure_removed:
                    return self.async_abort(reason="cannot_connect")
                # A different entry may have claimed this endpoint while the
                # user completed a challenge. Recheck after the last await.
                if self._reconfigure_endpoint_in_use():
                    return self.async_abort(reason="already_configured")
                update = {
                    "data_updates": {CONF_HOST: self._host, CONF_PORT: self._port},
                    "unique_id": f"{self._host}:{self._port}",
                    "title": f"eufy bridge ({self._host})",
                }
                entry = self._get_reconfigure_entry()
                # The integration's registered update listener already reloads
                # a loaded entry. Let that listener own the reload exactly once.
                if entry.update_listeners:
                    return self.async_update_and_abort(entry, **update)
                return self.async_update_reload_and_abort(
                    entry,
                    **update,
                    reload_even_if_entry_is_unchanged=False,
                )
            # Reauth → the entry exists, so reload it; first setup → create it.
            return (
                self.async_update_reload_and_abort(
                    self._get_reauth_entry(), data_updates={}
                )
                if self.source == config_entries.SOURCE_REAUTH
                else self.async_create_entry(
                    title=f"eufy bridge ({self._host})",
                    data={CONF_HOST: self._host, CONF_PORT: self._port},
                )
            )
        if state == "require_2fa":
            return await self.async_step_twofa()
        if state == "require_captcha":
            return await self.async_step_captcha()
        # The bridge owns login. Observing an incomplete or failed attempt must
        # not start additional cloud logins, including during a reauth flow.
        await client.close()
        self._client = None
        return self.async_abort(
            reason={
                "pending": "auth_pending",
                "blocked": "auth_blocked",
                "cooldown": "auth_cooldown",
            }.get(state, "auth_not_ready")
        )

    async def async_step_twofa(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Submit the 2FA code that was sent to the account."""
        if self._client is None:
            return self.async_abort(reason="cannot_connect")
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input.get("resend"):
                try:
                    await self._client.retrigger_auth()
                except BaseException:
                    await self._close_reconfigure_client()
                    raise
                return await self._continue_auth()
            try:
                auth = await self._client.submit_2fa(str(user_input["code"]))
            except BaseException:
                await self._close_reconfigure_client()
                raise
            if auth.get("state") == "ok":
                return await self._continue_auth()
            errors["base"] = "invalid_2fa"

        return self.async_show_form(
            step_id="twofa",
            data_schema=vol.Schema(
                {
                    vol.Required("code"): selector.TextSelector(),
                    vol.Optional("resend", default=False): selector.BooleanSelector(),
                },
            ),
            errors=errors,
        )

    async def async_step_captcha(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Show the captcha image (markdown in the description) and take the answer."""
        if self._client is None:
            return self.async_abort(reason="cannot_connect")
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input.get("refresh"):
                try:
                    await self._client.retrigger_auth()
                except BaseException:
                    await self._close_reconfigure_client()
                    raise
                return await self._continue_auth()
            try:
                auth = await self._client.submit_captcha(str(user_input["answer"]))
            except BaseException:
                await self._close_reconfigure_client()
                raise
            # An accepted answer may lead to email 2FA before login completes.
            if auth.get("state") in ("ok", "require_2fa"):
                return await self._continue_auth()
            errors["base"] = "invalid_captcha"

        try:
            auth = await self._client.auth_status()
        except BaseException:
            await self._close_reconfigure_client()
            raise
        image = auth.get("image", "")
        return self.async_show_form(
            step_id="captcha",
            data_schema=vol.Schema(
                {
                    vol.Required("answer"): selector.TextSelector(),
                    vol.Optional("refresh", default=False): selector.BooleanSelector(),
                },
            ),
            # The frontend renders the description as markdown; embed the data-URI.
            description_placeholders={
                "image": f"![captcha]({image})"
                if image
                else "(captcha unavailable — tick refresh)"
            },
            errors=errors,
        )


class EufySdkOptionsFlow(config_entries.OptionsFlow):
    """Configure polling and doorbell event compatibility."""

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Show and save polling and doorbell event options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        current = self.config_entry.options.get(
            CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL_MIN
        )
        soc_current = self.config_entry.options.get(
            CONF_SOC_REFRESH, DEFAULT_SOC_REFRESH_SEC
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_POLL_INTERVAL, default=current
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=1,
                            max=1440,
                            step=1,
                            unit_of_measurement="min",
                            mode=selector.NumberSelectorMode.BOX,
                        ),
                    ),
                    vol.Required(
                        CONF_SOC_REFRESH, default=soc_current
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=15,
                            max=3600,
                            step=5,
                            unit_of_measurement="s",
                            mode=selector.NumberSelectorMode.BOX,
                        ),
                    ),
                    vol.Required(
                        CONF_LEGACY_DOORBELL_PRESSED,
                        default=self.config_entry.options.get(
                            CONF_LEGACY_DOORBELL_PRESSED, False
                        ),
                    ): selector.BooleanSelector(),
                    vol.Required(
                        CONF_LIGHT_CORRECTION,
                        default=self.config_entry.options.get(
                            CONF_LIGHT_CORRECTION, False
                        ),
                    ): selector.BooleanSelector(),
                    vol.Optional(
                        CONF_LIGHT_CORRECTION_PAUSE,
                        description={
                            "suggested_value": self.config_entry.options.get(
                                CONF_LIGHT_CORRECTION_PAUSE
                            )
                        },
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain=["input_boolean", "switch"]
                        )
                    ),
                },
            ),
        )
