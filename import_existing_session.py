"""Import an existing DeepSeek session from Chromium Local Storage, offline.

This one-time recovery utility does not start a browser. It only reads the
DeepSeek origin and writes an authenticated token to the OS credential vault.
Normal api_client.py operation has no browser or forensics dependency.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
import sys
import types

import requests

from credential_store import save_credentials


TOOLS_DIR = Path(__file__).resolve().parents[2] / "tools"
CHROMIUM_READER = TOOLS_DIR / "ccl_chromium_reader" / "ccl_chromium_reader"
SNAPPY_READER = TOOLS_DIR / "ccl_simplesnappy"
DEEPSEEK_ORIGIN = "https://chat.deepseek.com"
PROFILE_PATHS = [
    Path.home() / "AppData/Local/Microsoft/Edge/User Data/Default/Local Storage/leveldb",
    Path.home() / "AppData/Local/Google/Chrome/User Data/Default/Local Storage/leveldb",
]


def _load_reader():
    if not CHROMIUM_READER.is_dir() or not SNAPPY_READER.is_dir():
        raise RuntimeError("Offline Local Storage reader is not installed")
    sys.path.insert(0, str(SNAPPY_READER))
    if "ccl_chromium_reader" not in sys.modules:
        package = types.ModuleType("ccl_chromium_reader")
        package.__path__ = [str(CHROMIUM_READER)]
        sys.modules["ccl_chromium_reader"] = package
    return importlib.import_module("ccl_chromium_reader.ccl_chromium_localstorage")


def _latest_value(database, key: str) -> str:
    if not database.contains_script_key(DEEPSEEK_ORIGIN, key):
        return ""
    records = list(database.iter_records_for_script_key(DEEPSEEK_ORIGIN, key))
    if not records:
        return ""
    raw = max(records, key=lambda rec: rec.leveldb_seq_number).value or ""
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed.get("value", "") if isinstance(parsed.get("value"), str) else ""
        if isinstance(parsed, str):
            return parsed
    except (ValueError, TypeError):
        pass
    return raw


def try_import() -> bool:
    """Return whether a valid locally authenticated DeepSeek session was saved."""
    try:
        local_storage = _load_reader()
    except (OSError, RuntimeError, ImportError):
        return False
    for path in PROFILE_PATHS:
        if not path.is_dir():
            continue
        try:
            with local_storage.LocalStoreDb(path) as database:
                if not database.contains_storage_key(DEEPSEEK_ORIGIN):
                    continue
                token = _latest_value(database, "userToken")
                hif_leim = _latest_value(database, "hif_leim_cached")
        except (OSError, ValueError, KeyError):
            continue
        if not token:
            continue
        try:
            response = requests.get(
                "https://chat.deepseek.com/api/v0/users/current",
                headers={"Authorization": f"Bearer {token}"},
                timeout=15,
            )
            authenticated = response.ok and response.json().get("code") == 0
        except (requests.RequestException, ValueError, TypeError, AttributeError):
            authenticated = False
        if authenticated:
            from api_client import DEFAULT_DEVICE_ID

            return save_credentials(
                token,
                device_id=DEFAULT_DEVICE_ID,
                hif_leim=hif_leim,
            )
    return False


def main() -> int:
    imported = try_import()
    print("Valid DeepSeek session imported into OS vault:", imported)
    return 0 if imported else 1


if __name__ == "__main__":
    raise SystemExit(main())
