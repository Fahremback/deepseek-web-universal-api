"""Web login tests exercise official-origin capture without opening a browser."""

import json
import unittest
from unittest.mock import MagicMock, patch

from browser_login import ORIGIN, capture_session, login_in_browser, parse_storage_value, profile_directory
from api_client import main


class BrowserLoginTests(unittest.TestCase):
    def test_unwraps_deepseek_storage(self):
        self.assertEqual(parse_storage_value('"raw-token"'), "raw-token")
        self.assertEqual(parse_storage_value(json.dumps({"value": "nested-token"})), "nested-token")
        self.assertEqual(parse_storage_value("plain-token"), "plain-token")
        self.assertEqual(parse_storage_value(json.dumps({"bad": "field"})), "")

    def test_verifies_and_saves_token_without_password(self):
        page = MagicMock(url=f"{ORIGIN}/")
        page.evaluate.side_effect = [
            {"token": '{"value":"test-token"}', "proof": None, "device": None},
            True,
        ]
        context = MagicMock()
        context.cookies.return_value = [
            {"name": "ds_session_id", "value": "session"},
            {"name": "other-site-data", "value": "must-not-save"},
        ]
        with patch("credential_store.save_credentials", return_value=True) as saved:
            self.assertTrue(capture_session(page, context))
        self.assertEqual(page.evaluate.call_args_list[1].args[1], "test-token")
        self.assertEqual(saved.call_args.args[0], "test-token")
        self.assertEqual(saved.call_args.kwargs["cookies"], {"ds_session_id": "session"})

    def test_foreign_origin_and_invalid_auth_are_not_saved(self):
        page = MagicMock(url="https://not-deepseek.example/sign_in")
        context = MagicMock()
        with patch("credential_store.save_credentials") as save:
            self.assertFalse(capture_session(page, context))
            save.assert_not_called()
        page.url = f"{ORIGIN}/"
        page.evaluate.side_effect = [{"token": "expired"}, False]
        with patch("credential_store.save_credentials") as save:
            self.assertFalse(capture_session(page, context))
            save.assert_not_called()

    def test_browser_window_opens_real_site_and_closes(self):
        page = MagicMock(url=f"{ORIGIN}/sign_in")
        page.is_closed.return_value = False
        context = MagicMock()
        context.pages = [page]
        manager = MagicMock()
        manager.__enter__.return_value = MagicMock()
        manager.__enter__.return_value.chromium.launch_persistent_context.return_value = context
        with patch("playwright.sync_api.sync_playwright", return_value=manager):
            with patch("browser_login.capture_session", return_value=True):
                self.assertTrue(login_in_browser(timeout_seconds=3, browser_type="chrome"))
        self.assertEqual(page.goto.call_args.args[0], f"{ORIGIN}/sign_in")
        self.assertFalse(context.new_page.called)
        context.close.assert_called_once()

    def test_managed_firefox_uses_its_engine_and_independent_profile(self):
        for engine_name in ("firefox", "webkit", "chromium"):
            with self.subTest(engine=engine_name):
                page = MagicMock(url=f"{ORIGIN}/sign_in")
                page.is_closed.return_value = False
                context = MagicMock(pages=[page])
                playwright = MagicMock()
                getattr(playwright, engine_name).launch_persistent_context.return_value = context
                manager = MagicMock()
                manager.__enter__.return_value = playwright
                with patch("playwright.sync_api.sync_playwright", return_value=manager), \
                     patch("browser_login.capture_session", return_value=True):
                    self.assertTrue(login_in_browser(timeout_seconds=3, browser_type=engine_name))
                engine = getattr(playwright, engine_name)
                self.assertEqual(engine.launch_persistent_context.call_count, 1)
                arguments = engine.launch_persistent_context.call_args.kwargs
                self.assertNotIn("channel", arguments)
                self.assertTrue(arguments["user_data_dir"].endswith(f"browser-profile-{engine_name}"))
                if engine_name != "chromium":
                    self.assertNotIn("args", arguments)
                context.close.assert_called_once()

    def test_profile_paths_follow_os_data_directories(self):
        with patch("browser_login.sys.platform", "darwin"), patch("browser_login.Path.home", return_value=__import__("pathlib").Path("/Users/example")):
            self.assertEqual(str(profile_directory("firefox")).replace("\\", "/"), "/Users/example/Library/Application Support/DeepSeekWebClient/browser-profile-firefox")
        with patch("browser_login.sys.platform", "linux"), patch.dict("browser_login.os.environ", {"XDG_DATA_HOME": "/tmp/userdata"}):
            self.assertEqual(str(profile_directory("webkit")).replace("\\", "/"), "/tmp/userdata/DeepSeekWebClient/browser-profile-webkit")

    def test_chrome_edge_and_other_managed_browsers_have_distinct_profiles(self):
        paths = {kind: profile_directory(kind) for kind in ("chrome", "msedge", "chromium", "firefox", "webkit")}
        self.assertEqual(len(set(paths.values())), len(paths))
        self.assertEqual(paths["chrome"].name, "browser-profile")  # existing Chrome users
        self.assertEqual(paths["msedge"].name, "browser-profile-msedge")

    def test_default_browser_opens_by_os_association_and_imports_session(self):
        with patch("browser_login.webbrowser.open", return_value=True) as open_browser:
            with patch("import_existing_session.try_import", side_effect=[False, False, True]) as importer:
                with patch("browser_login.time.sleep"):
                    self.assertTrue(login_in_browser(timeout_seconds=10))
        open_browser.assert_called_once_with(f"{ORIGIN}/sign_in", new=2)
        self.assertEqual(importer.call_count, 3)

    def test_reuses_valid_comet_session_without_opening_any_browser(self):
        with patch("browser_login.webbrowser.open") as open_browser:
            with patch("import_existing_session.try_import", return_value=True):
                self.assertTrue(login_in_browser(timeout_seconds=10))
        open_browser.assert_not_called()

    def test_default_browser_open_failure_is_reported(self):
        with patch("browser_login.webbrowser.open", return_value=False):
            with patch("import_existing_session.try_import", return_value=False) as importer:
                with self.assertRaisesRegex(RuntimeError, "default browser"):
                    login_in_browser(timeout_seconds=1)
                importer.assert_called_once()

    def test_comet_profile_is_discoverable(self):
        from import_existing_session import COMET_ROOT, PROFILE_PATHS
        self.assertEqual(PROFILE_PATHS[0], COMET_ROOT / "Default" / "Local Storage" / "leveldb")

    def test_cli_launches_browser_login_without_password(self):
        with patch("browser_login.login_in_browser", return_value=True) as login:
            with patch("builtins.print"):
                self.assertEqual(main(["--login-web", "--login-browser", "msedge"]), 0)
        login.assert_called_once_with(browser_type="msedge", timeout_seconds=300)

    def test_cli_defaults_to_system_browser(self):
        with patch("browser_login.login_in_browser", return_value=True) as login:
            with patch("builtins.print"):
                self.assertEqual(main(["--login-web"]), 0)
        login.assert_called_once_with(browser_type="default", timeout_seconds=300)


if __name__ == "__main__":
    unittest.main()
