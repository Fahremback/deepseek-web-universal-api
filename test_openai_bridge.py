"""Offline OpenAI-protocol gateway integration tests (no DeepSeek request)."""
from __future__ import annotations

import base64
import json
import threading
import unittest

import requests

from api_client import ChatResult
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

    def test_responses_input(self):
        req = normalize_responses_input({
            "input": [{"type": "function_call_output", "call_id": "call_1", "output": "5"}],
            "model": "deepseek-web",
        }, previous=[{"role": "assistant", "content": "Earlier"}])
        self.assertEqual(req["messages"][0]["role"], "assistant")
        self.assertEqual(req["messages"][1]["tool_call_id"], "call_1")

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

    def test_responses_stream(self):
        with self.post("/v1/responses", {"model": "deepseek-web", "input": "Test",
                       "stream": True}, stream=True) as r:
            self.assertEqual(r.status_code, 200)
            data = r.text
        self.assertIn("event: response.output_text.delta", data)
        self.assertIn("event: response.completed", data)

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
