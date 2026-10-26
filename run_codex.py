"""Run a Codex CLI session against a private, session-scoped DeepSeek gateway.

Usage:
    python run_codex.py
    python run_codex.py -- exec "Summarize this repository"

No global Codex configuration is edited. The localhost server is terminated
when the Codex CLI exits; this is not a background/system service.
"""

from __future__ import annotations

import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import threading

from openai_server import Gateway


ROOT = Path(__file__).resolve().parent
PROVIDER_ID = "deepseek_web_local"


def find_codex() -> str:
    # A path on PATH is not proof that the CLI is runnable: WSL may discover a
    # Windows npm shim whose Node.js interpreter is missing on Linux.
    candidates = ([shutil.which("codex.exe"), shutil.which("codex")]
                  if os.name == "nt" else [shutil.which("codex")])
    for binary in dict.fromkeys(path for path in candidates if path):
        try:
            probe = subprocess.run([binary, "--version"], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=5)
            if probe.returncode == 0:
                return binary
        except (OSError, subprocess.SubprocessError):
            continue
    raise RuntimeError("No runnable Codex CLI was found on PATH")


def codex_command(binary: str, port: int, extra: list[str]) -> list[str]:
    base = f"http://127.0.0.1:{port}/v1"
    return [
        binary,
        "-c", f'model_provider="{PROVIDER_ID}"',
        "-c", f'model_providers.{PROVIDER_ID}.name="DeepSeek Web Local"',
        "-c", f'model_providers.{PROVIDER_ID}.base_url="{base}"',
        "-c", f'model_providers.{PROVIDER_ID}.wire_api="responses"',
        "-c", f'model_providers.{PROVIDER_ID}.env_key="DEEPSEEK_BRIDGE_API_KEY"',
        "-c", f'model_providers.{PROVIDER_ID}.requires_openai_auth=false',
        "-m", "deepseek-web",
        *extra,
    ]


def main(args: list[str] | None = None) -> int:
    extra = sys.argv[1:] if args is None else list(args)
    if extra[:1] == ["--"]:
        extra = extra[1:]
    gateway: Gateway | None = None
    thread: threading.Thread | None = None
    previous_bridge_mode = os.environ.get("DEEPSEEK_CODEX_BRIDGE")
    try:
        binary = find_codex()
        # Only this ephemeral Codex gateway needs the larger prompt budget.
        # Restore the process environment in finally (including on failure).
        os.environ["DEEPSEEK_CODEX_BRIDGE"] = "1"
        env = os.environ.copy()
        # Keep the port bound from its assignment until shutdown. Reserving a
        # port and closing the reservation before Popen permitted another
        # process to claim it and impersonate the local bridge.
        env["DEEPSEEK_BRIDGE_API_KEY"] = secrets.token_urlsafe(32)
        gateway = Gateway(("127.0.0.1", 0), api_key=env["DEEPSEEK_BRIDGE_API_KEY"])
        port = gateway.server_port
        thread = threading.Thread(target=gateway.serve_forever,
                                  name="deepseek-codex-gateway", daemon=True)
        thread.start()
        print(f"Codex using DeepSeek Web at http://127.0.0.1:{port}/v1", flush=True)
        return subprocess.call(codex_command(binary, port, extra), env=env)
    except KeyboardInterrupt:
        return 130
    except (OSError, RuntimeError) as exc:
        print(f"Could not start local Codex gateway: {type(exc).__name__}", file=sys.stderr)
        return 1
    finally:
        if gateway is not None:
            if thread is not None and thread.is_alive():
                gateway.shutdown()
                thread.join(timeout=5)
            gateway.server_close()
        if previous_bridge_mode is None:
            os.environ.pop("DEEPSEEK_CODEX_BRIDGE", None)
        else:
            os.environ["DEEPSEEK_CODEX_BRIDGE"] = previous_bridge_mode


if __name__ == "__main__":
    raise SystemExit(main())
