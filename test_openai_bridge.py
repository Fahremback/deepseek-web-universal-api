"""Offline OpenAI-protocol gateway integration tests (no DeepSeek request)."""
from __future__ import annotations

import base64
import gzip
import json
import threading
import time
import unittest
from unittest.mock import patch

import requests
import zstandard

from api_client import ChatResult, DeepSeekAPIError
from anthropic_adapter import normalize_anthropic_request
from harness_adapter import RequestError, normalize_responses_input, parse_tool_calls, prepare_chat
from openai_server import Gateway


CALCULATOR = {
    "type": "function",
    "function": {
        "name": "calculator", "description": "Calculates a sum",
        "parameters": {
            "type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"], "additionalProperties": False,
        },
    },
}
CALLS = '<deepseek_tool_calls>{"calls":[{"name":"calculator","arguments":{"a":2,"b":3}}]}</deepseek_tool_calls>'
DSML_CALLS = ('<｜｜DSML｜｜ calls>\n<｜｜DSML｜｜ invoke name="exec_command">\n'
              '<｜｜DSML｜｜ parameter name="cmd" string="true">pwd</｜｜DSML｜｜ parameter>\n'
              '</｜｜DSML｜｜ invoke>\n</｜｜DSML｜｜ calls>')


class FakeDeepSeek:
    instances: list["FakeDeepSeek"] = []
    response = "OK"

    def __init__(self):
        self.calls = []
        self.session = self
        FakeDeepSeek.instances.append(self)

    def close(self):
        pass

    def upload_files(self, files, **kwargs):
        self.upload_args = (files, kwargs)
        return [{"file_id": f"mock-{i}"} for i, _ in enumerate(files)]

    def chat(self, prompt: str, **kwargs):
        self.calls.append((prompt, kwargs))
        output = type(self).response
        callback = kwargs.get("on_text")
        if callback:
            for bit in [output[:2], output[2:]]:
                if bit:
                    callback(bit)
        return ChatResult(session_id="fake", text=output, status="FINISHED")


class TestAdapter(unittest.TestCase):
    def test_roles_and_prior_tool_messages(self):
        req = prepare_chat({"messages": [
            {"role": "system", "content": "Important"},
            {"role": "assistant", "tool_calls": [{"id": "call_1", "type": "function",
                "function": {"name": "calculator", "arguments": '{"a":2,"b":3}'}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "5"},
            {"role": "user", "content": "Answer"},
        ]})
        self.assertIn('"role":"system","content":"Important"', req.prompt)
        self.assertIn('"role":"tool","content":"5","tool_call_id":"call_1"', req.prompt)

    def test_media_data_uri(self):
        uri = "data:image/png;base64," + base64.b64encode(b"test_image").decode()
        req = prepare_chat({"messages": [{"role": "user", "content": [
            {"type": "text", "text": "Look"},
            {"type": "image_url", "image_url": {"url": uri}},
        ]}]})
        self.assertEqual(req.attachments, [("image_1.png", b"test_image")])
        self.assertIn("[Attached image: image_1.png]", req.prompt)

    def test_remote_image_url_denied(self):
        with self.assertRaises(RequestError):
            prepare_chat({"messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": "http://127.0.0.1/secrets"}}
            ]}]})

    def test_tool_schema(self):
        req = prepare_chat({"messages": [{"role": "user", "content": "2+3?"}],
                            "tools": [CALCULATOR], "tool_choice": "required"})
        calls = parse_tool_calls(CALLS, req.tools, req.tool_choice)
        self.assertEqual(calls[0]["function"]["name"], "calculator")
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), {"a": 2, "b": 3})
        self.assertIsNone(parse_tool_calls(CALLS.replace('"a":2', '"a":"wrong"'),
                                           req.tools, req.tool_choice))
        self.assertIsNone(parse_tool_calls("No tools", req.tools, req.tool_choice))

    def test_custom_tool_schema(self):
        tool = {"type": "custom", "name": "exec_command", "description": "Run a command",
                "format": {"type": "text"}}
        req = prepare_chat({"messages": [{"role": "user", "content": "Run pwd"}],
                            "tools": [tool], "tool_choice": "required"})
        calls = parse_tool_calls('<deepseek_tool_calls>{"calls":[{"name":"exec_command",'
                                 '"arguments":{"input":"pwd"}}]}</deepseek_tool_calls>',
                                 req.tools, req.tool_choice)
        self.assertEqual(calls[0]["type"], "custom")
        self.assertEqual(calls[0]["input"], "pwd")

    def test_native_dsml_custom_tool_call(self):
        tool = {"type": "custom", "name": "exec_command", "format": {"type": "text"}}
        req = prepare_chat({"messages": [{"role": "user", "content": "Run pwd"}], "tools": [tool]})
        calls = parse_tool_calls(DSML_CALLS, req.tools, req.tool_choice)
        self.assertEqual(calls[0]["type"], "custom")
        self.assertEqual(json.loads(calls[0]["input"]), {"cmd": "pwd"})

    def test_custom_tool_responses_input(self):
        req = normalize_responses_input({"input": [
            {"type": "custom_tool_call", "call_id": "call_xyz", "name": "exec_command", "input": "pwd"},
            {"type": "custom_tool_call_output", "call_id": "call_xyz", "output": "C:\\temp"},
        ]})
        self.assertEqual(req["messages"][1]["tool_call_id"], "call_xyz")
        self.assertEqual(req["messages"][0]["tool_calls"][0]["function"]["name"], "exec_command")

    def test_responses_input(self):
        req = normalize_responses_input({
            "input": [{"type": "function_call_output", "call_id": "call_1", "output": "5"}],
            "model": "deepseek-web",
        }, previous=[{"role": "assistant", "content": "Earlier"}])
        self.assertEqual(req["messages"][0]["role"], "assistant")
        self.assertEqual(req["messages"][1]["tool_call_id"], "call_1")

    def test_responses_tool_output_content_parts_are_preserved(self):
        req = normalize_responses_input({"model": "deepseek-web", "input": [
            {"type": "function_call_output", "call_id": "call_1", "output": [
                {"type": "input_text", "text": "Exit code: 0"},
                {"type": "input_text", "text": "Files changed"},
            ]},
        ]})
        prepared = prepare_chat(req)
        self.assertIn("Exit code: 0", prepared.prompt)
        self.assertIn("Files changed", prepared.prompt)
        self.assertNotIn("'type': 'input_text'", prepared.prompt)

    def test_responses_tool_call_missing_id_is_rejected(self):
        for typ in ("function_call", "function_call_output", "custom_tool_call_output"):
            with self.subTest(typ=typ), self.assertRaises(RequestError):
                normalize_responses_input({"model": "deepseek-web", "input": [
                    {"type": typ, "name": "calculator", "output": "5"},
                ]})

    def test_anthropic_tool_cycle_conversion(self):
        req = normalize_anthropic_request({
            "model": "claude-sonnet-4-5",
            "system": "Be exact.",
            "messages": [
                {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_123",
                    "name": "calculator", "input": {"a": 2, "b": 3}}]},
                {"role": "user", "content": [{"type": "tool_result",
                    "tool_use_id": "toolu_123", "content": "5"}]},
            ],
            "tools": [{"name": "calculator", "input_schema": CALCULATOR["function"]["parameters"]}],
        })
        self.assertEqual(req["model"], "deepseek-web")
        self.assertEqual(req["messages"][2]["role"], "tool")
        self.assertEqual(req["messages"][2]["tool_call_id"], "toolu_123")



class TestHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = Gateway(("127.0.0.1", 0), api_key="test-secret",
                             client_factory=FakeDeepSeek)
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"
        cls.worker = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.worker.join(timeout=3)

    def setUp(self):
        FakeDeepSeek.instances.clear()
        FakeDeepSeek.response = "OK"
        self.headers = {"Authorization": "Bearer test-secret"}

    def post(self, path, body, *, stream=False):
        return requests.post(self.url + path, json=body, headers=self.headers,
                             timeout=10, stream=stream)

    def test_models_and_auth(self):
        self.assertEqual(requests.get(self.url + "/health", timeout=3).status_code, 200)
        self.assertEqual(requests.get(self.url + "/v1/models", timeout=3).status_code, 401)
        r = requests.get(self.url + "/v1/models", headers=self.headers, timeout=3)
        self.assertEqual(r.status_code, 200)
        self.assertIn("deepseek-web-reasoner", [x["id"] for x in r.json()["data"]])

    def test_upstream_cooldown_returns_http_429_and_retry_after(self):
        from request_guard import RateLimitPause

        with patch.object(self.server, "client_factory", side_effect=RateLimitPause(74.2)):
            result = self.post("/v1/responses", {
                "model": "deepseek-web", "input": "offline check", "stream": False,
            })
        self.assertEqual(result.status_code, 429, result.text)
        self.assertEqual(result.headers.get("Retry-After"), "75")
        self.assertEqual(result.json()["error"]["type"], "rate_limit_error")

    def test_unreadable_limit_state_returns_http_503(self):
        from request_guard import RateLimitStateError

        with patch.object(self.server, "client_factory", side_effect=RateLimitStateError("private details")):
            result = self.post("/v1/responses", {
                "model": "deepseek-web", "input": "offline check", "stream": False,
            })
        self.assertEqual(result.status_code, 503, result.text)
        self.assertEqual(result.json()["error"]["type"], "rate_limit_state_error")
        self.assertNotIn("private details", result.text)
        self.assertEqual(result.headers.get("Retry-After"), "60")

    def test_shared_limit_state_failure_is_visible_in_all_streams(self):
        from request_guard import RateLimitStateError

        cases = (
            ("/v1/responses", {"model": "deepseek-web", "input": "test", "stream": True}),
            ("/v1/chat/completions", {"model": "deepseek-web", "messages": [
                {"role": "user", "content": "test"}], "stream": True}),
            ("/v1/completions", {"model": "deepseek-web", "prompt": "test", "stream": True}),
            ("/v1/messages", {"model": "deepseek-web", "max_tokens": 32, "messages": [
                {"role": "user", "content": "test"}], "stream": True}),
        )
        for endpoint, body in cases:
            with self.subTest(endpoint=endpoint), patch.object(
                self.server, "client_factory",
                side_effect=RateLimitStateError("PRIVATE_CONTENT_NEVER_SHOW"),
            ):
                with self.post(endpoint, body, stream=True) as result:
                    self.assertEqual(result.status_code, 200)
                    self.assertNotIn("PRIVATE_CONTENT_NEVER_SHOW", result.text)
                    events = [json.loads(line[6:]) for line in result.text.splitlines()
                              if line.startswith("data: {")]
                errors = [event["error"] for event in events if "error" in event]
                self.assertEqual(len(errors), 1)
                self.assertEqual(errors[0]["type"], "rate_limit_state_error")

    def test_first_upstream_http_429_is_not_disguised_as_502(self):
        from api_client import DeepSeekAPIError

        upstream = DeepSeekAPIError("sensitive upstream response", status=429)
        with patch.object(self.server, "client_factory", side_effect=upstream):
            result = self.post("/v1/responses", {
                "model": "deepseek-web", "input": "offline check", "stream": False,
            })
        self.assertEqual(result.status_code, 429, result.text)
        self.assertEqual(result.json()["error"]["type"], "rate_limit_error")
        self.assertNotIn("sensitive upstream response", result.text)

    def test_legacy_stream_cooldown_exposes_retry_after_without_private_text(self):
        from request_guard import RateLimitPause

        with patch.object(self.server, "client_factory", side_effect=RateLimitPause(15.3)):
            with self.post("/v1/completions", {"model": "deepseek-web", "prompt": "Hi",
                           "stream": True}, stream=True) as result:
                self.assertEqual(result.status_code, 200)
                events = [json.loads(line[6:]) for line in result.text.splitlines()
                          if line.startswith("data: {")]
        errors = [event["error"] for event in events if "error" in event]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["type"], "rate_limit_error")
        self.assertEqual(errors[0]["retry_after"], 16)

    def test_anthropic_stream_cooldown_exposes_retry_after(self):
        from request_guard import RateLimitPause

        with patch.object(self.server, "client_factory", side_effect=RateLimitPause(25.2)):
            with self.post("/v1/messages", {"model": "deepseek-web", "max_tokens": 64,
                           "messages": [{"role": "user", "content": "Hi"}],
                           "stream": True}, stream=True) as result:
                self.assertEqual(result.status_code, 200)
                events = [json.loads(line[6:]) for line in result.text.splitlines()
                          if line.startswith("data: {")]
        errors = [event["error"] for event in events if event.get("type") == "error"]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["type"], "rate_limit_error")
        self.assertEqual(errors[0]["retry_after"], 26)

    def test_brief_busy_slot_queues_without_exceeding_parallel_limit(self):
        self.assertTrue(self.server.slots.acquire(timeout=2))
        def release_slot():
            time.sleep(0.08)
            self.server.slots.release()

        release = threading.Thread(target=release_slot, daemon=True)
        release.start()
        try:
            result = self.post("/v1/responses", {
                "model": "deepseek-web", "input": "hello", "stream": False,
            })
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()["output_text"], "OK")
        finally:
            release.join(timeout=2)
            self.assertFalse(release.is_alive())

    def test_busy_slot_still_rejects_requests_after_bounded_wait(self):
        self.assertTrue(self.server.slots.acquire(timeout=2))
        try:
            result = self.post("/v1/responses", {
                "model": "deepseek-web", "input": "hello", "stream": False,
            })
            self.assertEqual(result.status_code, 429)
            self.assertEqual(result.headers.get("Retry-After"), "1")
            self.assertEqual(result.json()["error"]["type"], "rate_limit_error")
        finally:
            self.server.slots.release()

    def test_legacy_and_anthropic_stream_remote_429_is_sanitized(self):
        from api_client import DeepSeekAPIError

        cases = (
            ("/v1/completions", {"model": "deepseek-web", "prompt": "Hi", "stream": True}),
            ("/v1/messages", {"model": "deepseek-web", "max_tokens": 64,
                              "messages": [{"role": "user", "content": "Hi"}], "stream": True}),
        )
        for endpoint, body in cases:
            with self.subTest(endpoint=endpoint), patch.object(
                self.server, "client_factory", side_effect=DeepSeekAPIError(
                    "UPSTREAM_PRIVATE_RESPONSE_123", status=429
                )
            ):
                with self.post(endpoint, body, stream=True) as result:
                    self.assertEqual(result.status_code, 200)
                    self.assertNotIn("UPSTREAM_PRIVATE_RESPONSE_123", result.text)
                    events = [json.loads(line[6:]) for line in result.text.splitlines()
                              if line.startswith("data: {")]
                error = next(event["error"] for event in events if "error" in event)
                self.assertEqual(error["type"], "rate_limit_error")

    def test_zstd_compressed_request(self):
        body = {"model": "deepseek-web", "input": "Respond OK", "stream": False}
        encoded = zstandard.ZstdCompressor().compress(json.dumps(body).encode("utf-8"))
        r = requests.post(self.url + "/v1/responses", data=encoded,
                          headers={**self.headers, "Content-Type": "application/json",
                                   "Content-Encoding": "zstd"}, timeout=10)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["output_text"], "OK")

    def test_gzip_compressed_request(self):
        body = {"model": "deepseek-web", "input": "Respond OK", "stream": False}
        encoded = gzip.compress(json.dumps(body).encode("utf-8"))
        r = requests.post(self.url + "/v1/responses", data=encoded,
                          headers={**self.headers, "Content-Type": "application/json",
                                   "Content-Encoding": "gzip"}, timeout=10)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["output_text"], "OK")

    def test_gzip_expansion_limit_and_corrupt_data(self):
        body = {"model": "deepseek-web", "input": "A" * 4000}
        encoded = gzip.compress(json.dumps(body).encode("utf-8"))
        headers = {**self.headers, "Content-Type": "application/json", "Content-Encoding": "gzip"}
        with patch("openai_server.MAX_REQUEST_BYTES", 1024):
            r = requests.post(self.url + "/v1/responses", data=encoded,
                              headers=headers, timeout=10)
            self.assertEqual(r.status_code, 400, r.text)
            self.assertEqual(r.json()["error"]["message"], "Decompressed request is too large")
        r = requests.post(self.url + "/v1/responses", data=encoded[:-6],
                          headers=headers, timeout=10)
        self.assertEqual(r.status_code, 400, r.text)

    def test_error_response_includes_allowed_local_origin(self):
        origin = "http://localhost:3000"
        r = requests.get(self.url + "/v1/models", headers={"Origin": origin}, timeout=3)
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.headers["Access-Control-Allow-Origin"], origin)
        r = requests.post(self.url + "/v1/chat/completions", json={"messages": []},
                          headers={**self.headers, "Origin": origin}, timeout=3)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.headers["Access-Control-Allow-Origin"], origin)
        r = requests.get(self.url + "/v1/models", headers={"Origin": "https://evil.example"}, timeout=3)
        self.assertEqual(r.status_code, 403)
        self.assertNotIn("Access-Control-Allow-Origin", r.headers)

    def test_custom_tool_responses(self):
        FakeDeepSeek.response = ('<deepseek_tool_calls>{"calls":[{"name":"exec_command",'
                                 '"arguments":{"input":"pwd"}}]}</deepseek_tool_calls>')
        body = {"model": "deepseek-web", "input": "Run pwd", "tool_choice": "required",
                "tools": [{"type": "custom", "name": "exec_command", "description": "Run", "format": {"type": "text"}}]}
        r = self.post("/v1/responses", body)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["output"][0]["type"], "custom_tool_call")
        self.assertEqual(r.json()["output"][0]["input"], "pwd")
        with self.post("/v1/responses", {**body, "stream": True}, stream=True) as streamed:
            self.assertEqual(streamed.status_code, 200)
            content = streamed.text
        self.assertIn("response.custom_tool_call_input.done", content)
        self.assertIn("response.completed", content)

    def test_native_dsml_tool_responses(self):
        FakeDeepSeek.response = DSML_CALLS
        r = self.post("/v1/responses", {
            "model": "deepseek-web", "input": "Run pwd",
            "tools": [{"type": "custom", "name": "exec_command", "format": {"type": "text"}}],
        })
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["output"][0]["type"], "custom_tool_call")
        self.assertEqual(json.loads(r.json()["output"][0]["input"]), {"cmd": "pwd"})

    def test_anthropic_browser_preflight(self):
        r = requests.options(self.url + "/v1/messages", headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "anthropic-version,x-api-key,content-type",
        }, timeout=3)
        self.assertEqual(r.status_code, 204)
        self.assertEqual(r.headers["Access-Control-Allow-Origin"], "http://localhost:3000")
        allowed = r.headers["Access-Control-Allow-Headers"].lower()
        self.assertIn("x-api-key", allowed)
        self.assertIn("anthropic-version", allowed)

    def test_json_object_output(self):
        FakeDeepSeek.response = '{"ok":true}'
        body = {"messages": [{"role": "user", "content": "Respond in JSON"}],
                "response_format": {"type": "json_object"}}
        r = self.post("/v1/chat/completions", body)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(json.loads(r.json()["choices"][0]["message"]["content"]), {"ok": True})
        r = self.post("/v1/chat/completions", {**body, "stream": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn('{\\"ok\\":true}', r.text)

    def test_json_schema_responses_output(self):
        FakeDeepSeek.response = '{"count":3}'
        data = {"model": "deepseek-web", "input": "Give count",
                "text": {"format": {"type": "json_schema", "name": "count",
                                    "schema": {"type": "object", "properties": {
                                        "count": {"type": "integer"}}, "required": ["count"]}}}}
        r = self.post("/v1/responses", data)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(json.loads(r.json()["output_text"]), {"count": 3})
        self.assertIn('"count": {"type": "integer"}', FakeDeepSeek.instances[-1].calls[0][0])

    def test_json_schema_invalid_retried_then_rejected(self):
        FakeDeepSeek.response = '{"count":"bad"}'
        r = self.post("/v1/chat/completions", {"messages": [{"role": "user", "content": "Count"}],
                      "response_format": {"type": "json_schema", "json_schema": {
                          "name": "count", "schema": {"type": "object", "properties": {
                              "count": {"type": "integer"}}, "required": ["count"]}}}})
        self.assertEqual(r.status_code, 502)
        self.assertEqual(len(FakeDeepSeek.instances[-1].calls), 2)

    def test_chat_completion(self):
        r = self.post("/v1/chat/completions", {"model": "deepseek-web",
                      "messages": [{"role": "user", "content": "say OK"}]})
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data["choices"][0]["message"]["content"], "OK")
        self.assertEqual(data["choices"][0]["finish_reason"], "stop")
        self.assertEqual(data["object"], "chat.completion")
        self.assertGreater(data["usage"]["total_tokens"], 0)
        self.assertTrue(data["x_deepseek_usage_estimated"])

    def test_chat_stream_usage(self):
        with self.post("/v1/chat/completions", {"model": "deepseek-web",
                       "messages": [{"role": "user", "content": "say OK"}],
                       "stream": True, "stream_options": {"include_usage": True}},
                       stream=True) as response:
            events = [json.loads(line[6:]) for line in response.iter_lines(decode_unicode=True)
                      if line and line.startswith("data: {")]
        self.assertEqual(events[-1]["choices"], [])
        self.assertGreater(events[-1]["usage"]["total_tokens"], 0)
        self.assertTrue(events[-1]["x_deepseek_usage_estimated"])

    def test_chat_stream(self):
        with self.post("/v1/chat/completions", {"model": "deepseek-web",
                       "messages": [{"role": "user", "content": "say OK"}],
                       "stream": True}, stream=True) as r:
            self.assertEqual(r.status_code, 200)
            lines = [x.decode() for x in r.iter_lines() if x]
        self.assertEqual(lines[-1], "data: [DONE]")
        events = [json.loads(x[6:]) for x in lines if x.startswith("data: {")]
        self.assertEqual("".join(e["choices"][0]["delta"].get("content", "") for e in events), "OK")
        self.assertEqual(events[-1]["choices"][0]["finish_reason"], "stop")

    def test_tool_call_and_stream(self):
        FakeDeepSeek.response = CALLS
        body = {"model": "deepseek-web", "messages": [{"role": "user", "content": "2+3?"}],
                "tools": [CALCULATOR], "tool_choice": "required"}
        r = self.post("/v1/chat/completions", body)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["choices"][0]["finish_reason"], "tool_calls")
        self.assertEqual(r.json()["choices"][0]["message"]["tool_calls"][0]["function"]["name"], "calculator")
        r = self.post("/v1/chat/completions", {**body, "stream": True})
        self.assertIn('"tool_calls"', r.text)
        self.assertIn("data: [DONE]", r.text)

    def test_missing_required_tool_is_upstream_error(self):
        r = self.post("/v1/chat/completions", {"messages": [{"role": "user", "content": "2+3?"}],
                      "tools": [CALCULATOR], "tool_choice": "required"})
        self.assertEqual(r.status_code, 502)
        self.assertEqual(len(FakeDeepSeek.instances[-1].calls), 2)

    def test_provider_error_never_exposes_private_message(self):
        private_text = "PRIVATE_TOKEN_AND_USER_MESSAGE"
        with patch.object(FakeDeepSeek, "chat", side_effect=DeepSeekAPIError(
            "The upstream response contained " + private_text, status=503,
        )):
            response = self.post("/v1/chat/completions", {
                "model": "deepseek-web", "messages": [{"role": "user", "content": "test"}],
            })
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"]["type"], "upstream_error")
        self.assertNotIn(private_text, response.text)

    def test_multimodal_http(self):
        uri = "data:application/pdf;base64," + base64.b64encode(b"PDFDATA").decode()
        r = self.post("/v1/chat/completions", {"messages": [
            {"role": "user", "content": [{"type": "file", "file": {
                "filename": "test.pdf", "file_data": uri,
            }}]}
        ]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(FakeDeepSeek.instances[-1].upload_args[0],
                         [("test.pdf", b"PDFDATA")])
        self.assertEqual(FakeDeepSeek.instances[-1].calls[0][1]["ref_file_ids"], ["mock-0"])

    def test_responses_previous_context(self):
        r1 = self.post("/v1/responses", {"model": "deepseek-web", "input": "First"})
        self.assertEqual(r1.status_code, 200, r1.text)
        rid = r1.json()["id"]
        self.assertEqual(r1.json()["output_text"], "OK")
        self.assertGreater(r1.json()["usage"]["total_tokens"], 0)
        r2 = self.post("/v1/responses", {"model": "deepseek-web", "input": "Second",
                       "previous_response_id": rid})
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertIn('"content":"OK"', FakeDeepSeek.instances[-1].calls[0][0])
        self.assertEqual(requests.get(self.url + "/v1/responses/" + rid,
                                      headers=self.headers, timeout=3).status_code, 200)

    def test_responses_previous_context_replays_function_tool_output(self):
        """Codex can execute a tool and continue with previous_response_id."""
        FakeDeepSeek.response = CALLS
        request = {"model": "deepseek-web", "input": "2+3?", "tool_choice": "required",
                   "tools": [{"type": "function", "name": "calculator",
                              "parameters": CALCULATOR["function"]["parameters"]}]}
        first = self.post("/v1/responses", request)
        self.assertEqual(first.status_code, 200, first.text)
        call = first.json()["output"][0]
        self.assertEqual(call["type"], "function_call")
        FakeDeepSeek.response = "5"
        second = self.post("/v1/responses", {"model": "deepseek-web",
                       "previous_response_id": first.json()["id"],
                       "input": [{"type": "function_call_output", "call_id": call["call_id"],
                                  "output": "5"}]})
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["output_text"], "5")
        prompt = FakeDeepSeek.instances[-1].calls[0][0]
        self.assertIn(call["call_id"], prompt)
        self.assertIn('"role":"tool","content":"5"', prompt)
        self.assertIn('"name":"calculator"', prompt)

    def test_responses_accepts_tool_policy_denial_as_feedback(self):
        """A tool permission denial must be forwarded, not retried or hidden."""
        FakeDeepSeek.response = CALLS
        first = self.post("/v1/responses", {"model": "deepseek-web", "input": "2+3?",
                         "tools": [CALCULATOR], "tool_choice": "required"})
        self.assertEqual(first.status_code, 200)
        call_id = first.json()["output"][0]["call_id"]
        FakeDeepSeek.response = "Tool could not execute"
        second = self.post("/v1/responses", {"model": "deepseek-web",
                          "previous_response_id": first.json()["id"],
                          "input": [{"type": "function_call_output", "call_id": call_id,
                                     "output": "exec_command failed: blocked by policy"}]})
        self.assertEqual(second.status_code, 200)
        prompt = FakeDeepSeek.instances[-1].calls[0][0]
        self.assertIn('"role":"tool"', prompt)
        self.assertIn("blocked by policy", prompt)

    def test_responses_previous_context_replays_custom_tool_output(self):
        FakeDeepSeek.response = ('<deepseek_tool_calls>{"calls":[{"name":"exec_command",'
                                 '"arguments":{"input":"pwd"}}]}</deepseek_tool_calls>')
        tool = {"type": "custom", "name": "exec_command", "format": {"type": "text"}}
        first = self.post("/v1/responses", {"model": "deepseek-web", "input": "Run pwd",
                                            "tools": [tool], "tool_choice": "required"})
        self.assertEqual(first.status_code, 200, first.text)
        call = first.json()["output"][0]
        self.assertEqual(call["type"], "custom_tool_call")
        FakeDeepSeek.response = "Directory confirmed"
        with self.post("/v1/responses", {"model": "deepseek-web", "stream": True,
                     "previous_response_id": first.json()["id"],
                     "input": [{"type": "custom_tool_call_output", "call_id": call["call_id"],
                                "output": "C:/repo"}]}, stream=True) as result:
            self.assertEqual(result.status_code, 200, result.text)
            events = [json.loads(line[6:]) for line in result.text.splitlines()
                      if line.startswith("data: {")]
        completed = [event for event in events if event.get("type") == "response.completed"]
        self.assertEqual(completed[-1]["response"]["output_text"], "Directory confirmed")
        prompt = FakeDeepSeek.instances[-1].calls[0][0]
        self.assertIn(call["call_id"], prompt)
        self.assertIn("C:/repo", prompt)
        self.assertIn('"name":"exec_command"', prompt)

    def test_responses_http_tool_output_parts_preserve_text(self):
        result = self.post("/v1/responses", {"model": "deepseek-web", "input": [
            {"type": "custom_tool_call_output", "call_id": "call_valid", "output": [
                {"type": "input_text", "text": "exit=0"},
                {"type": "input_text", "text": "done"},
            ]},
        ]})
        self.assertEqual(result.status_code, 200, result.text)
        prompt = FakeDeepSeek.instances[-1].calls[0][0]
        self.assertIn('"content":"exit=0\\ndone"', prompt)
        self.assertNotIn("'type': 'input_text'", prompt)

    def test_responses_http_missing_tool_call_id_is_rejected(self):
        for kind in ("function_call_output", "custom_tool_call_output"):
            with self.subTest(kind=kind):
                FakeDeepSeek.instances.clear()
                result = self.post("/v1/responses", {"model": "deepseek-web", "input": [
                    {"type": kind, "output": "done"},
                ]})
                self.assertEqual(result.status_code, 400, result.text)
                self.assertFalse(FakeDeepSeek.instances)

    def test_responses_stream(self):
        with self.post("/v1/responses", {"model": "deepseek-web", "input": "Test",
                       "stream": True}, stream=True) as r:
            self.assertEqual(r.status_code, 200)
            data = r.text
        self.assertIn("event: response.output_text.delta", data)
        self.assertIn("event: response.completed", data)

    def test_responses_stream_cooldown_emits_rate_limit_failure(self):
        from request_guard import RateLimitPause

        with patch.object(self.server, "client_factory", side_effect=RateLimitPause(42.2)):
            with self.post("/v1/responses", {"model": "deepseek-web", "input": "Test",
                           "stream": True}, stream=True) as result:
                self.assertEqual(result.status_code, 200)
                events = [json.loads(line[6:]) for line in result.text.splitlines()
                          if line.startswith("data: {")]
        failed = [event for event in events if event.get("type") == "response.failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["error"]["type"], "rate_limit_error")
        self.assertEqual(failed[0]["error"]["retry_after"], 43)

    def test_responses_stream_initial_remote_429_keeps_rate_limit_type(self):
        from api_client import DeepSeekAPIError

        upstream = DeepSeekAPIError("private upstream response", status=429)
        with patch.object(self.server, "client_factory", side_effect=upstream):
            with self.post("/v1/responses", {"model": "deepseek-web", "input": "Test",
                           "stream": True}, stream=True) as result:
                events = [json.loads(line[6:]) for line in result.text.splitlines()
                          if line.startswith("data: {")]
        failure = next(event["error"] for event in events if event.get("type") == "response.failed")
        self.assertEqual(failure["type"], "rate_limit_error")
        self.assertNotIn("private upstream response", str(failure))

    def test_chat_stream_cooldown_emits_rate_limit_error(self):
        from request_guard import RateLimitPause

        with patch.object(self.server, "client_factory", side_effect=RateLimitPause(60)):
            with self.post("/v1/chat/completions", {"model": "deepseek-web",
                           "messages": [{"role": "user", "content": "Test"}],
                           "stream": True}, stream=True) as result:
                self.assertEqual(result.status_code, 200)
                events = [json.loads(line[6:]) for line in result.text.splitlines()
                          if line.startswith("data: {")]
        errors = [event["error"] for event in events if "error" in event]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["type"], "rate_limit_error")
        self.assertEqual(errors[0]["retry_after"], 60)

    def test_responses_stream_item_ids_match_completed_response(self):
        for response_text, request in (
            ("OK", {"model": "deepseek-web", "input": "Test", "stream": True}),
            (CALLS, {"model": "deepseek-web", "input": "2+3?", "stream": True,
                     "tools": [{"type": "function", "name": "calculator",
                                "parameters": CALCULATOR["function"]["parameters"]}],
                     "tool_choice": "required"}),
        ):
            with self.subTest(tool_mode=response_text == CALLS):
                FakeDeepSeek.response = response_text
                with self.post("/v1/responses", request, stream=True) as r:
                    self.assertEqual(r.status_code, 200, r.text)
                    events = [json.loads(line[6:]) for line in r.text.splitlines()
                              if line.startswith("data: {")]
                self.assertFalse(any(e.get("type") == "response.failed" for e in events))
                added = [e["item"]["id"] for e in events
                         if e.get("type") == "response.output_item.added"]
                done = [e["item"]["id"] for e in events
                        if e.get("type") == "response.output_item.done"]
                completed = next(e for e in events if e.get("type") == "response.completed")
                final = [item["id"] for item in completed["response"]["output"]]
                self.assertTrue(added)
                self.assertEqual(added, done)
                self.assertEqual(done, final)

    def test_legacy_completions(self):
        r = self.post("/v1/completions", {"model": "deepseek-web", "prompt": "Hi"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["choices"][0]["text"], "OK")

    def test_invalid_requests(self):
        r = self.post("/v1/chat/completions", {"messages": []})
        self.assertEqual(r.status_code, 400)
        self.assertIn("error", r.json())
        r = self.post("/v1/chat/completions", {"model": "nonexistent",
                      "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(r.status_code, 400)

    def test_official_openai_python_sdk(self):
        # The official SDK is optional for running the gateway itself.
        try:
            from openai import OpenAI
        except ImportError:
            self.skipTest("optional openai SDK not installed")
        sdk = OpenAI(api_key="test-secret", base_url=self.url + "/v1", timeout=5)
        try:
            response = sdk.chat.completions.create(
                model="deepseek-web",
                messages=[{"role": "user", "content": "test"}],
            )
            self.assertEqual(response.choices[0].message.content, "OK")
            chunks = list(sdk.chat.completions.create(
                model="deepseek-web",
                messages=[{"role": "user", "content": "test"}],
                stream=True,
            ))
            self.assertEqual("".join(c.choices[0].delta.content or "" for c in chunks), "OK")
            new_response = sdk.responses.create(model="deepseek-web", input="test")
            self.assertEqual(new_response.output_text, "OK")
            events = list(sdk.responses.create(model="deepseek-web", input="test", stream=True))
            self.assertTrue(any(e.type == "response.output_text.delta" for e in events))
            self.assertTrue(any(e.type == "response.completed" for e in events))
            FakeDeepSeek.response = CALLS
            response = sdk.responses.create(
                model="deepseek-web", input="2+3?",
                tools=[{"type": "function", "name": "calculator",
                        "parameters": CALCULATOR["function"]["parameters"]}],
                tool_choice="required",
            )
            self.assertEqual(response.output[0].type, "function_call")
            self.assertEqual(response.output[0].name, "calculator")
            events = list(sdk.responses.create(
                model="deepseek-web", input="2+3?",
                tools=[{"type": "function", "name": "calculator",
                        "parameters": CALCULATOR["function"]["parameters"]}],
                tool_choice="required", stream=True,
            ))
            self.assertTrue(any(e.type == "response.function_call_arguments.delta" for e in events))
        finally:
            sdk.close()

    def test_official_anthropic_python_sdk(self):
        try:
            from anthropic import Anthropic
        except ImportError:
            self.skipTest("optional anthropic SDK not installed")
        sdk = Anthropic(api_key="test-secret", base_url=self.url, timeout=5)
        try:
            message = sdk.messages.create(
                model="deepseek-web", max_tokens=64,
                messages=[{"role": "user", "content": "test"}],
            )
            self.assertEqual(message.content[0].text, "OK")
            self.assertGreater(message.usage.input_tokens, 0)
            self.assertGreater(message.usage.output_tokens, 0)
            events = list(sdk.messages.create(
                model="deepseek-web", max_tokens=64,
                messages=[{"role": "user", "content": "test"}],
                stream=True,
            ))
            self.assertTrue(any(e.type == "content_block_delta" for e in events))
            FakeDeepSeek.response = CALLS
            tools = [{"name": "calculator", "description": "sum",
                      "input_schema": CALCULATOR["function"]["parameters"]}]
            tool_response = sdk.messages.create(
                model="deepseek-web", max_tokens=64, tools=tools,
                tool_choice={"type": "any"},
                messages=[{"role": "user", "content": "2+3?"}],
            )
            self.assertEqual(tool_response.stop_reason, "tool_use")
            self.assertEqual(tool_response.content[0].name, "calculator")
            stream = list(sdk.messages.create(
                model="deepseek-web", max_tokens=64, tools=tools,
                tool_choice={"type": "any"},
                messages=[{"role": "user", "content": "2+3?"}],
                stream=True,
            ))
            self.assertTrue(any(e.type == "content_block_delta" for e in stream))
            count = sdk.messages.count_tokens(
                model="deepseek-web", messages=[{"role": "user", "content": "abc"}],
            )
            self.assertGreater(count.input_tokens, 0)
        finally:
            sdk.close()


if __name__ == "__main__":
    unittest.main()
