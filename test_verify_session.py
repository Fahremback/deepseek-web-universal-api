"""Offline tests for the explicit first-party live-session preflight."""

from __future__ import annotations

import contextlib
import io
import json
import unittest
from unittest.mock import MagicMock, patch

from verify_session import live_probe, local_status, main, verify_authorization


class SessionVerificationTests(unittest.TestCase):
    def test_default_command_is_offline_and_prints_no_secret(self):
        output = io.StringIO()
        with patch("verify_session.load_credentials", return_value={"token": "PRIVATE_TOKEN"}), \
             patch("verify_session.load_login_credentials", return_value={"password": "SECRET"}), \
             patch("verify_session.DeepSeekClient", side_effect=AssertionError("must stay offline")), \
             contextlib.redirect_stdout(output):
            self.assertEqual(main([]), 0)
        self.assertEqual(json.loads(output.getvalue()), {
            "saved_session": True, "saved_password_login": True,
        })

    def test_invalid_session_does_not_generate(self):
        response = MagicMock(status_code=403)
        client = MagicMock(base_url="https://chat.deepseek.com", timeout=2)
        client.session.get.return_value = response
        with patch("verify_session.load_credentials", return_value={"token": "old"}):
            report = live_probe(generate=True, factory=lambda **_kwargs: client)
        self.assertEqual(report, {"authorization": "rejected", "http_status": 403})
        client.chat.assert_not_called()
        response.close.assert_called_once()
        client.session.close.assert_called_once()

    def test_expired_vault_session_recovers_from_comet_without_browser(self):
        old = MagicMock(base_url="https://chat.deepseek.com", timeout=10)
        fresh = MagicMock(base_url="https://chat.deepseek.com", timeout=10)
        result = [
            {"authorization": "rejected", "http_status": 200, "api_code": 40003},
            {"authorization": "accepted", "http_status": 200},
        ]
        tokens = []

        def factory(**kwargs):
            tokens.append(kwargs["token"])
            return old if len(tokens) == 1 else fresh

        fresh.chat.return_value.status = "FINISHED"
        fresh.chat.return_value.text = "OK"
        with patch("verify_session.load_credentials", side_effect=[
            {"token": "expired"}, {"token": "comet-refreshed"},
        ]), patch("verify_session.verify_authorization", side_effect=result) as verify, patch(
            "import_existing_session.try_import", return_value=True
        ) as import_comet:
            report = live_probe(generate=True, recover=True, factory=factory)
        self.assertEqual(report, {"authorization": "accepted", "http_status": 200,
                                  "generation": "accepted"})
        self.assertEqual(tokens, ["expired", "comet-refreshed"])
        self.assertEqual(verify.call_count, 2)
        import_comet.assert_called_once()
        old.session.close.assert_called_once()
        fresh.session.close.assert_called_once()

    def test_cli_live_enables_automatic_comet_recovery(self):
        with patch("verify_session.live_probe", return_value={
            "authorization": "accepted", "http_status": 200,
        }) as probe, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--live"]), 0)
        probe.assert_called_once_with(generate=False, recover=True)

    def test_http_200_with_business_error_rejected(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {"code": 0, "data": {"biz_code": 40003}}
        client = MagicMock(base_url="https://chat.deepseek.com", timeout=10)
        client.session.get.return_value = response
        self.assertEqual(verify_authorization(client), {
            "authorization": "rejected", "http_status": 200,
            "reason": "biz_code_rejected", "biz_code": 40003,
        })
        response.close.assert_called_once()

    def test_http_200_with_api_error_reports_code_without_message(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {
            "code": 401, "message": "SECRET MESSAGE", "data": {"email": "private@example.com"},
        }
        client = MagicMock(base_url="https://chat.deepseek.com", timeout=10)
        client.session.get.return_value = response
        report = verify_authorization(client)
        self.assertEqual(report, {
            "authorization": "rejected", "http_status": 200,
            "reason": "api_code_rejected", "api_code": 401,
        })
        self.assertNotIn("SECRET", json.dumps(report))
        response.close.assert_called_once()

    def test_unknown_json_type_is_not_authentication_success(self):
        response = MagicMock(status_code=200)
        response.json.return_value = ["not an account"]
        client = MagicMock(base_url="https://chat.deepseek.com", timeout=10)
        client.session.get.return_value = response
        self.assertEqual(verify_authorization(client)["reason"], "unexpected_response")
        response.close.assert_called_once()

    def test_valid_session_and_one_short_generation(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {"code": 0, "data": {"biz_code": 0}}
        client = MagicMock(base_url="https://chat.deepseek.com", timeout=10)
        client.session.get.return_value = response
        client.chat.return_value.status = "FINISHED"
        client.chat.return_value.text = "OK"
        with patch("verify_session.load_credentials", return_value={"token": "new"}):
            report = live_probe(generate=True, factory=lambda **_kwargs: client)
        self.assertEqual(report["generation"], "accepted")
        client.chat.assert_called_once()
        self.assertFalse(client.chat.call_args.kwargs["search_enabled"])
        response.close.assert_called_once()

    def test_missing_token_stops_without_client(self):
        with patch("verify_session.load_credentials", return_value={}), \
             patch("verify_session.DeepSeekClient", side_effect=AssertionError("no client")):
            self.assertEqual(live_probe(generate=True), {"authorization": "missing_session"})

    def test_browser_login_and_generation_are_one_explicit_flow(self):
        output = io.StringIO()
        with patch("browser_login.login_in_browser", return_value=True) as login, \
             patch("verify_session.live_probe", return_value={
                 "authorization": "accepted", "http_status": 200,
                 "generation": "accepted",
             }) as probe, contextlib.redirect_stdout(output):
            self.assertEqual(main(["--login-web", "--generate"]), 0)
        login.assert_called_once_with()
        probe.assert_called_once_with(generate=True)
        self.assertEqual(json.loads(output.getvalue())["generation"], "accepted")

    def test_browser_login_can_select_firefox_without_affecting_default(self):
        with patch("browser_login.login_in_browser", return_value=True) as login, \
             patch("verify_session.live_probe", return_value={
                 "authorization": "accepted", "http_status": 200,
             }) as probe, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--login-web", "--login-browser", "firefox"]), 0)
        login.assert_called_once_with(browser_type="firefox")
        probe.assert_called_once_with(generate=False)

    def test_browser_login_failure_cannot_reuse_old_session(self):
        output = io.StringIO()
        with patch("browser_login.login_in_browser", return_value=False), \
             patch("verify_session.live_probe") as probe, \
             contextlib.redirect_stdout(output):
            self.assertEqual(main(["--login-web", "--generate"]), 1)
        probe.assert_not_called()
        self.assertEqual(json.loads(output.getvalue()), {
            "authorization": "login_not_completed",
        })


if __name__ == "__main__":
    unittest.main()
