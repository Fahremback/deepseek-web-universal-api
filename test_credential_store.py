import json
import os
import unittest
from unittest.mock import patch

import requests

from api_client import DeepSeekClient
from credential_store import load_credentials, save_credentials


class CredentialCacheTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
