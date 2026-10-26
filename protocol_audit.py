"""Offline, secret-free protocol audit of user-provided HTTP Archive (HAR) files.

No network requests, cookies, credentials, fingerprint reproduction or playback.
The output describes *observed* traffic only, never assumes what the remote
server requires. Intended for investigating incomplete capture/test evidence.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
from urllib.parse import urlsplit


# Routes supported by the existing lightweight Python transport. This table
# intentionally describes capabilities; it is not a complete browser trace.
IMPLEMENTED_ROUTES = frozenset({
    "/api/v0/users/login",
    "/api/v0/chat/create_pow_challenge",
    "/api/v0/chat_session/create",
    "/api/v0/chat/completion",
    "/api/v0/chat/continue",
    "/api/v0/file/upload_file",
    "/api/v0/file/fetch_files",
})
DEEPSEEK_HOST = "chat.deepseek.com"
EXTRA_STATIC_ROUTES = frozenset({
    "/api/v0/client/settings",
    "/api/v0/users/current",
})


def _time(value: object) -> str | None:
    """Only retain the timestamp, dropping everything else in the HAR record."""
    if not isinstance(value, str):
        return None
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d.isoformat()


def inspect_har(data: dict) -> dict:
    """Return a deliberately minimal report without request/response secrets."""
    entries = data.get("log", {}).get("entries", [])
    if not isinstance(entries, list):
        raise ValueError("HAR must contain log.entries as an array")
    public_routes = Counter()
    status = Counter()
    official_calls = []
    extra_site_calls = []
    chronology = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        req = entry.get("request") or {}
        resp = entry.get("response") or {}
        if not isinstance(req, dict) or not isinstance(resp, dict):
            continue
        # Never retain URL query, fragments, request/response headers, cookies,
        # postData, content, HAR comments or arbitrary application payloads.
        try:
            url = urlsplit(str(req.get("url") or ""))
            host = (url.hostname or "").lower()
        except ValueError:
            continue
        if not host.endswith(".deepseek.com") and host != "deepseek.com":
            continue
        method = str(req.get("method") or "UNKNOWN").upper()
        if method not in ("GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH", "HEAD"):
            method = "UNKNOWN"
        code = resp.get("status")
        status[str(code) if isinstance(code, int) else "unknown"] += 1
        route = url.path or "/"
        if route.startswith("/api/") and host == DEEPSEEK_HOST:
            # API paths sometimes contain opaque IDs. Preserve only known
            # static routes; unknown paths receive a category instead.
            name = (route if route in IMPLEMENTED_ROUTES | EXTRA_STATIC_ROUTES
                    else "<other_api_route>")
            chronology.append({
                "at": _time(entry.get("startedDateTime")),
                "method": method,
                "route": name,
                "status": code if isinstance(code, int) else None,
            })
            official_calls.append(route)
            if route in EXTRA_STATIC_ROUTES:
                extra_site_calls.append(route)
            public_routes[(method, name)] += 1
        elif route.startswith("/api/"):
            public_routes[(method, "<other_deepseek_api_host>")] += 1
        else:
            public_routes[(method, "<website_or_static_asset>")] += 1
    observed_known = set(official_calls) & IMPLEMENTED_ROUTES
    return {
        "total_har_entries": len(entries),
        "matching_deepseek_entries": sum(status.values()),
        "http_status_counts": dict(sorted(status.items())),
        "route_counts": [
            {"method": method, "route": path, "count": count}
            for (method, path), count in sorted(public_routes.items())
        ],
        "known_client_routes_observed": sorted(observed_known),
        "known_client_routes_not_observed": sorted(IMPLEMENTED_ROUTES - observed_known),
        "other_known_site_routes_observed": sorted(set(extra_site_calls)),
        "api_request_sequence": chronology,
        "limitations": [
            "HAR traffic is a sample, not proof of server validation requirements.",
            "HTTP 200 does not establish future account authorization or compliance.",
            "No cookies, tokens, payloads, fingerprints or user identities are collected.",
            "Unobserved routes may be optional, used in different sessions or absent from capture.",
            "Only a provider can determine the exact reason for account suspension.",
        ],
    }


def inspect_files(paths: list[Path]) -> dict:
    captures = []
    for i, path in enumerate(paths, 1):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("HAR root must be an object")
            report = inspect_har(data)
        except (OSError, ValueError, UnicodeError, TypeError, AttributeError) as exc:
            # Do not print user-supplied path or exception content; these may
            # contain secrets (URLs, account names, or file contents).
            captures.append({"capture": i, "error": type(exc).__name__})
            continue
        captures.append({"capture": i, "report": report})
    return {
        "source": "offline HAR metadata audit",
        "network_requests_made": 0,
        "captures": captures,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarize captured DeepSeek HTTP traffic offline")
    parser.add_argument("har", nargs="+", type=Path, help="One or more HAR files")
    parser.add_argument("--output", type=Path, help="Optional sanitized JSON destination")
    args = parser.parse_args(argv)
    output = json.dumps(inspect_files(args.har), ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(output + "\n", encoding="utf-8")
        print("Sanitized report written successfully")
    else:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
