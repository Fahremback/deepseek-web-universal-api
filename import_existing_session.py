"""Import an existing DeepSeek session from Chromium Local Storage, offline.

This one-time recovery utility does not start a browser. It only reads the
DeepSeek origin and writes an authenticated token to the OS credential vault.
Normal api_client.py operation has no browser or forensics dependency.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import time
import types

from credential_store import save_credentials


TOOLS_DIR = Path(__file__).resolve().parents[2] / "tools"
CHROMIUM_READER = TOOLS_DIR / "ccl_chromium_reader" / "ccl_chromium_reader"
SNAPPY_READER = TOOLS_DIR / "ccl_simplesnappy"
DEEPSEEK_ORIGIN = "https://chat.deepseek.com"


def chromium_profile_roots(*, platform: str | None = None,
                           home: Path | None = None,
                           env: dict[str, str] | None = None) -> list[Path]:
    """Known Chromium user-data directories on Windows, macOS and Linux."""
    platform = platform or sys.platform
    home = home or Path.home()
    env = env if env is not None else os.environ
    if platform == "win32":
        base = Path(env.get("LOCALAPPDATA") or home / "AppData" / "Local")
        names = ("Perplexity/Comet/User Data", "Microsoft/Edge/User Data",
                 "Google/Chrome/User Data", "BraveSoftware/Brave-Browser/User Data",
                 "Chromium/User Data", "Vivaldi/User Data")
    elif platform == "darwin":
        base = home / "Library" / "Application Support"
        names = ("Perplexity/Comet", "Microsoft Edge", "Google/Chrome",
                 "BraveSoftware/Brave-Browser", "Chromium", "Vivaldi")
    else:
        base = Path(env.get("XDG_CONFIG_HOME") or home / ".config")
        names = ("comet", "microsoft-edge", "google-chrome",
                 "BraveSoftware/Brave-Browser", "chromium", "vivaldi")
    return [base / name for name in names]


_ROOTS = chromium_profile_roots()
COMET_ROOT = _ROOTS[0]
PROFILE_PATHS = [root / "Default" / "Local Storage" / "leveldb" for root in _ROOTS]
# A denied session must not trigger a network request on every login poll.
# Successfully verified sessions are deliberately not throttled.
_LAST_CHECK: dict[str, float] = {}
RECHECK_SECONDS = 60.0


def candidate_profile_paths() -> list[Path]:
    """Prefer Comet, then installed Chromium-family browsers and profiles."""
    def extra_profiles(root: Path) -> list[Path]:
        profiles = sorted(
            (p for p in root.glob("Profile *")
             if p.is_dir() and p.name[8:].isdigit()),
            key=lambda p: int(p.name[8:]),
        )
        return [p / "Local Storage" / "leveldb" for p in profiles]

    paths = [PROFILE_PATHS[0], *extra_profiles(COMET_ROOT)]
    for path in PROFILE_PATHS[1:]:
        paths.append(path)
        paths.extend(extra_profiles(path.parents[2]))
    # For other Chromium browsers, an explicit user-data profile can be given
    # without assuming how its vendor lays out filesystem directories.
    custom = os.environ.get("DEEPSEEK_BROWSER_PROFILE")
    if custom:
        path = Path(custom).expanduser()
        paths.append(path if path.name == "leveldb" else path / "Local Storage" / "leveldb")
    return list(dict.fromkeys(path for path in paths if path.is_dir()))


def _verify_candidate(token: str, proof: str, device_id: str) -> bool:
    """Use the gateway's actual request headers for one read-only account check."""
    from api_client import DeepSeekClient
    from verify_session import verify_authorization

    client = DeepSeekClient(
        token=token, hif_leim=proof, device_id=device_id or None,
        allow_unauthed=True, timeout=15,
    )
    # Old keyring cookies may belong to a different account.
    client.session.cookies.clear()
    try:
        return verify_authorization(client).get("authorization") == "accepted"
    finally:
        client.session.close()


def _load_reader():
    # Allow an independently installed reader so the project can be moved to
    # another computer without keeping the original deepseek_api/tools tree.
    # The adjacent offline reader remains supported for existing deployments.
    try:
        return importlib.import_module("ccl_chromium_reader.ccl_chromium_localstorage")
    except ImportError:
        pass
    if not CHROMIUM_READER.is_dir() or not SNAPPY_READER.is_dir():
        raise RuntimeError("Offline Local Storage reader is not installed; use a managed browser login or provide an existing token")
    if str(SNAPPY_READER) not in sys.path:
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
    for path in candidate_profile_paths():
        try:
            with local_storage.LocalStoreDb(path) as database:
                if not database.contains_storage_key(DEEPSEEK_ORIGIN):
                    continue
                token = _latest_value(database, "userToken")
                hif_leim = _latest_value(database, "hif_leim_cached")
                device_id = _latest_value(database, "deviceId")
        except (OSError, ValueError, KeyError):
            continue
        if not token:
            continue
        # A rejected token may become usable when Comet refreshes its proof
        # or device identifier without rotating the bearer token itself.
        # Cache only an opaque fingerprint; never store secrets in the cache.
        fingerprint = hashlib.sha256(
            json.dumps([token, hif_leim, device_id], ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        now = time.monotonic()
        if now - _LAST_CHECK.get(fingerprint, -float("inf")) < RECHECK_SECONDS:
            continue
        try:
            authenticated = _verify_candidate(token, hif_leim, device_id)
        except Exception:
            authenticated = False
        if authenticated:
            from api_client import DEFAULT_DEVICE_ID

            return save_credentials(
                token,
                device_id=device_id or DEFAULT_DEVICE_ID,
                hif_leim=hif_leim,
            )
        _LAST_CHECK[fingerprint] = now
        if len(_LAST_CHECK) > 32:
            _LAST_CHECK.clear()
            _LAST_CHECK[fingerprint] = now
    return False


def main() -> int:
    imported = try_import()
    print("Valid DeepSeek session imported into OS vault:", imported)
    return 0 if imported else 1


if __name__ == "__main__":
    raise SystemExit(main())
