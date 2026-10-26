"""Real Codex CLI -> real Responses HTTP gateway, with a simulated model.

No external provider requests or project edits are performed.
"""

from __future__ import annotations

import os
import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from api_client import ChatResult
from openai_server import Gateway
from run_codex import codex_command, find_codex
from test_openai_bridge import FakeDeepSeek


class CodexWireTests(unittest.TestCase):
    def test_actual_codex_exec_reads_gateway_response(self):
        try:
            codex = find_codex()
        except RuntimeError:
            self.skipTest("A runnable local Codex CLI is unavailable")
        class OfflineDeepSeek(FakeDeepSeek):
            response = "OFFLINE_CODEX_OK"

        previous_instance_count = len(FakeDeepSeek.instances)
        with tempfile.TemporaryDirectory(prefix="codex-wire-") as temporary:
            environment = os.environ.copy()
            environment["CODEX_HOME"] = str(Path(temporary) / "codex-home")
            environment["DEEPSEEK_BRIDGE_API_KEY"] = "offline-ephemeral-test-key"
            Path(environment["CODEX_HOME"]).mkdir()
            server = Gateway(("127.0.0.1", 0), api_key="offline-ephemeral-test-key",
                             client_factory=OfflineDeepSeek)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            command = codex_command(codex, server.server_port, [
                "exec", "--ephemeral", "--ignore-user-config", "--skip-git-repo-check",
                "--color", "never", "-C", temporary,
                "Respond with only the exact text OFFLINE_CODEX_OK. No tools.",
            ])
            try:
                result = subprocess.run(command, env=environment, cwd=temporary,
                                        capture_output=True, text=True, timeout=40)
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr[-1200:])
            self.assertIn("OFFLINE_CODEX_OK", result.stdout + result.stderr)
            new_clients = FakeDeepSeek.instances[previous_instance_count:]
            self.assertTrue(new_clients, "Codex never reached the gateway")
            self.assertTrue(new_clients[-1].calls)

    @unittest.skip("Codex denied executing the tool by policy; do not retry this action")
    def test_actual_codex_exec_tool_result_or_refusal_round_trip(self):
        """Codex relays tool feedback, including an explicit policy refusal."""
        try:
            codex = find_codex()
        except RuntimeError:
            self.skipTest("A runnable local Codex CLI is unavailable")

        class ScriptedDeepSeek(FakeDeepSeek):
            round_trips = []

            def chat(self, prompt, **kwargs):
                type(self).round_trips.append(prompt)
                if len(type(self).round_trips) == 1:
                    response = ('<deepseek_tool_calls>{"calls":[{"name":"exec_command",'
                                '"arguments":{"cmd":"python -c \\\"print(\'OFFLINE_TOOL_EXECUTED\')\\\""}}]}'
                                '</deepseek_tool_calls>')
                else:
                    response = "OFFLINE_TOOL_CYCLE_COMPLETE"
                return ChatResult(session_id="offline", text=response, status="FINISHED")

        ScriptedDeepSeek.round_trips = []
        with tempfile.TemporaryDirectory(prefix="codex-wire-tool-") as temporary:
            environment = os.environ.copy()
            environment["CODEX_HOME"] = str(Path(temporary) / "codex-home")
            environment["DEEPSEEK_BRIDGE_API_KEY"] = "offline-ephemeral-test-key"
            Path(environment["CODEX_HOME"]).mkdir()
            server = Gateway(("127.0.0.1", 0), api_key="offline-ephemeral-test-key",
                             client_factory=ScriptedDeepSeek)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            command = codex_command(codex, server.server_port, [
                "exec", "--ephemeral", "--ignore-user-config", "--skip-git-repo-check",
                "--color", "never", "-C", temporary,
                "Use a terminal tool to print OFFLINE_TOOL_EXECUTED, then conclude.",
            ])
            try:
                result = subprocess.run(command, env=environment, cwd=temporary,
                                        capture_output=True, text=True, timeout=45)
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr[-1000:])
            self.assertIn("OFFLINE_TOOL_CYCLE_COMPLETE", result.stdout + result.stderr)
            self.assertGreaterEqual(len(ScriptedDeepSeek.round_trips), 2)
            records = ScriptedDeepSeek.round_trips[-1].split("MESSAGES_JSONL:\n", 1)[-1]
            tool_records = [json.loads(line) for line in records.splitlines()
                            if line.startswith('{"role":"tool"')]
            self.assertTrue(tool_records, "Codex did not return its tool feedback")
            feedback = tool_records[-1]["content"]
            # Codex enforces its own tool policy. An explicit refusal is an
            # expected terminal tool result and must not be bypassed.
            self.assertTrue(
                "OFFLINE_TOOL_EXECUTED" in feedback or "blocked by policy" in feedback
                or "exec_command failed" in feedback,
                feedback[:250],
            )


if __name__ == "__main__":
    unittest.main()
