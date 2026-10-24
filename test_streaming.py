"""Tests for the real-time JSONL CLI output (no network required)."""

import contextlib
import io
import json
import unittest
from unittest.mock import patch

from api_client import ChatResult, StreamChunk, main


class StreamingCLITests(unittest.TestCase):
    def test_stream_sends_thinking_and_text_before_done(self) -> None:
        seen = {}

        class FakeClient:
            def __init__(self, **_kwargs):
                pass

            def chat(self, prompt, **kwargs):
                seen["prompt"] = prompt
                seen["thinking_enabled"] = kwargs["thinking_enabled"]
                on_chunk = kwargs["on_chunk"]
                on_chunk(StreamChunk(event="message", thinking="Reasoning " ))
                on_chunk(StreamChunk(event="message", text="Response"))
                on_chunk(StreamChunk(event="title", title="Test chat"))
                return ChatResult(
                    session_id="session-123",
                    text="Response",
                    thinking="Reasoning ",
                    status="FINISHED",
                    request_message_id=1,
                    response_message_id=2,
                    events=3,
                )

        output = io.StringIO()
        with patch("api_client.DeepSeekClient", FakeClient):
            with contextlib.redirect_stdout(output):
                code = main(["--stream", "--thinking", "--quiet", "Hello"])

        self.assertEqual(code, 0)
        self.assertEqual(seen, {"prompt": "Hello", "thinking_enabled": True})
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([event["type"] for event in events], ["thinking", "text", "title", "done"])
        self.assertEqual(events[0]["delta"], "Reasoning ")
        self.assertEqual(events[1]["delta"], "Response")
        self.assertEqual(events[2]["title"], "Test chat")
        self.assertEqual(events[3]["status"], "FINISHED")
        self.assertEqual(events[3]["session_id"], "session-123")


if __name__ == "__main__":
    unittest.main()
