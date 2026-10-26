"""Deterministic HTTP login and expired-token retry tests; no real passwords."""

import io
import json
import os
import unittest
from unittest.mock import patch

import requests

from api_client import AuthenticationError, DeepSeekAPIError, DeepSeekClient, LOGIN_PATH, main
from credential_store import load_login_credentials, save_login_credentials


def reply(code=0, token=None, status=200):
    r = requests.Response()
    r.status_code = status
    r.raw = io.BytesIO()
    data = {"code": code, "data": {"biz_code": 0, "biz_data": {}}}
    if token is not None:
        data["data"]["biz_data"]["user"] = {"token": token}
    r._content = json.dumps(data).encode()
    return r


class HttpLoginTests(unittest.TestCase):
    def test_http_200_html_challenge_is_not_treated_as_success(self):
        """An intermediary's challenge page must not be accepted as API data."""
        client = DeepSeekClient(token="fake-token")
        html = requests.Response()
        html.status_code = 200
        html.headers["Content-Type"] = "text/html; charset=utf-8"
        html._content = b"<html><title>Verification required</title></html>"
        html.raw = io.BytesIO()
        with patch.object(client.session, "post", return_value=html), \
             patch.object(client, "_persist_valid_credentials") as persist, \
             patch.object(client, "_reload_token") as refresh:
            with self.assertRaises(DeepSeekAPIError):
                client._post_json("/api/v0/chat_session/create", {})
            persist.assert_not_called()
            refresh.assert_not_called()
        client.session.close()

    def test_http_200_non_object_json_is_not_treated_as_success(self):
        client = DeepSeekClient(token="fake-token")
        malformed = reply()
        malformed._content = b'["unexpected", "list"]'
        with patch.object(client.session, "post", return_value=malformed), \
             patch.object(client, "_persist_valid_credentials") as persist:
            with self.assertRaises(DeepSeekAPIError):
                client._post_json("/api/v0/chat_session/create", {})
            persist.assert_not_called()
        client.session.close()

    def test_json_object_with_raw_metadata_is_still_accepted(self):
        client = DeepSeekClient(token="fake-token")
        valid = reply()
        valid._content = json.dumps({"raw": "metadata", "code": 0,
                                     "data": {"biz_code": 0, "biz_data": {"ok": True}}}).encode()
        with patch.object(client.session, "post", return_value=valid):
            self.assertEqual(client._post_json("/api/v0/test", {}), {"ok": True})
        client.session.close()

    def test_stream_html_challenge_is_not_retried_or_echoed(self):
        client = DeepSeekClient(token="fake-token")
        html = requests.Response()
        html.status_code = 200
        html.headers["Content-Type"] = "text/html; charset=utf-8"
        html._content = b"<html>login challenge SECRET_PRIVATE_BODY</html>"
        html.raw = io.BytesIO()
        with patch.object(client, "create_pow_challenge", return_value={}), \
             patch("api_client.solve_pow", return_value=1), \
             patch("api_client.build_pow_header", return_value="pow"), \
             patch.object(client.session, "post", return_value=html) as post, \
             patch.object(client, "_reload_token") as reload:
            with self.assertRaises(DeepSeekAPIError) as failure:
                client.create_completion_stream(chat_session_id="offline", prompt="test")
            self.assertNotIn("SECRET_PRIVATE_BODY", str(failure.exception))
            reload.assert_not_called()
            post.assert_called_once()
        client.session.close()

    def test_failed_completion_closes_stream_response(self):
        client = DeepSeekClient(token="fake-token")
        response = reply(status=429)
        with patch.object(client, "create_pow_challenge", return_value={}), \
             patch("api_client.solve_pow", return_value=1), \
             patch("api_client.build_pow_header", return_value="pow"), \
             patch.object(client.session, "post", return_value=response), \
             patch.object(response, "close", wraps=response.close) as close_response:
            with self.assertRaises(DeepSeekAPIError):
                client.create_completion_stream(chat_session_id="test-session", prompt="test")
            close_response.assert_called_once()
        client.session.close()

    def test_json_requests_close_response_on_success_and_failure(self):
        client = DeepSeekClient(token="fake-token")
        success = reply()
        failure = reply(status=429)
        with patch.object(client.session, "post", side_effect=[success, failure]), \
             patch.object(success, "close", wraps=success.close) as close_success, \
             patch.object(failure, "close", wraps=failure.close) as close_failure:
            self.assertEqual(client._post_json("/api/v0/test", {}), {})
            with self.assertRaises(DeepSeekAPIError):
                client._post_json("/api/v0/test", {})
            close_success.assert_called_once()
            close_failure.assert_called_once()
        client.session.close()

    def test_vault_login_roundtrip(self):
        storage = {}
        with patch("keyring.set_password", side_effect=lambda _s, name, value: storage.update({name: value})):
            self.assertTrue(save_login_credentials(email="test@example.invalid", password="private"))
        with patch("keyring.get_password", side_effect=lambda _s, name: storage.get(name)):
            self.assertEqual(load_login_credentials()["password"], "private")

    def test_login_http_persists_both_credentials_and_omits_bearer(self):
        client = DeepSeekClient(token="", allow_unauthed=True)
        with patch.object(client.session, "post", return_value=reply(token="new-token")) as post:
            with patch("credential_store.save_credentials", return_value=True) as save_token:
                with patch("credential_store.save_login_credentials", return_value=True) as save_login:
                    client.login(email="person@example.invalid", password="secret")
        self.assertEqual(client.token, "new-token")
        args, kwargs = post.call_args
        self.assertEqual(args[0], f"https://chat.deepseek.com{LOGIN_PATH}")
        self.assertEqual(kwargs["json"]["password"], "secret")
        self.assertNotIn("authorization", kwargs["headers"])
        save_token.assert_called_once()
        save_login.assert_called_once()
        client.session.close()

    def test_rejects_login_without_persisting(self):
        client = DeepSeekClient(token="", allow_unauthed=True)
        with patch.object(client.session, "post", return_value=reply(code=40003)):
            with patch("credential_store.save_credentials") as save:
                with self.assertRaises(AuthenticationError):
                    client.login(email="person@example.invalid", password="bad")
                save.assert_not_called()
        client.session.close()

    def test_401_renews_and_retries_exactly_once(self):
        client = DeepSeekClient(token="old-token")
        hits = []

        def send(url, **kwargs):
            hits.append((url, kwargs))
            if url.endswith(LOGIN_PATH):
                return reply(token="new-token")
            return reply(status=401 if len([x for x in hits if not x[0].endswith(LOGIN_PATH)]) == 1 else 200)

        with patch.object(client.session, "post", side_effect=send):
            with patch("credential_store.load_credentials", return_value={"token": "old-token"}):
                with patch("credential_store.load_login_credentials", return_value={"email": "x@example.invalid", "password": "secret"}):
                    with patch("credential_store.save_credentials", return_value=True):
                        self.assertEqual(client._post_json("/api/v0/chat_session/create", {"foo": 1}), {})
        self.assertEqual(len(hits), 3)
        self.assertEqual(hits[0][1]["headers"]["authorization"], "Bearer old-token")
        self.assertEqual(hits[2][1]["headers"]["authorization"], "Bearer new-token")
        client.session.close()

    def test_401_recovers_comet_session_without_account_password(self):
        client = DeepSeekClient(token="expired-token")
        vault = {"token": "expired-token"}
        calls = []

        def send(url, **kwargs):
            calls.append(kwargs["headers"]["authorization"])
            return reply(status=401 if len(calls) == 1 else 200)

        def import_comet():
            vault.update({"token": "new-comet-token", "hif_leim": "new-proof",
                          "device_id": "new-device", "cookies": {}})
            return True

        with patch.object(client.session, "post", side_effect=send), patch(
            "credential_store.load_credentials", side_effect=lambda: vault.copy()
        ), patch("credential_store.load_login_credentials", return_value={}), patch(
            "import_existing_session.try_import", side_effect=import_comet
        ) as importer:
            self.assertEqual(client._post_json("/api/v0/chat_session/create", {}), {})

        importer.assert_called_once()
        self.assertEqual(calls, ["Bearer expired-token", "Bearer new-comet-token"])
        self.assertEqual(client.device_id, "new-device")
        self.assertEqual(client.hif_leim, "new-proof")
        client.session.close()

    def test_vault_token_rotation_discards_cookies_from_previous_account(self):
        client = DeepSeekClient(token="old-token")
        client.session.cookies.set("ds_session_id", "previous-account")
        with patch("credential_store.load_credentials", return_value={
            "token": "rotated-token", "hif_leim": "rotated-proof",
            "device_id": "rotated-device", "cookies": {"ds_session_id": "current-account"},
        }):
            self.assertTrue(client._reload_token())
        self.assertEqual(client.token, "rotated-token")
        self.assertEqual(client.session.cookies.get_dict(), {"ds_session_id": "current-account"})
        self.assertEqual(client.hif_leim, "rotated-proof")
        client.session.close()

    def test_vault_token_rotation_with_no_cookies_clears_previous_account(self):
        client = DeepSeekClient(token="old-token")
        client.session.cookies.set("ds_session_id", "previous-account")
        with patch("credential_store.load_credentials", return_value={
            "token": "rotated-token", "hif_leim": "", "cookies": {},
        }):
            self.assertTrue(client._reload_token())
        self.assertEqual(client.session.cookies.get_dict(), {})
        self.assertEqual(client.hif_leim, "")
        client.session.close()

    def test_environment_token_rotation_discards_old_session_state(self):
        client = DeepSeekClient(token="old-token", hif_leim="old-proof")
        client.session.cookies.set("ds_session_id", "old-account")
        with patch("credential_store.load_credentials", return_value={"token": "old-token"}), patch(
            "credential_store.load_login_credentials", return_value={}
        ), patch("import_existing_session.try_import", return_value=False), patch(
            "api_client._read_token", return_value="different-token"
        ):
            self.assertTrue(client._reload_token())
        self.assertEqual(client.token, "different-token")
        self.assertEqual(client.hif_leim, "")
        self.assertEqual(client.session.cookies.get_dict(), {})
        client.session.close()

    def test_second_401_does_not_login_again(self):
        client = DeepSeekClient(token="old-token")
        requests_made = []

        def send(url, **kwargs):
            requests_made.append(url)
            return reply(token="fresh-token") if url.endswith(LOGIN_PATH) else reply(status=401)

        with patch.object(client.session, "post", side_effect=send):
            with patch("credential_store.load_credentials", return_value={"token": "old-token"}):
                with patch("credential_store.load_login_credentials", return_value={"email": "x@example.invalid", "password": "secret"}):
                    with patch("credential_store.save_credentials", return_value=True):
                        with self.assertRaises(AuthenticationError):
                            client._post_json("/api/v0/chat_session/create", {})
        self.assertEqual(len(requests_made), 3)
        self.assertEqual(sum(url.endswith(LOGIN_PATH) for url in requests_made), 1)
        client.session.close()

    def test_http_200_with_expired_token_business_code_also_refreshes(self):
        client = DeepSeekClient(token="expired")
        response = reply()
        response._content = json.dumps({"code": 40003, "msg": "expired", "data": {}}).encode()
        called = []

        def send(url, **kwargs):
            called.append(url)
            if url.endswith(LOGIN_PATH):
                return reply(token="valid")
            return response if len(called) == 1 else reply()

        with patch.object(client.session, "post", side_effect=send):
            with patch("credential_store.load_credentials", return_value={"token": "expired"}):
                with patch("credential_store.load_login_credentials", return_value={"email": "u@example.invalid", "password": "secret"}):
                    with patch("credential_store.save_credentials", return_value=True):
                        self.assertEqual(client._post_json("/api/v0/chat_session/create", {}), {})
        self.assertEqual(len(called), 3)
        self.assertEqual(client.token, "valid")
        client.session.close()

    def test_cli_login_from_stdin(self):
        with patch("sys.stdin", io.StringIO("not-a-real-password\n")):
            with patch.object(DeepSeekClient, "login") as login:
                with patch("builtins.print"):
                    self.assertEqual(main(["--login", "--email", "user@example.invalid", "--password-stdin"]), 0)
        self.assertEqual(login.call_args.kwargs["password"], "not-a-real-password")


if __name__ == "__main__":
    unittest.main()
