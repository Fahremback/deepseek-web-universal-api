"""Interactive login on the real DeepSeek website, not a replica login form.

The browser's private, persistent profile allows future user-driven logins
without collecting passwords. Only a verified bearer token is saved to the OS
keyring. No account password is ever read by this module.
"""

from __future__ import annotations

import json
import os
import sys
import time
import webbrowser
from pathlib import Path
from typing import Any, Callable


ORIGIN = "https://chat.deepseek.com"
LOGIN_URL = f"{ORIGIN}/sign_in"
ALLOWED_COOKIES = {"ds_session_id", "HWWAFSESID", "HWWAFSESTIME"}


def profile_directory(browser_type: str = "chrome") -> Path:
    """Keep independent browser profiles in the current OS's user data dir."""
    if sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    # Retain Chrome's legacy profile path, but do not share its browser data
    # with Edge or other engines. They can use the token saved in OS keyring.
    name = "browser-profile" if browser_type == "chrome" else f"browser-profile-{browser_type}"
    return root / "DeepSeekWebClient" / name


def parse_storage_value(value: Any) -> str:
    """DeepSeek localStorage has used both raw and JSON wrapped tokens."""
    if not isinstance(value, str) or not value:
        return ""
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return value.strip()
    if isinstance(parsed, dict):
        return str(parsed.get("value") or "").strip()
    if isinstance(parsed, str):
        return parsed.strip()
    return ""


def capture_session(page: Any, context: Any, *, validator: Callable[..., bool] | None = None) -> bool:
    """Read only the official origin, verify the token, and persist it securely."""
    from credential_store import save_credentials

    if not page.url.startswith(f"{ORIGIN}/") and page.url != ORIGIN:
        return False
    try:
        storage = page.evaluate("""() => ({
            token: localStorage.getItem('userToken'),
            proof: localStorage.getItem('hif_leim_cached'),
            device: localStorage.getItem('deviceId')
        })""")
    except Exception:
        return False
    if not isinstance(storage, dict):
        return False
    token = parse_storage_value(storage.get("token"))
    if not token:
        return False
    # Verify authentication in the logged-in page itself. This uses the real
    # first-party cookies, avoiding accidental bypasses of the site's controls.
    if validator is None:
        try:
            valid = page.evaluate("""async token => {
                try {
                    const result = await fetch('/api/v0/users/current', {
                        headers: {'authorization': 'Bearer ' + token},
                        credentials: 'same-origin',
                        cache: 'no-store'
                    });
                    if (!result.ok) return false;
                    const payload = await result.json();
                    return !!(payload && payload.code === 0 &&
                        (!payload.data || payload.data.biz_code == null || payload.data.biz_code === 0));
                } catch (_) { return false; }
            }""", token)
        except Exception:
            return False
    else:
        valid = validator(token=token, page=page)
    if not valid:
        return False
    try:
        cookies = {c["name"]: c["value"] for c in context.cookies([ORIGIN])
                   if c.get("name") in ALLOWED_COOKIES}
    except Exception:
        cookies = {}
    return save_credentials(
        token,
        device_id=parse_storage_value(storage.get("device")),
        hif_leim=parse_storage_value(storage.get("proof")),
        cookies=cookies,
    )


def login_in_browser(*, timeout_seconds: float = 300,
                     poll_seconds: float = 5,
                     browser_type: str = "default",
                     launcher: Callable[..., Any] | None = None) -> bool:
    """Open the official sign-in page, wait for the user, then save the session.

    If login or a challenge requires user action, the page stays interactive.
    The account password is typed only into the first-party DeepSeek page.
    """
    if timeout_seconds <= 0 or poll_seconds <= 0:
        raise ValueError("Timeouts must be positive")
    engines = {
        "chrome": ("chromium", "chrome"),
        "msedge": ("chromium", "msedge"),
        "chromium": ("chromium", None),
        "firefox": ("firefox", None),
        "webkit": ("webkit", None),
    }
    if browser_type not in {"default", *engines}:
        raise ValueError("Unsupported browser; use default, chrome, msedge, chromium, firefox or webkit")

    if browser_type == "default":
        from import_existing_session import try_import

        # A valid session in Comet is sufficient; no browser window is needed.
        if try_import():
            return True

        # Open through the OS URL association. Session import is opportunistic:
        # an arbitrary default browser may not use a readable Chromium profile.
        if not webbrowser.open(LOGIN_URL, new=2):
            raise RuntimeError("Could not open the default browser")
        print("DeepSeek: aguardando sessão no navegador padrão...", file=sys.stderr, flush=True)
        deadline = time.monotonic() + timeout_seconds
        next_notice = time.monotonic() + 30
        while time.monotonic() < deadline:
            # Offline reader touches only chat.deepseek.com localStorage and
            # verifies each discovered token before storing it in the keyring.
            if try_import():
                return True
            if time.monotonic() >= next_notice:
                print("DeepSeek: ainda verificando a sessão do navegador...", file=sys.stderr, flush=True)
                next_notice = time.monotonic() + 30
            time.sleep(min(poll_seconds, max(0, deadline - time.monotonic())))
        return False

    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        engine_name, channel = engines[browser_type]
        engine = getattr(playwright, engine_name)
        launch_kwargs: dict[str, Any] = {
            "user_data_dir": str(profile_directory(browser_type)),
            "headless": False,
            "no_viewport": True,
        }
        if channel:
            launch_kwargs["channel"] = channel
        if engine_name == "chromium":
            launch_kwargs["args"] = ["--no-first-run"]
        context = (launcher or engine.launch_persistent_context)(**launch_kwargs)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
            end = time.monotonic() + timeout_seconds
            while time.monotonic() < end:
                # OAuth providers can open a popup, then return to another tab.
                pages = list(context.pages)
                if not pages:
                    return False
                for candidate in pages:
                    if candidate.is_closed():
                        continue
                    try:
                        if capture_session(candidate, context):
                            return True
                    except Exception:
                        continue
                time.sleep(poll_seconds)
            return False
        finally:
            context.close()
