"""Optional OS credential-vault storage for the DeepSeek Web API session.

No browser dependency. The token must be provided once by an authorized
account owner, just like an API key. The OS keyring caches it after a valid
authenticated request, so subsequent requests can run unattended.
"""

from __future__ import annotations

import json
from typing import Any


SERVICE = "DeepSeekWebClient"
ACCOUNT = "api-credentials"


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


def clear_credentials() -> None:
    try:
        import keyring

        keyring.delete_password(SERVICE, ACCOUNT)
    except Exception:
        pass
