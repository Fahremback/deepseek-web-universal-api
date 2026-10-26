"""Credential preflight and explicit, minimal DeepSeek Web integration probe.

Without --live this program performs no network requests. --live checks the
saved account's first-party identity endpoint. If the saved session is rejected,
it attempts to import an already authenticated Comet session and checks once
more. --generate sends one short prompt only after authentication passes.
Only status summaries are printed; the session token and model output stay local.
"""

from __future__ import annotations

import argparse
import json
from typing import Any, Callable

import requests

from api_client import DeepSeekClient
from credential_store import load_credentials, load_login_credentials


CHECK_PATH = "/api/v0/users/current"


def _public_code(value: Any) -> int | None:
    """Expose provider numeric status codes, never provider-supplied free text."""
    return value if type(value) is int else None


def local_status() -> dict[str, bool]:
    """Report whether credentials exist without exposing the values."""
    return {
        "saved_session": bool(load_credentials().get("token")),
        "saved_password_login": bool(load_login_credentials().get("password")),
    }


def verify_authorization(client: Any) -> dict[str, Any]:
    """One authorized-session check; never refresh or retry on rejection."""
    response = None
    try:
        response = client.session.get(
            client.base_url + CHECK_PATH,
            headers=client._headers(referer=client._referer()),
            timeout=min(client.timeout, 15),
        )
        code = response.status_code
        if code != 200:
            return {"authorization": "rejected", "http_status": code}
        data = response.json()
        if not isinstance(data, dict):
            return {"authorization": "unverified", "http_status": code,
                    "reason": "unexpected_response"}
        api_code = data.get("code")
        if api_code != 0:
            result: dict[str, Any] = {
                "authorization": "rejected", "http_status": code,
                "reason": "api_code_rejected" if api_code is not None else "missing_api_code",
            }
            if _public_code(api_code) is not None:
                result["api_code"] = api_code
            return result
        block = data.get("data")
        if isinstance(block, dict) and block.get("biz_code") not in (None, 0):
            result = {"authorization": "rejected", "http_status": code,
                      "reason": "biz_code_rejected"}
            biz_code = _public_code(block["biz_code"])
            if biz_code is not None:
                result["biz_code"] = biz_code
            return result
        return {"authorization": "accepted", "http_status": code}
    except (requests.RequestException, ValueError, TypeError):
        return {"authorization": "unverified"}
    finally:
        if response is not None:
            response.close()


def live_probe(*, generate: bool = False, recover: bool = False,
               factory: Callable[..., Any] = DeepSeekClient) -> dict[str, Any]:
    """Verify saved credentials; optionally recover from existing browser storage."""
    saved = load_credentials()
    if not saved.get("token"):
        return {"authorization": "missing_session"}
    client = factory(token=saved["token"], allow_unauthed=True)
    try:
        status = verify_authorization(client)
        if recover and status["authorization"] == "rejected":
            # Only use an already authenticated Comet profile. No login page,
            # stored password, or browser automation is involved.
            try:
                from import_existing_session import try_import

                if try_import():
                    refreshed = load_credentials()
                    if refreshed.get("token"):
                        client.session.close()
                        client = factory(token=refreshed["token"], allow_unauthed=True)
                        status = verify_authorization(client)
            except (ImportError, OSError, RuntimeError, ValueError):
                pass
        if status["authorization"] != "accepted" or not generate:
            return status
        try:
            result = client.chat(
                "Responda apenas OK.", thinking_enabled=False,
                search_enabled=False, auto_continue=False,
            )
            status["generation"] = (
                "accepted" if result.status == "FINISHED" and bool(result.text.strip())
                else "incomplete"
            )
        except Exception:
            # Do not print provider error messages: they may contain private data.
            status["generation"] = "failed"
        return status
    finally:
        client.session.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Check and recover the saved session using available local browser profiles when needed")
    parser.add_argument("--login-web", action="store_true",
                        help="Sign in interactively on DeepSeek's website before the live check")
    parser.add_argument("--login-browser", choices=("default", "chrome", "msedge", "chromium", "firefox", "webkit"),
                        default="default", help="Browser used by --login-web (default: system browser)")
    parser.add_argument("--generate", action="store_true", help="Also generate one short reply after validation")
    args = parser.parse_args(argv)
    if args.generate and not (args.live or args.login_web):
        parser.error("--generate requires --live or --login-web")
    if args.login_web:
        from browser_login import login_in_browser

        try:
            signed_in = login_in_browser(**({"browser_type": args.login_browser}
                                          if args.login_browser != "default" else {}))
        except (ImportError, OSError, RuntimeError, ValueError):
            signed_in = False
        if not signed_in:
            print(json.dumps({"authorization": "login_not_completed"}))
            return 1
        report = live_probe(generate=args.generate)
    elif not args.live:
        report: dict[str, Any] = local_status()
    else:
        report = live_probe(generate=args.generate, recover=True)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    if not (args.live or args.login_web):
        return 0
    return 0 if report.get("authorization") == "accepted" and (
        not args.generate or report.get("generation") == "accepted"
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
