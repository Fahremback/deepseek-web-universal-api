"""Offline Comet session-import tests; never use real browser secrets."""

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import import_existing_session as importer


class SessionImportTests(unittest.TestCase):
    def setUp(self):
        importer._LAST_CHECK.clear()

    def test_installed_reader_works_without_adjacent_tools_directory(self):
        installed = SimpleNamespace(LocalStoreDb=object())
        with tempfile.TemporaryDirectory() as root:
            with patch.object(importer, "CHROMIUM_READER", Path(root) / "missing-chromium"), \
                 patch.object(importer, "SNAPPY_READER", Path(root) / "missing-snappy"), \
                 patch.object(importer.importlib, "import_module", return_value=installed) as load:
                self.assertIs(importer._load_reader(), installed)
        load.assert_called_once_with("ccl_chromium_reader.ccl_chromium_localstorage")

    def test_missing_reader_reports_unavailable_without_crashing(self):
        with tempfile.TemporaryDirectory() as root:
            with patch.object(importer, "CHROMIUM_READER", Path(root) / "missing-chromium"), \
                 patch.object(importer, "SNAPPY_READER", Path(root) / "missing-snappy"), \
                 patch.object(importer.importlib, "import_module", side_effect=ModuleNotFoundError("missing")):
                with self.assertRaisesRegex(RuntimeError, "not installed"):
                    importer._load_reader()

    def test_discovers_comet_secondary_profiles_in_order(self):
        with tempfile.TemporaryDirectory() as root:
            comet_root = Path(root) / "Comet"
            profile_paths = [comet_root / "Default/Local Storage/leveldb"]
            for n in (3, 1):
                profile_paths.append(comet_root / f"Profile {n}/Local Storage/leveldb")
            for path in profile_paths:
                path.mkdir(parents=True)
            with patch.object(importer, "COMET_ROOT", comet_root), patch.object(
                importer, "PROFILE_PATHS", [profile_paths[0]]
            ):
                self.assertEqual(importer.candidate_profile_paths(), [
                    profile_paths[0], profile_paths[2], profile_paths[1],
                ])

    def test_chromium_browser_paths_are_cross_platform(self):
        examples = (
            ("win32", "C:/Users/example/AppData/Local", "Google/Chrome/User Data"),
            ("darwin", "/Users/example/Library/Application Support", "Google/Chrome"),
            ("linux", "/home/example/.config", "google-chrome"),
        )
        for platform, base, chrome_suffix in examples:
            with self.subTest(platform=platform):
                root = Path(base)
                env = {"LOCALAPPDATA": base} if platform == "win32" else (
                    {"XDG_CONFIG_HOME": base} if platform == "linux" else {})
                paths = importer.chromium_profile_roots(platform=platform, home=root, env=env)
                normalized = [str(p).replace("\\", "/") for p in paths]
                self.assertEqual(len(paths), 6)
                self.assertTrue(normalized[2].endswith(chrome_suffix))
                self.assertTrue(any("Brave" in p for p in normalized))

    def test_custom_chromium_profile_is_discoverable(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "MyProfile"
            leveldb = profile / "Local Storage" / "leveldb"
            leveldb.mkdir(parents=True)
            with patch.dict("import_existing_session.os.environ", {"DEEPSEEK_BROWSER_PROFILE": str(profile)}):
                self.assertIn(leveldb, importer.candidate_profile_paths())

    def test_saved_session_is_validated_with_gateway_headers(self):
        token = "CURRENT_USER_TOKEN"
        records = {
            "userToken": json.dumps({"value": token}),
            "hif_leim_cached": json.dumps({"value": "proof"}),
            "deviceId": json.dumps({"value": "device"}),
        }
        db = MagicMock()
        db.__enter__.return_value = db
        db.contains_storage_key.return_value = True
        db.contains_script_key.side_effect = lambda _origin, key: key in records
        db.iter_records_for_script_key.side_effect = lambda _origin, key: [
            SimpleNamespace(value=records[key], leveldb_seq_number=1)
        ]
        reader = SimpleNamespace(LocalStoreDb=lambda _path: db)
        with patch.object(importer, "_load_reader", return_value=reader), patch.object(
            importer, "candidate_profile_paths", return_value=[Path("test-profile")]
        ), patch.object(importer, "_verify_candidate", return_value=True) as verify, patch.object(
            importer, "save_credentials", return_value=True
        ) as save:
            self.assertTrue(importer.try_import())
            verify.assert_called_once_with(token, "proof", "device")
            save.assert_called_once_with(token, device_id="device", hif_leim="proof")

    def test_rejected_token_is_not_checked_on_every_poll(self):
        db = MagicMock()
        db.__enter__.return_value = db
        db.contains_storage_key.return_value = True
        db.contains_script_key.return_value = True
        db.iter_records_for_script_key.return_value = [
            SimpleNamespace(value="rejected-token", leveldb_seq_number=1)
        ]
        reader = SimpleNamespace(LocalStoreDb=lambda _path: db)
        with patch.object(importer, "_load_reader", return_value=reader), patch.object(
            importer, "candidate_profile_paths", return_value=[Path("test-profile")]
        ), patch.object(importer, "_verify_candidate", return_value=False) as verify:
            self.assertFalse(importer.try_import())
            self.assertFalse(importer.try_import())
            verify.assert_called_once()

    def test_valid_token_can_be_imported_twice_without_cooldown(self):
        db = MagicMock()
        db.__enter__.return_value = db
        db.contains_storage_key.return_value = True
        db.contains_script_key.side_effect = lambda _origin, key: key == "userToken"
        db.iter_records_for_script_key.return_value = [
            SimpleNamespace(value='{"value":"valid-token"}', leveldb_seq_number=1)
        ]
        reader = SimpleNamespace(LocalStoreDb=lambda _path: db)
        with patch.object(importer, "_load_reader", return_value=reader), patch.object(
            importer, "candidate_profile_paths", return_value=[Path("test-profile")]
        ), patch.object(importer, "_verify_candidate", return_value=True) as verify, patch.object(
            importer, "save_credentials", return_value=True
        ) as save:
            self.assertTrue(importer.try_import())
            self.assertTrue(importer.try_import())
            self.assertEqual(verify.call_count, 2)
            self.assertEqual(save.call_count, 2)

    def test_changed_proof_rechecks_same_token_without_cooldown(self):
        state = {"proof": "stale-proof"}
        db = MagicMock()
        db.__enter__.return_value = db
        db.contains_storage_key.return_value = True
        db.contains_script_key.side_effect = lambda _origin, key: key in {"userToken", "hif_leim_cached"}

        def records(_origin, key):
            value = "same-token" if key == "userToken" else state["proof"]
            return [SimpleNamespace(value=json.dumps({"value": value}), leveldb_seq_number=1)]

        db.iter_records_for_script_key.side_effect = records
        reader = SimpleNamespace(LocalStoreDb=lambda _path: db)
        with patch.object(importer, "_load_reader", return_value=reader), patch.object(
            importer, "candidate_profile_paths", return_value=[Path("test-profile")]
        ), patch.object(importer, "_verify_candidate", side_effect=[False, True]) as verify, patch.object(
            importer, "save_credentials", return_value=True
        ):
            self.assertFalse(importer.try_import())
            state["proof"] = "refreshed-proof"
            self.assertTrue(importer.try_import())
            self.assertEqual(verify.call_count, 2)


if __name__ == "__main__":
    unittest.main()
