"""DeepSeek session and account login secrets in the operating-system keyring.

The password is stored in a separate vault entry so rotating a session token
never overwrites or exposes account login details.
"""

from __future__ import annotations

import json
from typing import Any


SERVICE = "DeepSeekWebClient"
ACCOUNT = "api-credentials"
LOGIN_ACCOUNT = "account-login"


def load_credentials() -> dict[str, Any]:
    try:
        import keyring

        raw = keyring.get_password(SERVICE, ACCOUNT)
        if not raw:
            return {}
        data = json.loads(raw)
        if isinstance(data, dict) and isinstance(data.get("token"), str):
            return data
    except Exception:
        pass
    return {}


def save_credentials(
    token: str,
    *,
    device_id: str = "",
    hif_leim: str = "",
    cookies: dict[str, str] | None = None,
) -> bool:
    if not token or not token.strip():
        return False
    try:
        import keyring

        keyring.set_password(
            SERVICE,
            ACCOUNT,
            json.dumps(
                {
                    "token": token.strip(),
                    "device_id": device_id,
                    "hif_leim": hif_leim,
                    "cookies": {
                        key: value
                        for key, value in (cookies or {}).items()
                        if key in {"ds_session_id", "HWWAFSESID", "HWWAFSESTIME"}
                    },
                }
            ),
        )
        return True
    except Exception:
        return False


def load_login_credentials() -> dict[str, str]:
    """Read an explicitly provisioned email/mobile and password from the vault."""
    try:
        import keyring

        raw = keyring.get_password(SERVICE, LOGIN_ACCOUNT)
        data = json.loads(raw) if raw else {}
        if isinstance(data, dict) and isinstance(data.get("password"), str):
            return {
                "email": str(data.get("email") or ""),
                "mobile": str(data.get("mobile") or ""),
                "area_code": str(data.get("area_code") or ""),
                "password": data["password"],
            }
    except Exception:
        pass
    return {}


def save_login_credentials(
    *, email: str = "", mobile: str = "", area_code: str = "", password: str
) -> bool:
    """Provision unattended HTTP login without writing the password to disk."""
    if not password or not (email.strip() or mobile.strip()):
        return False
    try:
        import keyring

        keyring.set_password(SERVICE, LOGIN_ACCOUNT, json.dumps({
            "email": email.strip(),
            "mobile": mobile.strip(),
            "area_code": area_code.strip(),
            "password": password,
        }))
        return True
    except Exception:
        return False


def clear_credentials() -> None:
    try:
        import keyring

        for account in (ACCOUNT, LOGIN_ACCOUNT):
            try:
                keyring.delete_password(SERVICE, account)
            except Exception:
                pass
    except Exception:
        pass
