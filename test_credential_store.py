import json
import os
import unittest
from unittest.mock import patch

import requests

from api_client import DeepSeekClient
from credential_store import load_credentials, save_credentials


class CredentialCacheTests(unittest.TestCase):
    def test_explicit_token_does_not_inherit_unrelated_saved_session_state(self) -> None:
        vault = {"token": "account-a-token", "hif_leim": "account-a-proof",
                 "device_id": "account-a-device", "cookies": {"ds_session_id": "account-a-cookie"}}
        with patch("credential_store.load_credentials", return_value=vault), \
             patch.dict(os.environ, {"DEEPSEEK_DEVICE_ID": "", "DEEPSEEK_HIF_LEIM": "",
                                  "DEEPSEEK_COOKIES": ""}):
            client = DeepSeekClient(token="account-b-token")
        try:
            self.assertNotEqual(client.device_id, "account-a-device")
            self.assertEqual(client.hif_leim, "")
            self.assertEqual(client.session.cookies.get_dict(), {})
            self.assertEqual(client._headers()["authorization"], "Bearer account-b-token")
            self.assertNotIn("x-hif-leim", client._headers())
        finally:
            client.session.close()

    def test_matching_saved_token_reuses_proof_and_cookies(self) -> None:
        vault = {"token": "same-token", "hif_leim": "valid-proof",
                 "device_id": "valid-device", "cookies": {"ds_session_id": "valid-cookie"}}
        with patch("credential_store.load_credentials", return_value=vault), \
             patch.dict(os.environ, {"DEEPSEEK_DEVICE_ID": "", "DEEPSEEK_HIF_LEIM": "",
                                  "DEEPSEEK_COOKIES": ""}):
            client = DeepSeekClient(token="same-token")
        try:
            self.assertEqual(client.device_id, "valid-device")
            self.assertEqual(client.hif_leim, "valid-proof")
            self.assertEqual(client.session.cookies.get_dict(), {"ds_session_id": "valid-cookie"})
        finally:
            client.session.close()

    def test_missing_vault_token_recovers_without_browser(self) -> None:
        saved = {}

        def recover() -> bool:
            saved["token"] = "recovered-from-local-storage"
            return True

        with patch.dict(os.environ, {"DEEPSEEK_TOKEN": "", "DEEPSEEK_TOKEN_FILE": ""}):
            with patch("credential_store.load_credentials", side_effect=lambda: dict(saved)):
                with patch("import_existing_session.try_import", side_effect=recover) as importer:
                    client = DeepSeekClient()
        self.assertEqual(client.token, "recovered-from-local-storage")
        importer.assert_called_once()
        client.session.close()

    def test_save_and_reload_from_os_keyring(self) -> None:
        stored: dict[str, str] = {}
        with patch("keyring.set_password", side_effect=lambda _s, _a, value: stored.update(raw=value)):
            self.assertTrue(save_credentials("test-token", device_id="test-device"))
        with patch("keyring.get_password", return_value=stored["raw"]):
            self.assertEqual(load_credentials()["token"], "test-token")
            with patch.dict(os.environ, {"DEEPSEEK_TOKEN": "", "DEEPSEEK_TOKEN_FILE": ""}):
                client = DeepSeekClient()
        self.assertEqual(client.token, "test-token")
        self.assertEqual(client.device_id, "test-device")
        client.session.close()

    def test_only_successful_request_persists_new_token(self) -> None:
        client = DeepSeekClient(token="test-token")
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps({"code": 0, "data": {"biz_data": {}}}).encode()
        with patch.object(client.session, "post", return_value=response):
            with patch("credential_store.save_credentials", return_value=True) as save:
                client._post_json("/api/v0/chat_session/create", {})
        save.assert_called_once()
        self.assertFalse(client._persist_auth_on_success)
        client.session.close()

    def test_expired_session_refreshes_proof_when_token_is_unchanged(self) -> None:
        """A Comet session may rotate proof/device without rotating the bearer."""
        saved = {"token": "same-token", "hif_leim": "old-proof",
                 "device_id": "old-device", "cookies": {"ds_session_id": "old-cookie"}}
        with patch("credential_store.load_credentials", side_effect=lambda: dict(saved)):
            client = DeepSeekClient(token="same-token")
            try:
                def reimport() -> bool:
                    saved.update(hif_leim="new-proof", device_id="new-device",
                                 cookies={"ds_session_id": "new-cookie"})
                    return True

                with patch.object(client, "_login_with_saved_credentials", return_value=False), \
                     patch("import_existing_session.try_import", side_effect=reimport):
                    self.assertTrue(client._reload_token())
                self.assertEqual(client.token, "same-token")
                self.assertEqual(client.hif_leim, "new-proof")
                self.assertEqual(client.device_id, "new-device")
                self.assertEqual(client.session.cookies.get("ds_session_id"), "new-cookie")
            finally:
                client.session.close()

    def test_api_auth_rejection_retries_with_renewed_comet_proof(self) -> None:
        """An API 40003 should retry with the refreshed proof, not reject the session."""
        saved = {"token": "same-token", "hif_leim": "old-proof",
                 "device_id": "old-device", "cookies": {}}
        seen_headers = []

        def post(_url, *, headers, **_kwargs):
            seen_headers.append(headers)
            response = requests.Response()
            response.status_code = 200
            response._content = json.dumps(
                {"code": 40003, "msg": "expired"} if len(seen_headers) == 1
                else {"code": 0, "data": {"biz_data": {"session_id": "recovered"}}}
            ).encode()
            return response

        def reimport():
            saved["hif_leim"] = "new-proof"
            return True

        with patch("credential_store.load_credentials", side_effect=lambda: dict(saved)):
            client = DeepSeekClient(token="same-token")
            try:
                with patch.object(client.session, "post", side_effect=post), \
                     patch.object(client, "_login_with_saved_credentials", return_value=False), \
                     patch("import_existing_session.try_import", side_effect=reimport):
                    result = client._post_json("/api/v0/chat_session/create", {})
                self.assertEqual(result, {"session_id": "recovered"})
                self.assertEqual([h["x-hif-leim"] for h in seen_headers],
                                 ["old-proof", "new-proof"])
            finally:
                client.session.close()


if __name__ == "__main__":
    unittest.main()
