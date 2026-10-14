# ruff: noqa: ANN201, D100, D101, D102, INP001, PT009, SLF001

import unittest
from unittest.mock import AsyncMock, Mock, patch

from custom_components.eufy_sdk.api import EufySdkApiClientError
from custom_components.eufy_sdk.config_flow import EufySdkFlowHandler


class PilotAuthSafetyTests(unittest.IsolatedAsyncioTestCase):
    def make_flow(self, state: str, source: str = "user"):
        flow = EufySdkFlowHandler()
        flow.context = {"source": source}
        client = Mock()
        # An unwanted automatic retrigger makes the baseline fail immediately,
        # rather than allowing its recursive cloud-login loop to continue.
        client.auth_status = AsyncMock(return_value={"state": state})
        client.retrigger_auth = AsyncMock(
            side_effect=AssertionError("unexpected_login")
        )
        client.close = AsyncMock()
        flow._client = client
        flow.async_abort = Mock(side_effect=lambda **kwargs: kwargs)
        return flow, client

    async def test_unready_states_never_request_login_in_setup_or_reauth(self):
        for source in ("user", "reauth"):
            for state, reason in (
                ("pending", "auth_pending"),
                ("blocked", "auth_blocked"),
                ("cooldown", "auth_cooldown"),
                ("failed", "auth_not_ready"),
                (None, "auth_not_ready"),
            ):
                with self.subTest(source=source, state=state):
                    flow, client = self.make_flow(state, source)
                    result = await flow._continue_auth()
                    self.assertEqual(result, {"reason": reason})
                    client.auth_status.assert_awaited_once()
                    client.close.assert_awaited_once()
                    client.retrigger_auth.assert_not_awaited()
                    self.assertIsNone(flow._client)

    async def test_challenges_route_without_fresh_login(self):
        for state, method in (
            ("require_2fa", "async_step_twofa"),
            ("require_captcha", "async_step_captcha"),
        ):
            flow, client = self.make_flow(state)
            handler = AsyncMock(return_value={"challenge": state})
            setattr(flow, method, handler)
            self.assertEqual(await flow._continue_auth(), {"challenge": state})
            handler.assert_awaited_once()
            client.retrigger_auth.assert_not_awaited()
            client.close.assert_not_awaited()

    async def test_explicit_resend_requests_once_then_stops_when_pending(self):
        for method, field in (
            ("async_step_twofa", "resend"),
            ("async_step_captcha", "refresh"),
        ):
            flow, client = self.make_flow("pending")
            client.retrigger_auth = AsyncMock(return_value={"state": "pending"})
            result = await getattr(flow, method)({field: True})
            self.assertEqual(result, {"reason": "auth_pending"})
            client.retrigger_auth.assert_awaited_once()
            client.close.assert_awaited_once()

    async def test_explicit_challenge_submission_preserved(self):
        for method, field, submit in (
            ("async_step_twofa", "code", "submit_2fa"),
            ("async_step_captcha", "answer", "submit_captcha"),
        ):
            flow, client = self.make_flow("ok")
            handler = AsyncMock(return_value={"state": "ok"})
            setattr(client, submit, handler)
            flow.async_create_entry = Mock(return_value={"created": True})
            self.assertEqual(
                await getattr(flow, method)({field: "synthetic"}), {"created": True}
            )
            handler.assert_awaited_once_with("synthetic")
            client.retrigger_auth.assert_not_awaited()
            client.close.assert_awaited_once()

    async def test_auth_status_failure_logs_only_fixed_reason(self):
        flow, client = self.make_flow("pending")
        client.auth_status.side_effect = EufySdkApiClientError(
            "synthetic-private-marker"
        )
        with patch("custom_components.eufy_sdk.config_flow.LOGGER") as logger:
            result = await flow._continue_auth()
        self.assertEqual(result, {"reason": "cannot_connect"})
        logger.error.assert_called_once_with(
            "auth.status failed [auth_status_unavailable]"
        )
        client.close.assert_awaited_once()
        client.retrigger_auth.assert_not_awaited()
