"""Security regression tests for the local stdio protocol adapter."""

from __future__ import annotations

import contextlib
import io
import unittest
from unittest.mock import patch

from api_client import DeepSeekAPIError
import codex_stdio


class StdioSecurityTests(unittest.TestCase):
    def test_provider_error_does_not_print_private_response_to_stderr(self):
        marker = "PRIVATE_TOKEN_AND_USER_MESSAGE"
        request = '{"model":"deepseek-web","input":"Hello"}'
        output = io.StringIO()
        with patch("sys.stdin", io.StringIO(request)), contextlib.redirect_stderr(output), patch.object(
            codex_stdio.StdioHandler, "_run", side_effect=DeepSeekAPIError(
                "provider returned: " + marker, status=503,
            ),
        ):
            self.assertEqual(codex_stdio.main(), 1)
        self.assertNotIn(marker, output.getvalue())
        self.assertIn("DeepSeekAPIError", output.getvalue())

    def test_failed_responses_stream_sets_nonzero_exit_code(self):
        """Do not report success to Codex when SSE has response.failed."""
        from types import SimpleNamespace

        request = '{"model":"deepseek-web","input":"Hello","stream":true}'
        output = io.BytesIO()
        stdout = SimpleNamespace(buffer=output)
        with patch("sys.stdin", io.StringIO(request)), patch("sys.stdout", stdout), \
             contextlib.redirect_stderr(io.StringIO()), patch.object(
                 codex_stdio.StdioHandler, "_run", side_effect=DeepSeekAPIError(
                     "provider rejected the request", status=503,
                 ),
             ):
            code = codex_stdio.main()
        events = output.getvalue().decode("utf-8")
        self.assertIn("event: response.failed", events)
        self.assertEqual(code, 1)

    def test_successful_responses_stream_exits_zero(self):
        from types import SimpleNamespace
        from openai_server import Completion

        output = io.BytesIO()
        stdout = SimpleNamespace(buffer=output)

        def generate(_prepared, on_text=None, **_kwargs):
            if on_text:
                on_text("OK")
            return Completion("OK", None)

        with patch("sys.stdin", io.StringIO('{"model":"deepseek-web","input":"Hi","stream":true}')), \
             patch("sys.stdout", stdout), patch.object(codex_stdio.StdioHandler, "_run", side_effect=generate):
            code = codex_stdio.main()
        content = output.getvalue().decode("utf-8")
        self.assertEqual(code, 0)
        self.assertIn("event: response.completed", content)
        self.assertIn("event: response.output_text.delta", content)
        self.assertNotIn("event: response.failed", content)

    def test_non_boolean_stream_flag_is_rejected_before_generation(self):
        captured = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        with patch("sys.stdin", io.StringIO('{"model":"deepseek-web","input":"Hi","stream":"true"}')), \
             patch("sys.stdout", captured), contextlib.redirect_stderr(io.StringIO()), \
             patch.object(codex_stdio.StdioHandler, "_run") as generate:
            self.assertEqual(codex_stdio.main(), 1)
        generate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
