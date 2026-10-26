"""End-to-end localhost fallback routing tests, without external provider calls."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import gzip
import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import requests

from openai_server import Gateway, GatewayHandler
from test_openai_bridge import FakeDeepSeek


class UpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.received.append({
            "path": self.path,
            "data": data,
            "content_type": self.headers.get("Content-Type"),
            "content_encoding": self.headers.get("Content-Encoding"),
            "authorization": self.headers.get("Authorization"),
            "x_api_key": self.headers.get("X-API-Key"),
            "cookie": self.headers.get("Cookie"),
            "proxy_authorization": self.headers.get("Proxy-Authorization"),
        })
        if getattr(self.server, "required_key", None) and self.headers.get("Authorization") != (
            "Bearer " + self.server.required_key
        ):
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if data.get("stream"):
            body = (b'event: response.created\ndata: {"type":"response.created"}\n\n'
                    b'event: response.completed\ndata: {"type":"response.completed"}\n\n')
            status, mime = 200, "text/event-stream"
        elif data.get("compressed_response"):
            status, mime, body = 200, "application/json", gzip.compress(b'{"ok":true}')
        elif data.get("force_error"):
            status, mime, body = 429, "application/json", b'{"error":{"message":"limited"}}'
        else:
            status, mime = 200, "application/json"
            body = json.dumps({"model": data["model"], "path": self.path}).encode()
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        if data.get("upstream_cors_origin"):
            self.send_header("Access-Control-Allow-Origin", data["upstream_cors_origin"])
            self.send_header("Vary", "Accept-Encoding")
        if data.get("compressed_response"):
            self.send_header("Content-Encoding", "gzip")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()


class TestFallbackRouting(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        cls.original.received = []
        cls.original_thread = threading.Thread(target=cls.original.serve_forever, daemon=True)
        cls.original_thread.start()
        cls.gateway = Gateway(
            ("127.0.0.1", 0), api_key="local-secret",
            client_factory=FakeDeepSeek,
            fallback_url=f"http://127.0.0.1:{cls.original.server_port}",
        )
        cls.gateway_thread = threading.Thread(target=cls.gateway.serve_forever, daemon=True)
        cls.gateway_thread.start()
        cls.url = f"http://127.0.0.1:{cls.gateway.server_port}/v1/responses"

    @classmethod
    def tearDownClass(cls):
        for server, thread in [(cls.gateway, cls.gateway_thread),
                               (cls.original, cls.original_thread)]:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def setUp(self):
        self.original.received.clear()
        self.original.required_key = None
        FakeDeepSeek.instances.clear()
        FakeDeepSeek.response = "OK"

    def test_unknown_model_goes_to_original_gateway(self):
        result = requests.post(self.url, json={"model": "gpt-original", "input": "Hello"},
                               headers={"Authorization": "Bearer local-secret"}, timeout=3)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["model"], "gpt-original")
        self.assertEqual(self.original.received[-1]["path"], "/v1/responses")
        self.assertEqual(self.original.received[-1]["content_type"], "application/json")
        self.assertIsNone(self.original.received[-1]["content_encoding"])
        self.assertFalse(FakeDeepSeek.instances)

    def test_fallback_uses_separate_upstream_credentials(self):
        self.original.required_key = "upstream-key"
        self.gateway.fallback_api_key = "upstream-key"
        try:
            result = requests.post(self.url, json={"model": "gpt-original", "input": "Hello"},
                                   headers={"Authorization": "Bearer local-secret"}, timeout=3)
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(self.original.received[-1]["authorization"], "Bearer upstream-key")
            self.assertIsNone(self.original.received[-1]["x_api_key"])
        finally:
            self.gateway.fallback_api_key = None

    def test_fallback_filters_local_x_api_key_with_separate_credentials(self):
        self.original.required_key = "upstream-key"
        self.gateway.fallback_api_key = "upstream-key"
        try:
            result = requests.post(self.url, json={"model": "gpt-original", "input": "Hello"},
                                   headers={"X-API-Key": "local-secret"}, timeout=3)
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(self.original.received[-1]["authorization"], "Bearer upstream-key")
            self.assertIsNone(self.original.received[-1]["x_api_key"])
        finally:
            self.gateway.fallback_api_key = None

    def test_fallback_never_forwards_browser_cookies_or_proxy_authorization(self):
        response = requests.post(self.url, json={"model": "gpt-original", "input": "Hello"},
                                 headers={"Authorization": "Bearer local-secret",
                                          "Cookie": "session=PRIVATE_BROWSER_COOKIE",
                                          "Proxy-Authorization": "Basic PRIVATE_PROXY_CREDENTIAL"}, timeout=3)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.original.received[-1]["cookie"])
        self.assertIsNone(self.original.received[-1]["proxy_authorization"])
        self.assertEqual(self.original.received[-1]["authorization"], "Bearer local-secret")

    def test_deepseek_model_stays_local(self):
        result = requests.post(self.url, json={"model": "deepseek-web", "input": "Hello"},
                               headers={"Authorization": "Bearer local-secret"}, timeout=3)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["output_text"], "OK")
        self.assertFalse(self.original.received)
        self.assertTrue(FakeDeepSeek.instances)

    def test_fallback_stream_and_status_are_preserved(self):
        headers = {"Authorization": "Bearer local-secret"}
        with requests.post(self.url, json={"model": "gpt-original", "input": "Hello", "stream": True},
                           headers=headers, stream=True, timeout=3) as result:
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.headers["Content-Type"], "text/event-stream")
            self.assertIn(b"response.completed", result.content)
        error = requests.post(self.url, json={"model": "gpt-original", "force_error": True},
                              headers=headers, timeout=3)
        self.assertEqual(error.status_code, 429)
        self.assertEqual(error.json()["error"]["message"], "limited")

    def test_fallback_preserves_local_browser_cors_on_success_error_and_stream(self):
        origin = "http://localhost:3000"
        headers = {"Authorization": "Bearer local-secret", "Origin": origin}
        for extra in ({}, {"force_error": True}, {"stream": True}):
            with self.subTest(extra=extra):
                with requests.post(self.url,
                                   json={"model": "gpt-original", "input": "Hello", **extra},
                                   headers=headers, timeout=3, stream=True) as response:
                    self.assertEqual(response.status_code, 429 if extra.get("force_error") else 200)
                    self.assertEqual(response.headers.get("Access-Control-Allow-Origin"), origin)
                    self.assertIn("Origin", response.headers.get("Vary", ""))
                    _ = response.content

    def test_fallback_cannot_override_local_cors_policy(self):
        origin = "http://127.0.0.1:4317"
        response = requests.post(self.url, json={
            "model": "gpt-original", "input": "Hello",
            "upstream_cors_origin": "https://other.example",
        }, headers={"Authorization": "Bearer local-secret", "Origin": origin}, timeout=3)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("Access-Control-Allow-Origin"), origin)
        vary = {value.strip().lower() for value in response.headers.get("Vary", "").split(",")}
        self.assertEqual(vary, {"accept-encoding", "origin"})

    def test_fallback_compressed_response_preserves_encoding(self):
        response = requests.post(self.url, json={"model": "gpt-original", "compressed_response": True},
                                 headers={"Authorization": "Bearer local-secret"}, timeout=3)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Content-Encoding"], "gzip")
        self.assertEqual(response.json(), {"ok": True})

    def test_logging_discards_untrusted_request_content(self):
        stub = SimpleNamespace(client_address=("127.0.0.1", 10))
        with patch("builtins.print") as printer:
            GatewayHandler.log_message(stub, 'code %d, message %s', 400, 'secret=do-not-log')
            GatewayHandler.log_message(stub, '"%s" %s %s', 'POST /secret/path HTTP/1.1', 401, '-')
        lines = " ".join(str(call) for call in printer.call_args_list)
        self.assertNotIn("secret", lines)
        self.assertIn("HTTP 401", lines)

    def test_invalid_local_key_never_reaches_fallback(self):
        result = requests.post(self.url, json={"model": "gpt-original"}, timeout=3)
        self.assertEqual(result.status_code, 401)
        self.assertEqual(result.headers.get("Connection"), "close")
        self.assertFalse(self.original.received)


if __name__ == "__main__":
    unittest.main()
