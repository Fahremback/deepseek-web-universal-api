"""Exercise the real SSE continuation state machine without network calls."""

import unittest
from unittest.mock import patch

from api_client import CompletionAccumulator, DeepSeekAPIError, DeepSeekClient


class FakeSSE:
    def __init__(self, events: str):
        self.events = events
        self.closed = False

    def iter_lines(self, chunk_size=1024):
        return iter(line.encode() for line in self.events.split("\n"))

    def close(self):
        self.closed = True


INITIAL = """event: ready
data: {"request_message_id":1,"response_message_id":2}

data: {"v":{"response":{"message_id":2,"status":"WIP","fragments":[{"type":"RESPONSE","content":"Parte "}]}}}

data: {"p":"response/status","o":"SET","v":"INCOMPLETE"}

event: close
data: {}

"""

CONTINUED = """data: {"v":{"response":{"message_id":2,"status":"WIP","fragments":[{"type":"RESPONSE","content":"Parte final"}]}}}

data: {"p":"response/status","o":"SET","v":"FINISHED"}

event: close
data: {}

"""

RESET_AND_REPLAY = """data: {"v":{"response":{"message_id":2,"status":"WIP","fragments":[]}}}

data: {"v":{"response":{"message_id":2,"status":"WIP","fragments":[{"type":"RESPONSE","content":"Parte "}]}}}

data: {"v":{"response":{"message_id":2,"status":"WIP","fragments":[{"type":"RESPONSE","content":"Parte final"}]}}}

data: {"p":"response/status","o":"SET","v":"FINISHED"}

event: close
data: {}

"""

CONTINUED_WITH_STALE_QUASI = """data: {"p":"response/quasi_status","o":"SET","v":"INCOMPLETE"}

data: {"p":"response/status","o":"SET","v":"FINISHED"}

event: close
data: {}

"""


class AutoContinueTests(unittest.TestCase):
    def test_done_marker_with_nonterminal_status_is_rejected(self):
        partial = '''data: {"v":{"response":{"message_id":2,"status":"WIP","fragments":[{"type":"RESPONSE","content":"partial"}]}}}

data: [DONE]

'''
        client = DeepSeekClient(token="test-token")
        with patch.object(client, "create_completion_stream", return_value=(FakeSSE(partial), CompletionAccumulator())):
            with self.assertRaisesRegex(DeepSeekAPIError, "WIP"):
                list(client.stream_completion(chat_session_id="session", prompt="long"))
        client.session.close()

    def test_done_marker_with_finished_status_is_accepted(self):
        finished = '''data: {"v":{"response":{"message_id":2,"status":"FINISHED","fragments":[{"type":"RESPONSE","content":"complete"}]}}}

data: [DONE]

'''
        client = DeepSeekClient(token="test-token")
        with patch.object(client, "create_completion_stream", return_value=(FakeSSE(finished), CompletionAccumulator())):
            chunks = list(client.stream_completion(chat_session_id="session", prompt="short"))
        self.assertEqual("".join(chunk.text for chunk in chunks), "complete")
        self.assertEqual(client.last_accumulator.status, "FINISHED")
        self.assertEqual([chunk.done for chunk in chunks if chunk.done], [True])
        client.session.close()

    def test_done_followed_by_close_emits_only_one_terminal_chunk(self):
        finished = '''data: {"v":{"response":{"message_id":2,"status":"FINISHED","fragments":[{"type":"RESPONSE","content":"complete"}]}}}

data: [DONE]

event: close
data: {}

'''
        client = DeepSeekClient(token="test-token")
        with patch.object(client, "create_completion_stream", return_value=(FakeSSE(finished), CompletionAccumulator())):
            chunks = list(client.stream_completion(chat_session_id="session", prompt="short"))
        self.assertEqual(sum(chunk.done for chunk in chunks), 1)
        client.session.close()

    def test_incomplete_done_marker_continues_and_emits_final_done(self):
        initial = INITIAL.replace("event: close\ndata: {}", "data: [DONE]")
        finished = CONTINUED.replace("event: close\ndata: {}", "data: [DONE]")
        self.assertNotEqual(initial, INITIAL)
        self.assertNotEqual(finished, CONTINUED)
        client = DeepSeekClient(token="test-token")
        with patch.object(client, "create_completion_stream", return_value=(FakeSSE(initial), CompletionAccumulator())):
            with patch.object(client, "continue_completion_stream", return_value=FakeSSE(finished)) as continuation:
                chunks = list(client.stream_completion(chat_session_id="session", prompt="long"))
        self.assertEqual("".join(chunk.text for chunk in chunks), "Parte final")
        self.assertEqual([chunk.done for chunk in chunks if chunk.event == "message" and chunk.raw == "[DONE]"], [False, True])
        self.assertEqual(client.last_accumulator.continuations, 1)
        continuation.assert_called_once_with(chat_session_id="session", message_id=2)
        client.session.close()

    def test_close_while_still_generating_does_not_complete(self):
        partial = '''event: ready
data: {"request_message_id":1,"response_message_id":2}

data: {"v":{"response":{"message_id":2,"status":"WIP","fragments":[{"type":"RESPONSE","content":"partial"}]}}}

event: close
data: {}

'''
        client = DeepSeekClient(token="test-token")
        with patch.object(client, "create_completion_stream", return_value=(FakeSSE(partial), CompletionAccumulator())):
            chunks = []
            with self.assertRaisesRegex(DeepSeekAPIError, "WIP"):
                for chunk in client.stream_completion(chat_session_id="session", prompt="long"):
                    chunks.append(chunk)
        self.assertFalse(any(chunk.done for chunk in chunks))
        client.session.close()

    def test_failed_status_does_not_complete(self):
        failed = '''data: {"v":{"response":{"message_id":2,"status":"FAILED","fragments":[{"type":"RESPONSE","content":"partial"}]}}}

event: close
data: {}

'''
        client = DeepSeekClient(token="test-token")
        with patch.object(client, "create_completion_stream", return_value=(FakeSSE(failed), CompletionAccumulator())):
            with self.assertRaisesRegex(DeepSeekAPIError, "FAILED"):
                list(client.stream_completion(chat_session_id="session", prompt="long"))
        client.session.close()

    def test_final_status_overrides_stale_incomplete_quasi_status(self):
        acc = CompletionAccumulator()
        acc.feed("message", '{"v":{"response":{"status":"WIP","fragments":[]}}}')
        acc.feed("message", '{"p":"response/quasi_status","o":"SET","v":"INCOMPLETE"}')
        acc.feed("message", '{"p":"response/status","o":"SET","v":"FINISHED"}')
        self.assertEqual(acc.status, "FINISHED")

    def test_thinking_to_response_switch_keeps_implicit_content_path(self):
        acc = CompletionAccumulator()
        acc.feed("message", '{"v":{"response":{"status":"WIP","fragments":[{"type":"THINK","content":"Thought"}]}}}')
        acc.feed("message", '{"p":"response/fragments","o":"APPEND","v":[{"type":"RESPONSE","content":"Um"}]}')
        first, _ = acc.feed("message", '{"p":"response/fragments/-1/content","v":" planeta"}')
        second, _ = acc.feed("message", '{"v":" azul"}')
        self.assertEqual((first, second), (" planeta", " azul"))
        self.assertEqual(acc.text, "Um planeta azul")
        self.assertEqual(len(acc.response["fragments"]), 2)

    def test_incomplete_triggers_continue_without_duplicate_text(self):
        client = DeepSeekClient(token="test-token")
        first, second = FakeSSE(INITIAL), FakeSSE(CONTINUED)
        with patch.object(client, "create_completion_stream", return_value=(first, CompletionAccumulator())):
            with patch.object(client, "continue_completion_stream", return_value=second) as continuation:
                chunks = list(client.stream_completion(chat_session_id="session", prompt="long"))
        self.assertEqual("".join(c.text for c in chunks), "Parte final")
        self.assertEqual([c.attempt for c in chunks if c.event == "continue"], [1])
        self.assertEqual([c.done for c in chunks if c.event == "close"], [False, True])
        self.assertEqual(client.last_accumulator.status, "FINISHED")
        self.assertEqual(client.last_accumulator.continuations, 1)
        continuation.assert_called_once_with(chat_session_id="session", message_id=2)
        self.assertTrue(first.closed and second.closed)
        client.session.close()

    def test_disable_continue_retains_incomplete_status(self):
        client = DeepSeekClient(token="test-token")
        with patch.object(client, "create_completion_stream", return_value=(FakeSSE(INITIAL), CompletionAccumulator())):
            with patch.object(client, "continue_completion_stream") as continuation:
                chunks = list(client.stream_completion(chat_session_id="session", prompt="long", auto_continue=False))
        self.assertTrue(chunks[-1].done)
        self.assertEqual(client.last_accumulator.status, "INCOMPLETE")
        continuation.assert_not_called()
        client.session.close()

    def test_continuation_stops_on_final_status_even_with_stale_quasi(self):
        client = DeepSeekClient(token="test-token")
        with patch.object(client, "create_completion_stream", return_value=(FakeSSE(INITIAL), CompletionAccumulator())):
            with patch.object(client, "continue_completion_stream", return_value=FakeSSE(CONTINUED_WITH_STALE_QUASI)) as continuation:
                chunks = list(client.stream_completion(chat_session_id="session", prompt="long"))
        self.assertEqual(client.last_accumulator.status, "FINISHED")
        self.assertEqual(client.last_accumulator.continuations, 1)
        self.assertTrue(chunks[-1].done)
        continuation.assert_called_once()
        client.session.close()

    def test_replayed_prefix_after_reset_is_not_streamed_twice(self):
        client = DeepSeekClient(token="test-token")
        with patch.object(client, "create_completion_stream", return_value=(FakeSSE(INITIAL), CompletionAccumulator())):
            with patch.object(client, "continue_completion_stream", return_value=FakeSSE(RESET_AND_REPLAY)):
                chunks = list(client.stream_completion(chat_session_id="session", prompt="long"))
        self.assertEqual("".join(c.text for c in chunks), "Parte final")
        self.assertEqual(client.last_accumulator.text, "Parte final")
        client.session.close()

    def test_final_rewrite_must_not_silently_disagree_with_streamed_text(self):
        rewritten = '''data: {"v":{"response":{"message_id":2,"status":"WIP","fragments":[{"type":"RESPONSE","content":"Outra resposta"}]}}}

data: {"p":"response/status","o":"SET","v":"FINISHED"}

event: close
data: {}

'''
        client = DeepSeekClient(token="test-token")
        with patch.object(client, "create_completion_stream", return_value=(FakeSSE(INITIAL), CompletionAccumulator())):
            with patch.object(client, "continue_completion_stream", return_value=FakeSSE(rewritten)):
                chunks = []
                with self.assertRaisesRegex(DeepSeekAPIError, "diverg"):
                    for chunk in client.stream_completion(chat_session_id="session", prompt="long"):
                        chunks.append(chunk)
        self.assertFalse(any(chunk.done for chunk in chunks))
        client.session.close()

    def test_continue_cap_does_not_report_success(self):
        client = DeepSeekClient(token="test-token")
        with patch.object(client, "create_completion_stream", return_value=(FakeSSE(INITIAL), CompletionAccumulator())):
            with self.assertRaisesRegex(DeepSeekAPIError, "INCOMPLETE"):
                list(client.stream_completion(chat_session_id="session", prompt="long", max_continue_attempts=0))
        client.session.close()


if __name__ == "__main__":
    unittest.main()
