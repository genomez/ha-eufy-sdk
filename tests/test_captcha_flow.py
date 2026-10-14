# ruff: noqa: ANN201, ANN202, D100, D101, D102, INP001, PT009, SLF001

import unittest
from unittest.mock import AsyncMock, Mock, patch

from custom_components.eufy_sdk import config_flow


class CaptchaStepTests(unittest.IsolatedAsyncioTestCase):
    def _flow(self, after_submit: str):
        flow = config_flow.EufySdkFlowHandler()
        flow.hass = Mock()
        flow.context = {"source": "user"}
        client = Mock()
        client.submit_captcha = AsyncMock(return_value={"state": after_submit})
        client.auth_status = AsyncMock(return_value={"state": after_submit})
        client.close = AsyncMock()
        client.retrigger_auth = AsyncMock()
        flow._client = client
        flow.async_show_form = Mock(side_effect=lambda **kw: kw)
        flow.async_abort = Mock(side_effect=lambda **kw: kw)
        flow.async_create_entry = Mock(side_effect=lambda **kw: kw)
        return flow, client

    async def test_accepted_captcha_leads_to_twofa_without_new_login(self):
        flow, client = self._flow("require_2fa")
        result = await flow.async_step_captcha({"answer": "SYNTHETIC"})
        self.assertEqual(result["step_id"], "twofa")
        self.assertEqual(result["errors"], {})
        client.submit_captcha.assert_awaited_once_with("SYNTHETIC")
        client.retrigger_auth.assert_not_awaited()
        client.close.assert_not_awaited()

    async def test_repeated_captcha_keeps_invalid_answer_error(self):
        flow, client = self._flow("require_captcha")
        result = await flow.async_step_captcha({"answer": "WRONG"})
        self.assertEqual(result["step_id"], "captcha")
        self.assertEqual(result["errors"], {"base": "invalid_captcha"})
        client.retrigger_auth.assert_not_awaited()

    async def test_accepted_captcha_can_complete_initial_setup(self):
        flow, client = self._flow("ok")
        result = await flow.async_step_captcha({"answer": "SYNTHETIC"})
        self.assertIn("data", result)
        client.close.assert_awaited_once()
        client.retrigger_auth.assert_not_awaited()
        self.assertIsNone(flow._client)

    async def test_accepted_captcha_completes_reauth_without_duplicate_entry(self):
        flow, client = self._flow("ok")
        flow.context = {"source": config_flow.config_entries.SOURCE_REAUTH}
        entry = Mock()
        flow._get_reauth_entry = Mock(return_value=entry)
        flow.async_update_reload_and_abort = Mock(return_value={"reason": "done"})
        result = await flow.async_step_captcha({"answer": "SYNTHETIC"})
        self.assertEqual(result, {"reason": "done"})
        flow.async_update_reload_and_abort.assert_called_once_with(
            entry, data_updates={}
        )
        flow.async_create_entry.assert_not_called()
        client.close.assert_awaited_once()
        client.retrigger_auth.assert_not_awaited()

    async def test_terminal_bridge_states_do_not_start_login_retries(self):
        expected = {
            "pending": "auth_pending",
            "blocked": "auth_blocked",
            "cooldown": "auth_cooldown",
            "other": "auth_not_ready",
        }
        for state, reason in expected.items():
            with self.subTest(state=state):
                flow, client = self._flow("require_2fa")
                # The bridge may transition again between submit and status.
                client.auth_status.return_value = {"state": state}
                result = await flow.async_step_captcha({"answer": "SYNTHETIC"})
                self.assertEqual(result, {"reason": reason})
                client.close.assert_awaited_once()
                client.retrigger_auth.assert_not_awaited()
                self.assertIsNone(flow._client)

    async def test_status_failure_logs_category_without_exception_payload(self):
        flow, client = self._flow("require_2fa")
        client.auth_status.side_effect = config_flow.EufySdkApiClientError(
            "SYNTHETIC_SENSITIVE_PAYLOAD"
        )
        with patch.object(config_flow.LOGGER, "error") as log:
            result = await flow.async_step_captcha({"answer": "SYNTHETIC"})
        self.assertEqual(result, {"reason": "cannot_connect"})
        log.assert_called_once_with("auth.status failed [auth_status_unavailable]")
        client.close.assert_awaited_once()
        client.retrigger_auth.assert_not_awaited()

    async def test_missing_client_aborts_without_submission(self):
        flow, client = self._flow("require_2fa")
        flow._client = None
        result = await flow.async_step_captcha({"answer": "SYNTHETIC"})
        self.assertEqual(result, {"reason": "cannot_connect"})
        client.submit_captcha.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
