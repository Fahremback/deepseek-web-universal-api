"""Offline lifecycle/argument checks for the optional Codex CLI launcher."""

import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from pathlib import Path
from unittest.mock import MagicMock, patch

import run_codex


class CodexLauncherTests(unittest.TestCase):
    def test_launcher_allows_codex_sized_prompt_without_widening_other_clients(self):
        from openai_server import Gateway
        from test_openai_bridge import FakeDeepSeek

        class OfflineDeepSeek(FakeDeepSeek):
            response = "LONG_CONTEXT_OK"

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DEEPSEEK_CODEX_BRIDGE", None)
            with patch.object(run_codex, "find_codex", return_value="codex"):
                def inspect(command, *, env):
                    import requests
                    port = int(re.search(r':(\d+)/v1', " ".join(command)).group(1))
                    payload = {"model": "deepseek-web", "input": "x" * 250000, "stream": False}
                    with requests.Session() as session:
                        response = session.post(f"http://127.0.0.1:{port}/v1/responses",
                                                json=payload,
                                                headers={"Authorization": "Bearer " +
                                                         env["DEEPSEEK_BRIDGE_API_KEY"]},
                                                timeout=10)
                    self.assertEqual(response.status_code, 200, response.text[:250])
                    self.assertEqual(response.json()["output_text"], "LONG_CONTEXT_OK")
                    return 0

                def factory(address, *, api_key):
                    return Gateway(address, api_key=api_key, client_factory=OfflineDeepSeek)

                with patch.object(run_codex, "Gateway", side_effect=factory), \
                     patch.object(run_codex.subprocess, "call", side_effect=inspect):
                    self.assertEqual(run_codex.main(["exec", "Long prompt"]), 0)
            self.assertNotIn("DEEPSEEK_CODEX_BRIDGE", os.environ)

    def test_real_codex_uses_launcher_and_offline_backend(self):
        """Exercise the launcher + Codex executable + Responses, never DeepSeek."""
        try:
            run_codex.find_codex()
        except RuntimeError:
            self.skipTest("Codex CLI not available in this operating system")
        from openai_server import Gateway
        from test_openai_bridge import FakeDeepSeek

        class OfflineDeepSeek(FakeDeepSeek):
            response = "OFFLINE_LAUNCHER_OK"

        starting_count = len(FakeDeepSeek.instances)
        original_command = run_codex.codex_command

        def isolated_command(binary, port, extra):
            command = original_command(binary, port, extra)
            # Keep the integration test completely offline and avoid the Codex
            # plugin manager cloning repositories in CODEX_HOME asynchronously.
            boundary = command.index("-m")
            command[boundary:boundary] = ["--disable", "plugins", "--disable", "apps"]
            return command

        with tempfile.TemporaryDirectory(prefix="codex-launcher-offline-") as directory:
            workspace = Path(directory)
            codex_home = workspace / "home"
            codex_home.mkdir()
            args = ["--", "exec", "--ephemeral", "--ignore-user-config",
                    "--skip-git-repo-check", "--color", "never", "-C", str(workspace),
                    "Respond with exactly OFFLINE_LAUNCHER_OK and nothing else"]

            def offline_gateway(address, *, api_key):
                return Gateway(address, api_key=api_key, client_factory=OfflineDeepSeek)

            with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}), \
                 patch.object(run_codex, "Gateway", side_effect=offline_gateway), \
                 patch.object(run_codex, "codex_command", side_effect=isolated_command):
                self.assertEqual(run_codex.main(args), 0)
        self.assertTrue(FakeDeepSeek.instances[starting_count:],
                        "Real Codex failed to reach the in-process gateway")
        self.assertTrue(FakeDeepSeek.instances[-1].calls)

    def test_unusable_codex_shim_is_not_selected(self):
        broken = MagicMock(returncode=127)
        with patch.object(run_codex.os, "name", "posix"), \
             patch.object(run_codex.shutil, "which", return_value="/mnt/c/npm/codex"), \
             patch.object(run_codex.subprocess, "run", return_value=broken):
            with self.assertRaisesRegex(RuntimeError, "No runnable Codex"):
                run_codex.find_codex()

    def test_real_codex_help_starts_and_closes_gateway(self):
        """Smoke-test real process lifecycle; never request model generation."""
        try:
            run_codex.find_codex()
        except RuntimeError:
            self.skipTest("Codex CLI is not installed")
        result = subprocess.run(
            [sys.executable, str(run_codex.ROOT / "run_codex.py"), "--", "--help"],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Codex CLI", result.stdout)
        address = re.search(r"http://127\.0\.0\.1:(\d+)/v1", result.stdout)
        self.assertIsNotNone(address, result.stdout)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
            connection.settimeout(0.5)
            self.assertNotEqual(connection.connect_ex(("127.0.0.1", int(address.group(1)))), 0)

    def test_custom_provider_uses_responses_and_localhost(self):
        args = run_codex.codex_command("codex", 19341, ["exec", "Say hello"])
        flattened = " ".join(args)
        self.assertIn('model_provider="deepseek_web_local"', flattened)
        self.assertIn('wire_api="responses"', flattened)
        self.assertIn('base_url="http://127.0.0.1:19341/v1"', flattened)
        self.assertIn('env_key="DEEPSEEK_BRIDGE_API_KEY"', flattened)
        self.assertIn('requires_openai_auth=false', flattened)
        self.assertEqual(args[-2:], ["exec", "Say hello"])

    def test_port_is_bound_and_api_key_required_during_codex_execution(self):
        observed = {}

        def inspect(command, *, env):
            port = int(re.search(r'base_url="http://127\.0\.0\.1:(\d+)/v1"',
                                 " ".join(command)).group(1))
            observed["port"] = port
            observed["secret"] = env["DEEPSEEK_BRIDGE_API_KEY"]
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as rival:
                with self.assertRaises(OSError):
                    rival.bind(("127.0.0.1", port))
            with self.assertRaises(HTTPError) as unauthorized:
                urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=3)
            self.assertEqual(unauthorized.exception.code, 401)
            unauthorized.exception.close()
            request = Request(f"http://127.0.0.1:{port}/v1/models",
                              headers={"Authorization": "Bearer " + observed["secret"]})
            with urlopen(request, timeout=3) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(json.load(response)["object"], "list")
            self.assertNotIn(observed["secret"], str(command))
            self.assertIn("exec", command)
            return 0

        with patch.object(run_codex, "find_codex", return_value="codex"), \
             patch.object(run_codex.secrets, "token_urlsafe", return_value="temporary-secret"), \
             patch.object(run_codex.subprocess, "call", side_effect=inspect) as cli:
            self.assertEqual(run_codex.main(["--", "exec", "Say hello"]), 0)
        cli.assert_called_once()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as closed:
            closed.settimeout(0.5)
            self.assertNotEqual(closed.connect_ex(("127.0.0.1", observed["port"])), 0)

    def test_gateway_is_closed_when_codex_fails(self):
        captured = {}

        def fail(command, *, env):
            captured["port"] = int(re.search(r':(\d+)/v1', " ".join(command)).group(1))
            raise OSError("unavailable")

        with patch.object(run_codex, "find_codex", return_value="codex"), \
             patch.object(run_codex.subprocess, "call", side_effect=fail):
            with patch.dict(os.environ, {"DEEPSEEK_CODEX_BRIDGE": "existing-value"}):
                self.assertEqual(run_codex.main([]), 1)
                self.assertEqual(os.environ["DEEPSEEK_CODEX_BRIDGE"], "existing-value")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as closed:
            self.assertNotEqual(closed.connect_ex(("127.0.0.1", captured["port"])), 0)

    def test_gateway_bind_error_never_starts_codex(self):
        with patch.object(run_codex, "find_codex", return_value="codex"), \
             patch.object(run_codex, "Gateway", side_effect=OSError("bind failed")), \
             patch.object(run_codex.subprocess, "call") as cli:
            self.assertEqual(run_codex.main([]), 1)
        cli.assert_not_called()


if __name__ == "__main__":
    unittest.main()
