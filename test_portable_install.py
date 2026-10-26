"""Smoke test an unpacked distribution without the original checkout tree."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT = Path(__file__).resolve().parent
PORTABLE_MODULES = (
    "anthropic_adapter.py", "api_client.py", "browser_login.py", "codex_stdio.py", "credential_store.py",
    "harness_adapter.py", "import_existing_session.py", "openai_server.py",
    "protocol_audit.py", "request_guard.py", "run_codex.py", "verify_session.py",
)


class PortableDistributionTests(unittest.TestCase):
    def test_extracted_python_modules_work_without_original_checkout(self):
        """No files from the checkout's parent tools directory are needed."""
        with tempfile.TemporaryDirectory(prefix="deepseek-portable-") as dirname:
            destination = Path(dirname)
            for filename in PORTABLE_MODULES:
                shutil.copy2(PROJECT / filename, destination / filename)
            self.assertFalse((destination.parent / "tools").exists())
            program = (
                "import api_client, browser_login, codex_stdio, credential_store, "
                "harness_adapter, import_existing_session, openai_server, protocol_audit, request_guard, run_codex, verify_session\n"
                "hash_value = api_client.deepseek_hash_v1(b's_1_1')\n"
                "challenge = {'salt':'s','expire_at':1,'difficulty':3,'challenge':hash_value.hex()}\n"
                "assert api_client.solve_pow_python(challenge) == 1\n"
                "assert browser_login.ORIGIN == 'https://chat.deepseek.com'\n"
                "print('PORTABLE_OK')\n"
            )
            result = subprocess.run(
                [sys.executable, "-c", program], cwd=destination,
                capture_output=True, text=True, timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("PORTABLE_OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
