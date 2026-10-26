"""Native PoW portability tests without modifying installed project files."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import api_client


class SolverPortabilityTests(unittest.TestCase):
    def test_native_solver_wrong_nonce_is_verified_and_recovered_in_python(self):
        challenge = {"algorithm": "DeepSeekHashV1", "salt": "salt", "expire_at": 1,
                     "difficulty": 4, "challenge": api_client.deepseek_hash_v1(b"salt_1_2").hex()}
        wrong = SimpleNamespace(returncode=0, stdout="1\n", stderr="")
        with patch.object(api_client, "_ensure_native_solver", return_value=Path("fake-solver")), \
             patch.object(api_client.subprocess, "run", return_value=wrong):
            self.assertEqual(api_client.solve_pow(challenge), 2)

    def test_native_solver_runtime_error_uses_python_fallback(self):
        challenge = {"algorithm": "DeepSeekHashV1", "salt": "salt", "expire_at": 1,
                     "difficulty": 4, "challenge": api_client.deepseek_hash_v1(b"salt_1_2").hex()}
        failed = SimpleNamespace(returncode=1, stdout="-1\n", stderr="native solver error")
        with patch.object(api_client, "_ensure_native_solver", return_value=Path("fake-solver")), \
             patch.object(api_client.subprocess, "run", return_value=failed):
            self.assertEqual(api_client.solve_pow(challenge), 2)

    def test_compiles_real_native_solver_with_no_project_source_or_binary(self):
        if not any(shutil.which(c) for c in ("gcc", "clang", "cc")):
            self.skipTest("C compiler not installed on this host")
        with tempfile.TemporaryDirectory(prefix="pow-portable-") as dirname:
            root = Path(dirname)
            cached = root / "user-cache" / ("solver.exe" if os.name == "nt" else "solver")
            with patch.object(api_client, "SOLVER_EXE", root / "missing-binary"), \
                 patch.object(api_client, "SOLVER_SRC", root / "missing-source.c"), \
                 patch.object(api_client, "_solver_cache_path", return_value=cached), \
                 patch.object(api_client, "_native_solver_checked", False), \
                 patch.object(api_client, "_native_solver_path", None):
                self.assertEqual(api_client._ensure_native_solver(), cached)
                self.assertEqual(api_client.solve_pow(api_client.KNOWN_POW),
                                 api_client.KNOWN_POW["answer"])
            self.assertTrue(cached.is_file())
            self.assertFalse((root / "missing-source.c").exists())

    def test_wrong_platform_binary_is_rebuilt_outside_readonly_code_directory(self):
        with tempfile.TemporaryDirectory(prefix="pow-portable-") as dirname:
            root = Path(dirname)
            bundled = root / "read-only-code" / "ds_pow_solver"
            bundled.parent.mkdir()
            bundled.write_bytes(b"wrong architecture executable")
            cache = root / "user-cache" / "ds_pow_solver-test"
            commands = []

            def compile_mock(command, **_kwargs):
                commands.append(command)
                Path(command[command.index("-o") + 1]).write_bytes(b"compiled solver")
                return SimpleNamespace(returncode=0)

            def validate(path):
                return path == cache and path.is_file()

            with patch.object(api_client, "SOLVER_EXE", bundled), \
                 patch.object(api_client, "SOLVER_SRC", root / "missing-project-source.c"), \
                 patch.object(api_client, "_solver_cache_path", return_value=cache), \
                 patch.object(api_client, "_valid_native_solver", side_effect=validate), \
                 patch.object(api_client.shutil, "which", return_value="gcc"), \
                 patch.object(api_client.subprocess, "run", side_effect=compile_mock), \
                 patch.object(api_client, "_native_solver_checked", False), \
                 patch.object(api_client, "_native_solver_path", None):
                self.assertEqual(api_client._ensure_native_solver(), cache)
                self.assertEqual(api_client._ensure_native_solver(), cache)
            self.assertEqual(len(commands), 1)
            self.assertNotIn("-march=native", commands[0])
            self.assertEqual(bundled.read_bytes(), b"wrong architecture executable")
            self.assertIn("static void ds_hash", cache.with_suffix(".c").read_text(encoding="utf-8"))

    def test_missing_compiler_keeps_python_fallback(self):
        with tempfile.TemporaryDirectory(prefix="pow-portable-") as dirname:
            root = Path(dirname)
            with patch.object(api_client, "SOLVER_EXE", root / "nonexistent"), \
                 patch.object(api_client, "_solver_cache_path", return_value=root / "cache" / "solver"), \
                 patch.object(api_client.shutil, "which", return_value=None), \
                 patch.object(api_client, "_native_solver_checked", False), \
                 patch.object(api_client, "_native_solver_path", None):
                expected = api_client.deepseek_hash_v1(b"salt_1_1").hex()
                self.assertEqual(api_client.solve_pow({"algorithm": "DeepSeekHashV1",
                                                       "salt": "salt", "expire_at": 1,
                                                       "difficulty": 3, "challenge": expected}), 1)

    def test_invalid_executable_is_not_trusted(self):
        with patch.object(api_client.subprocess, "run", side_effect=OSError("wrong format")):
            self.assertFalse(api_client._valid_native_solver(Path("incompatible-executable")))

    def test_cache_path_changes_with_cpu_and_source(self):
        with patch.object(api_client.platform, "machine", return_value="aarch64"):
            arm = api_client._solver_cache_path()
        with patch.object(api_client.platform, "machine", return_value="x86_64"):
            intel = api_client._solver_cache_path()
        self.assertNotEqual(arm, intel)
        self.assertIn("aarch64", str(arm))


if __name__ == "__main__":
    unittest.main()
