"""OpenAI-compatible localhost HTTP gateway for DeepSeek Web.

Usage: python openai_server.py --host 127.0.0.1 --port 8000
Requires an existing DeepSeek Web credential (see README.md).
"""
from __future__ import annotations

import argparse
from collections import OrderedDict
from dataclasses import dataclass
import gzip
import io
import json
import math
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import uuid

from request_guard import RateLimitPause, RateLimitStateError

from api_client import AuthenticationError, DeepSeekAPIError, DeepSeekClient, ProofOfWorkError
from anthropic_adapter import normalize_anthropic_request
from harness_adapter import (
    MODEL_IDS, PreparedRequest, RequestError, ToolProtocolError,
    normalize_responses_input, parse_tool_calls, prepare_chat, valid_json_output,
)


MAX_REQUEST_BYTES = 48 * 1024 * 1024


def _validate_fallback_url(url: str | None) -> str | None:
    """Only allow a local API origin, never a credential-bearing remote URL."""
    if url is None:
        return None
    if not isinstance(url, str):
        raise ValueError("Fallback URL must be a localhost HTTP origin")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise ValueError("Invalid fallback URL") from exc
    if (parts.scheme.lower() not in {"http", "https"}
            or parts.hostname not in {"localhost", "127.0.0.1", "::1"}
            or port is None or not 1 <= port <= 65535
            or parts.username is not None or parts.password is not None
            or parts.path not in {"", "/"} or parts.query or parts.fragment):
        raise ValueError("Fallback URL must be a localhost HTTP origin with a port")
    return url.rstrip("/")


class _NoFallbackRedirect(HTTPRedirectHandler):
    """Never forward fallback credentials to a redirected origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass
class Completion:
    text: str
    calls: list[dict[str, Any]] | None


def _completion_id(prefix: str = "chatcmpl") -> str:
    return prefix + "-" + uuid.uuid4().hex


def _finish_reason(calls: list[dict[str, Any]] | None) -> str:
    return "tool_calls" if calls else "stop"


def _estimate_tokens(text: str) -> int:
    """Rough characters/4 estimate; never reported as exact provider usage."""
    return max(1, (len(text) + 3) // 4)


def _usage(prompt: str, result: Completion) -> dict[str, Any]:
    output = result.text
    if result.calls:
        output = json.dumps(result.calls, ensure_ascii=False)
    p, c = _estimate_tokens(prompt), _estimate_tokens(output)
    return {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}


def _response_usage(prompt: str, result: Completion) -> dict[str, Any]:
    u = _usage(prompt, result)
    return {"input_tokens": u["prompt_tokens"], "output_tokens": u["completion_tokens"],
            "total_tokens": u["total_tokens"], "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0}}


def _chat_json(result: Completion, model: str, ident: str, timestamp: int,
               prompt: str) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": None if result.calls else result.text}
    if result.calls:
        message["tool_calls"] = result.calls
    return {
        "id": ident, "object": "chat.completion", "created": timestamp,
        "model": model, "choices": [{
            "index": 0, "message": message, "finish_reason": _finish_reason(result.calls),
            "logprobs": None,
        }],
        "usage": _usage(prompt, result),
        "x_deepseek_usage_estimated": True,
    }


def _chat_chunk(model: str, ident: str, timestamp: int, delta: dict[str, Any],
                reason: str | None = None) -> dict[str, Any]:
    return {
        "id": ident, "object": "chat.completion.chunk", "created": timestamp,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": reason, "logprobs": None}],
    }


def _response_object(result: Completion, model: str, ident: str, timestamp: int,
                     previous_response_id: str | None = None, prompt: str = "") -> dict[str, Any]:
    if result.calls:
        output = [_response_call_item(call) for call in result.calls]
    else:
        output = [{
            "id": "msg_" + uuid.uuid4().hex, "type": "message", "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": result.text, "annotations": []}],
        }]
    return {
        "id": ident, "object": "response", "created_at": timestamp,
        "model": model, "status": "completed", "output": output,
        "output_text": "" if result.calls else result.text,
        "previous_response_id": previous_response_id,
        "error": None, "incomplete_details": None,
        "usage": _response_usage(prompt, result) if prompt else None,
        "x_deepseek_usage_estimated": True,
    }


def _response_call_item(call: dict[str, Any], pending: bool = False) -> dict[str, Any]:
    common = {"call_id": call["id"], "name": call["function"]["name"],
              "status": "in_progress" if pending else "completed"}
    if call["type"] == "custom":
        return {"type": "custom_tool_call", "id": "ctc_" + uuid.uuid4().hex,
                **common, "input": "" if pending else call["input"]}
    return {"type": "function_call", "id": "fc_" + uuid.uuid4().hex,
            **common, "arguments": "" if pending else call["function"]["arguments"]}


def _assistant_history(result: Completion) -> dict[str, Any]:
    if result.calls:
        return {"role": "assistant", "content": None, "tool_calls": result.calls}
    return {"role": "assistant", "content": result.text}


def _anthropic_message(result: Completion, model: str, ident: str, prompt: str) -> dict[str, Any]:
    if result.calls:
        content = [{
            "type": "tool_use", "id": c["id"],
            "name": c["function"]["name"],
            "input": json.loads(c["function"]["arguments"]),
        } for c in result.calls]
    else:
        content = [{"type": "text", "text": result.text}]
    return {
        "id": ident, "type": "message", "role": "assistant",
        "model": model, "content": content,
        "stop_reason": "tool_use" if result.calls else "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": _estimate_tokens(prompt),
                  "output_tokens": _estimate_tokens(result.text or json.dumps(result.calls))},
    }


class Gateway(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], api_key: str | None = None,
                 max_parallel: int = 1, search_enabled: bool = False,
                 client_factory: Callable[[], DeepSeekClient] = DeepSeekClient,
                 fallback_url: str | None = None,
                 fallback_api_key: str | None = None):
        safe_fallback_url = _validate_fallback_url(fallback_url)
        super().__init__(address, GatewayHandler)
        self.api_key = api_key or None
        self.search_enabled = search_enabled
        self.client_factory = client_factory
        self.fallback_url = safe_fallback_url
        self.fallback_api_key = fallback_api_key
        self.slots = threading.BoundedSemaphore(max_parallel)
        self.history_lock = threading.Lock()
        self.response_history: OrderedDict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = OrderedDict()

    def remember(self, response: dict[str, Any], messages: list[dict[str, Any]]) -> None:
        with self.history_lock:
            self.response_history[response["id"]] = (response, messages)
            self.response_history.move_to_end(response["id"])
            while len(self.response_history) > 256:
                self.response_history.popitem(last=False)

    def recall(self, ident: str) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        with self.history_lock:
            item = self.response_history.get(ident)
            if item is not None:
                self.response_history.move_to_end(ident)
            return item


class GatewayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: Gateway

    def log_message(self, format: str, *args: Any) -> None:
        # BaseHTTPRequestHandler can log a rejected request line verbatim, which
        # may contain an unconsumed JSON body or bearer token. Log status only.
        status = str(args[1]) if format == '"%s" %s %s' and len(args) >= 2 else "error"
        print(f"[bridge] {self.client_address[0]} HTTP {status}", flush=True)

    def _origin_headers(self) -> dict[str, str]:
        origin = self.headers.get("Origin", "")
        if not origin:
            return {}
        if not re.fullmatch(r"https?://(?:localhost|127\.0\.0\.1|\[::1\])(?::\d+)?", origin, re.I):
            raise RequestError("Browser origin is not allowed")
        return {"Access-Control-Allow-Origin": origin, "Vary": "Origin"}

    def _authorized(self) -> bool:
        if not self.server.api_key:
            return True
        supplied = self.headers.get("Authorization", "")
        alternative = self.headers.get("x-api-key", "")
        import hmac
        return (
            hmac.compare_digest(supplied, "Bearer " + self.server.api_key)
            or hmac.compare_digest(alternative, self.server.api_key)
        )

    def _json(self, status: int, content: dict[str, Any], extra: dict[str, str] | None = None) -> None:
        body = json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str, code: str = "invalid_request_error",
               *, extra_headers: dict[str, str] | None = None) -> None:
        try:
            cors = self._origin_headers()
        except RequestError:
            cors = {}
        # The body may not have been consumed (e.g. rejected credentials).
        # Never let a subsequent keep-alive parse treat it as another request.
        self.close_connection = True
        cors["Connection"] = "close"
        if extra_headers:
            cors.update(extra_headers)
        if self.path.startswith("/v1/messages"):
            self._json(status, {"type": "error", "error": {"message": message, "type": code}}, cors)
            return
        self._json(status, {"error": {"message": message, "type": code, "code": code}}, cors)

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise RequestError("Invalid Content-Length") from exc
        if length <= 0 or length > MAX_REQUEST_BYTES:
            raise RequestError("Invalid or excessive request size")
        try:
            body = self.rfile.read(length)
            encoding = self.headers.get("Content-Encoding", "identity").lower().strip()
            if encoding == "zstd":
                import zstandard
                body = zstandard.ZstdDecompressor().decompress(body, max_output_size=MAX_REQUEST_BYTES)
            elif encoding == "gzip":
                # A small compressed payload can expand far beyond the request limit.
                # Limit decompression itself, before allocating the complete output.
                with gzip.GzipFile(fileobj=io.BytesIO(body)) as archive:
                    body = archive.read(MAX_REQUEST_BYTES + 1)
            elif encoding not in {"identity", ""}:
                raise RequestError("Unsupported Content-Encoding")
            if len(body) > MAX_REQUEST_BYTES:
                raise RequestError("Decompressed request is too large")
            obj = json.loads(body)
        except RequestError:
            raise
        except (UnicodeError, ValueError, ImportError) as exc:
            raise RequestError("Invalid JSON body") from exc
        except Exception as exc:
            if isinstance(exc, RequestError):
                raise
            raise RequestError("Invalid compressed JSON body") from exc
        if not isinstance(obj, dict):
            raise RequestError("JSON body must be an object")
        return obj

    def _proxy_to_fallback(self, data: dict[str, Any], cors: dict[str, str]) -> None:
        """Forward non-DeepSeek models to the original localhost Codex gateway."""
        hop_by_hop = {"host", "content-length", "connection", "transfer-encoding",
                      "upgrade", "keep-alive", "proxy-connection"}
        # Browser cookies and proxy credentials belong to the inbound local
        # connection, never the separately configured upstream gateway.
        request_excluded = hop_by_hop | {"content-encoding", "accept-encoding",
                                         "cookie", "proxy-authorization"}
        excluded = request_excluded | ({"authorization", "x-api-key"}
                                       if self.server.fallback_api_key is not None else set())
        headers = {key: value for key, value in self.headers.items()
                   if key.lower() not in excluded}
        if self.server.fallback_api_key is not None:
            headers["Authorization"] = "Bearer " + self.server.fallback_api_key
        headers["Content-Type"] = "application/json"
        headers["Accept-Encoding"] = "identity"
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        target = self.server.fallback_url + self.path
        req = Request(target, data=body, headers=headers, method="POST")
        try:
            # Disable proxy environment settings and redirects for the local
            # fallback: neither may relocate this authenticated HTTP request.
            upstream = build_opener(ProxyHandler({}), _NoFallbackRedirect()).open(req, timeout=300)
        except HTTPError as exc:
            upstream = exc
        except URLError:
            self._error(502, "Original Codex gateway is unavailable", "upstream_error")
            return
        with upstream:
            self.send_response(upstream.status)
            for key, value in upstream.headers.items():
                # Keep upstream Content-Encoding paired with its original bytes.
                # This gateway owns the browser-origin policy, not the fallback.
                if key.lower() not in hop_by_hop | {"server", "date", "vary"} and not key.lower().startswith("access-control-"):
                    self.send_header(key, value)
            vary = upstream.headers.get("Vary", "")
            if cors and "origin" not in {part.strip().lower() for part in vary.split(",")}:
                vary = f"{vary}, Origin" if vary else "Origin"
            if vary:
                self.send_header("Vary", vary)
            if cors:
                self.send_header("Access-Control-Allow-Origin", cors["Access-Control-Allow-Origin"])
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            while chunk := upstream.read1(8192):
                self.wfile.write(chunk)
                self.wfile.flush()

    def _start_sse(self, origin_headers: dict[str, str]) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        for k, v in origin_headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.close_connection = True

    def _event(self, obj: dict[str, Any], event: str | None = None) -> None:
        if event and event.startswith("response."):
            obj = dict(obj)
            obj.setdefault("sequence_number", getattr(self, "_response_sequence", 0))
            self._response_sequence = obj["sequence_number"] + 1
        prefix = f"event: {event}\n" if event else ""
        text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        self.wfile.write((prefix + "data: " + text + "\n\n").encode("utf-8"))
        self.wfile.flush()

    def _done(self) -> None:
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def do_OPTIONS(self) -> None:
        try:
            headers = self._origin_headers()
        except RequestError as exc:
            self._error(403, str(exc))
            return
        self.send_response(204)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", (
            "Authorization, Content-Type, X-API-Key, Anthropic-Version, "
            "Anthropic-Beta, Anthropic-Dangerous-Direct-Browser-Access, "
            "OpenAI-Organization, OpenAI-Project"
        ))
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        try:
            cors = self._origin_headers()
            if self.server.fallback_url and self.headers.get("Upgrade", "").lower() == "websocket":
                self.send_response(426)
                self.send_header("Connection", "close")
                self.send_header("Content-Length", "0")
                self.end_headers()
                self.close_connection = True
                return
            if self.path.rstrip("/") in {"/health", "/healthz"}:
                self._json(200, {"status": "ok", "service": "deepseek-web-bridge"}, cors)
                return
            if not self._authorized():
                self._error(401, "Invalid API key", "authentication_error")
                return
            if self.path.rstrip("/") in {"/v1/models", "/models"}:
                now = int(time.time())
                self._json(200, {"object": "list", "data": [
                    {"id": name, "object": "model", "created": now, "owned_by": "deepseek-web"}
                    for name in MODEL_IDS
                ]}, cors)
                return
            m = re.fullmatch(r"/v1/models/([\w.-]+)", self.path)
            if m:
                name = m.group(1)
                if name not in MODEL_IDS:
                    self._error(404, "Unknown model", "not_found_error")
                else:
                    self._json(200, {"id": name, "object": "model",
                                     "created": int(time.time()), "owned_by": "deepseek-web"}, cors)
                return
            m = re.fullmatch(r"/v1/responses/(resp_[a-f0-9]+)", self.path)
            if m:
                item = self.server.recall(m.group(1))
                if item is None:
                    self._error(404, "Unknown or expired response", "not_found_error")
                else:
                    self._json(200, item[0], cors)
                return
            self._error(404, "Unknown route", "not_found_error")
        except RequestError as exc:
            self._error(403, str(exc))

    def do_POST(self) -> None:
        try:
            cors = self._origin_headers()
            if not self._authorized():
                self._error(401, "Invalid API key", "authentication_error")
                return
            data = self._read_json()
            if self.server.fallback_url and data.get("model") not in MODEL_IDS:
                self._proxy_to_fallback(data, cors)
                return
            # Allow the previous handler a brief interval to finish writing
            # and release its slot. Responses can reach a local client just
            # before that release, creating spurious 429s on sequential calls.
            # This still permits only max_parallel *active* requests.
            if not self.server.slots.acquire(timeout=0.35):
                self._error(429, "Too many concurrent requests", "rate_limit_error",
                            extra_headers={"Retry-After": "1"})
                return
            try:
                if self.path.rstrip("/") in {"/v1/chat/completions", "/chat/completions"}:
                    self._handle_chat(data, cors)
                elif self.path.rstrip("/") in {"/v1/completions", "/completions"}:
                    self._handle_legacy(data, cors)
                elif self.path.rstrip("/") == "/v1/responses":
                    self._handle_responses(data, cors)
                elif self.path.rstrip("/") == "/v1/messages":
                    self._handle_anthropic(data, cors)
                elif self.path.rstrip("/") == "/v1/messages/count_tokens":
                    converted = normalize_anthropic_request(data)
                    prepared = prepare_chat(converted)
                    # The private web protocol has no token counter.
                    self._json(200, {"input_tokens": max(1, len(prepared.prompt) // 4)}, cors)
                else:
                    self._error(404, "Unknown route", "not_found_error")
            finally:
                self.server.slots.release()
        except RequestError as exc:
            self._error(400, str(exc))
        except RateLimitPause as exc:
            self._error(429, "DeepSeek provider cooldown is active", "rate_limit_error",
                        extra_headers={"Retry-After": str(math.ceil(exc.retry_after))})
        except RateLimitStateError:
            self._error(503, "DeepSeek request-limit state is unavailable", "rate_limit_state_error",
                        extra_headers={"Retry-After": "60"})
        except AuthenticationError:
            self._error(503, "DeepSeek Web authentication is unavailable", "provider_authentication_error")
        except DeepSeekAPIError as exc:
            if exc.status == 429:
                self._error(429, "DeepSeek provider rate limit reached", "rate_limit_error")
            else:
                # Upstream error payloads can contain tokens or private prompts.
                self._error(502, "DeepSeek Web upstream request failed", "upstream_error")
        except (ProofOfWorkError, ToolProtocolError):
            # Upstream exception messages may contain responses, prompts or
            # authentication material. Never reflect them to API clients.
            self._error(502, "DeepSeek Web upstream request failed", "upstream_error")
        except (BrokenPipeError, ConnectionResetError):
            # Clients may disconnect while the DeepSeek Web stream is active.
            pass
        except Exception as exc:
            # Catch network errors without leaking tokens or request body.
            self.log_error("upstream exception: %s", type(exc).__name__)
            try:
                self._error(502, "DeepSeek Web request failed", "upstream_error")
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _run(self, prepared: PreparedRequest,
             on_text: Callable[[str], None] | None = None,
             on_thinking: Callable[[str], None] | None = None) -> Completion:
        client = self.server.client_factory()
        try:
            active_tools = bool(prepared.tools and prepared.tool_choice != "none")
            structured = bool(prepared.output_format and
                              prepared.output_format.get("type") in {"json_object", "json_schema"})
            # Upload only once, even if a malformed tool call needs correction.
            file_ids = [
                uploaded["file_id"]
                for uploaded in client.upload_files(
                    prepared.attachments, thinking_enabled=prepared.thinking,
                )
            ] if prepared.attachments else []
            tries = 2 if active_tools or structured else 1
            for attempt in range(tries):
                prompt = prepared.prompt
                if attempt:
                    prompt += ("\nFORMAT CORRECTION: Your previous answer failed format validation. "
                               "Return ONLY the required JSON output or prescribed "
                               "<deepseek_tool_calls> invocation, without a Markdown fence.")
                result = client.chat(
                    prompt,
                    ref_file_ids=file_ids,
                    thinking_enabled=prepared.thinking,
                    search_enabled=self.server.search_enabled,
                    on_text=None if active_tools or structured else on_text,
                    on_thinking=None if active_tools or structured else on_thinking,
                )
                calls = parse_tool_calls(result.text, prepared.tools, prepared.tool_choice)
                if calls:
                    return Completion("", calls)
                looked_like_tool = (
                    "<deepseek_tool_calls>" in result.text
                    or result.text.strip().startswith('{"calls"')
                    or result.text.strip().startswith('{"tool_calls"')
                )
                if active_tools and (
                    prepared.tool_choice == "required" or isinstance(prepared.tool_choice, dict)
                    or looked_like_tool
                ):
                    if attempt + 1 < tries:
                        continue
                    raise ToolProtocolError("Required tool call not returned in valid JSON")
                if structured and not valid_json_output(result.text, prepared.output_format):
                    if attempt + 1 < tries:
                        continue
                    raise ToolProtocolError("Structured JSON output did not match the requested format")
                return Completion(result.text, None)
            raise ToolProtocolError("Tool invocation failed")
        finally:
            client.session.close()

    def _handle_chat(self, data: dict[str, Any], cors: dict[str, str]) -> None:
        prepared = prepare_chat(data)
        if data.get("n", 1) != 1:
            raise RequestError("Only n=1 is supported")
        streaming = data.get("stream", False)
        if not isinstance(streaming, bool):
            raise RequestError("stream must be boolean")
        ident, timestamp = _completion_id(), int(time.time())
        include_usage = (isinstance(data.get("stream_options"), dict)
                         and bool(data["stream_options"].get("include_usage")))
        buffered = bool((prepared.tools and prepared.tool_choice != "none") or
                        (prepared.output_format and prepared.output_format.get("type") in
                         {"json_object", "json_schema"}))
        if not streaming:
            result = self._run(prepared)
            self._json(200, _chat_json(result, prepared.model, ident, timestamp, prepared.prompt), cors)
            return
        # Tool mode buffers the model output until a complete JSON invocation is verified.
        if buffered:
            result = self._run(prepared)
            self._start_sse(cors)
            self._event(_chat_chunk(prepared.model, ident, timestamp, {"role": "assistant"}))
            if result.calls:
                delta = {"tool_calls": [{
                    "index": i, "id": c["id"], "type": "function",
                    "function": c["function"],
                } for i, c in enumerate(result.calls)]}
                self._event(_chat_chunk(prepared.model, ident, timestamp, delta))
            elif result.text:
                self._event(_chat_chunk(prepared.model, ident, timestamp, {"content": result.text}))
            self._event(_chat_chunk(prepared.model, ident, timestamp, {}, _finish_reason(result.calls)))
            if include_usage:
                self._event({"id": ident, "object": "chat.completion.chunk",
                             "created": timestamp, "model": prepared.model, "choices": [],
                             "usage": _usage(prepared.prompt, result),
                             "x_deepseek_usage_estimated": True})
            self._done()
            return
        self._start_sse(cors)
        self._event(_chat_chunk(prepared.model, ident, timestamp, {"role": "assistant", "content": ""}))
        try:
            emitted = []
            def text_delta(value: str) -> None:
                emitted.append(value)
                self._event(_chat_chunk(prepared.model, ident, timestamp, {"content": value}))
            result = self._run(
                prepared,
                on_text=text_delta,
                on_thinking=(lambda x: self._event(_chat_chunk(prepared.model, ident, timestamp,
                                                                {"reasoning_content": x}))) if prepared.thinking else None,
            )
            self._event(_chat_chunk(prepared.model, ident, timestamp, {}, "stop"))
            if include_usage:
                result.text = "".join(emitted) or result.text
                self._event({"id": ident, "object": "chat.completion.chunk",
                             "created": timestamp, "model": prepared.model, "choices": [],
                             "usage": _usage(prepared.prompt, result),
                             "x_deepseek_usage_estimated": True})
        except RateLimitPause as exc:
            self._event({"error": {"message": "DeepSeek provider cooldown is active",
                                   "type": "rate_limit_error",
                                   "retry_after": math.ceil(exc.retry_after)}})
        except RateLimitStateError:
            self._event({"error": {"message": "DeepSeek request-limit state is unavailable",
                                   "type": "rate_limit_state_error", "retry_after": 60}})
        except DeepSeekAPIError as exc:
            self._event({"error": {
                "message": "DeepSeek provider rate limit reached" if exc.status == 429
                           else "DeepSeek Web generation failed",
                "type": "rate_limit_error" if exc.status == 429 else "upstream_error",
            }})
        except Exception:
            self._event({"error": {"message": "DeepSeek Web generation failed", "type": "upstream_error"}})
        self._done()

    def _handle_legacy(self, data: dict[str, Any], cors: dict[str, str]) -> None:
        prompt = data.get("prompt")
        if not isinstance(prompt, str):
            raise RequestError("Legacy completions require a string prompt")
        prepared = prepare_chat({"model": data.get("model"), "messages": [{"role": "user", "content": prompt}]})
        ident, now = _completion_id("cmpl"), int(time.time())
        stream = data.get("stream", False)
        if stream:
            self._start_sse(cors)
            try:
                self._run(prepared, on_text=lambda t: self._event({
                    "id": ident, "object": "text_completion", "created": now, "model": prepared.model,
                    "choices": [{"index": 0, "text": t, "finish_reason": None, "logprobs": None}],
                }))
                self._event({"id": ident, "object": "text_completion", "created": now, "model": prepared.model,
                             "choices": [{"index": 0, "text": "", "finish_reason": "stop", "logprobs": None}]})
            except RateLimitPause as exc:
                self._event({"error": {"message": "DeepSeek provider cooldown is active",
                                       "type": "rate_limit_error",
                                       "retry_after": math.ceil(exc.retry_after)}})
            except RateLimitStateError:
                self._event({"error": {"message": "DeepSeek request-limit state is unavailable",
                                       "type": "rate_limit_state_error", "retry_after": 60}})
            except DeepSeekAPIError as exc:
                self._event({"error": {
                    "message": "DeepSeek provider rate limit reached" if exc.status == 429
                               else "DeepSeek Web generation failed",
                    "type": "rate_limit_error" if exc.status == 429 else "upstream_error",
                }})
            except Exception:
                self._event({"error": {"message": "DeepSeek Web generation failed", "type": "upstream_error"}})
            self._done()
        else:
            result = self._run(prepared)
            self._json(200, {"id": ident, "object": "text_completion", "created": now,
                             "model": prepared.model, "choices": [{
                                 "text": result.text, "index": 0, "finish_reason": "stop", "logprobs": None,
                             }], "usage": _usage(prepared.prompt, result),
                             "x_deepseek_usage_estimated": True}, cors)

    def _handle_responses(self, data: dict[str, Any], cors: dict[str, str]) -> None:
        self._response_sequence = 0
        prev_id = data.get("previous_response_id")
        prior = None
        if prev_id is not None:
            if not isinstance(prev_id, str):
                raise RequestError("previous_response_id must be a string")
            item = self.server.recall(prev_id)
            if not item:
                raise RequestError("previous_response_id unknown or expired")
            prior = item[1]
        converted = normalize_responses_input(data, prior)
        prepared = prepare_chat(converted)
        ident, now = _completion_id("resp").replace("resp-", "resp_"), int(time.time())
        stream = data.get("stream", False)
        if not isinstance(stream, bool):
            raise RequestError("stream must be boolean")
        all_messages = converted["messages"]
        if not stream:
            result = self._run(prepared)
            output = _response_object(result, prepared.model, ident, now, prev_id, prepared.prompt)
            self.server.remember(output, all_messages + [_assistant_history(result)])
            self._json(200, output, cors)
            return
        # Emit the documented Responses event sequence; tool events are sent
        # after the backend has selected and validated the function call.
        buffered = bool((prepared.tools and prepared.tool_choice != "none") or
                        (prepared.output_format and prepared.output_format.get("type") in
                         {"json_object", "json_schema"}))
        result = self._run(prepared) if buffered else None
        provisional = _response_object(Completion("", None), prepared.model, ident, now, prev_id)
        provisional["status"] = "in_progress"
        provisional["output"] = []
        provisional["output_text"] = ""
        self._start_sse(cors)
        self._event({"type": "response.created", "response": provisional}, "response.created")
        self._event({"type": "response.in_progress", "response": provisional}, "response.in_progress")
        try:
            emitted_item_ids: list[str] = []
            if buffered:
                for i, call in enumerate((result.calls or [])):
                    item = _response_call_item(call, pending=True)
                    emitted_item_ids.append(item["id"])
                    self._event({"type": "response.output_item.added", "output_index": i, "item": item},
                                "response.output_item.added")
                    if call["type"] == "custom":
                        custom_input = call["input"]
                        self._event({"type": "response.custom_tool_call_input.delta",
                                     "output_index": i, "delta": custom_input},
                                    "response.custom_tool_call_input.delta")
                        self._event({"type": "response.custom_tool_call_input.done",
                                     "output_index": i, "input": custom_input},
                                    "response.custom_tool_call_input.done")
                        item["input"] = custom_input
                    else:
                        args = call["function"]["arguments"]
                        self._event({"type": "response.function_call_arguments.delta",
                                     "output_index": i, "delta": args},
                                    "response.function_call_arguments.delta")
                        self._event({"type": "response.function_call_arguments.done",
                                     "output_index": i, "arguments": args},
                                    "response.function_call_arguments.done")
                        item["arguments"] = args
                    item["status"] = "completed"
                    self._event({"type": "response.output_item.done", "output_index": i, "item": item},
                                "response.output_item.done")
                if not result.calls:
                    emitted_item_ids.append(self._stream_response_text_item(result.text))
            else:
                emitted_item_ids.append(self._stream_response_text_item(None, prepared))
                # Text streamed directly, but obtain full final response from _run return.
                result = self._last_stream_result
            output = _response_object(result, prepared.model, ident, now, prev_id, prepared.prompt)
            # SSE output_item.added/done and the terminal response must identify
            # the very same item, including function/custom tool calls.
            for item, item_id in zip(output["output"], emitted_item_ids):
                item["id"] = item_id
            self.server.remember(output, all_messages + [_assistant_history(result)])
            self._event({"type": "response.completed", "response": output}, "response.completed")
        except RateLimitPause as exc:
            self._event({"type": "response.failed", "error": {
                "message": "DeepSeek provider cooldown is active", "type": "rate_limit_error",
                "retry_after": math.ceil(exc.retry_after),
            }}, "response.failed")
        except RateLimitStateError:
            self._event({"type": "response.failed", "error": {
                "message": "DeepSeek request-limit state is unavailable",
                "type": "rate_limit_state_error", "retry_after": 60,
            }}, "response.failed")
        except DeepSeekAPIError as exc:
            self._event({"type": "response.failed", "error": {
                "message": "DeepSeek provider rate limit reached" if exc.status == 429
                           else "DeepSeek Web generation failed",
                "type": "rate_limit_error" if exc.status == 429 else "upstream_error",
            }}, "response.failed")
        except Exception:
            self._event({"type": "response.failed", "error": {
                "message": "DeepSeek Web generation failed", "type": "upstream_error"
            }}, "response.failed")
        self._done()

    def _stream_response_text_item(self, completed: str | None,
                                   prepared: PreparedRequest | None = None) -> str:
        item_id = "msg_" + uuid.uuid4().hex
        item = {"id": item_id, "type": "message", "role": "assistant",
                "content": [], "status": "in_progress"}
        self._event({"type": "response.output_item.added", "output_index": 0, "item": item},
                    "response.output_item.added")
        self._event({"type": "response.content_part.added", "output_index": 0, "content_index": 0,
                     "part": {"type": "output_text", "text": "", "annotations": []}},
                    "response.content_part.added")
        if prepared is not None:
            response = self._run(prepared, on_text=lambda t: self._event({
                "type": "response.output_text.delta", "output_index": 0,
                "content_index": 0, "delta": t,
            }, "response.output_text.delta"))
            self._last_stream_result = response
            text = response.text
        else:
            text = completed or ""
            if text:
                self._event({"type": "response.output_text.delta", "output_index": 0,
                             "content_index": 0, "delta": text}, "response.output_text.delta")
        self._event({"type": "response.output_text.done", "output_index": 0,
                     "content_index": 0, "text": text}, "response.output_text.done")
        part = {"type": "output_text", "text": text, "annotations": []}
        self._event({"type": "response.content_part.done", "output_index": 0,
                     "content_index": 0, "part": part}, "response.content_part.done")
        item["content"] = [part]
        item["status"] = "completed"
        self._event({"type": "response.output_item.done", "output_index": 0, "item": item},
                    "response.output_item.done")
        return item_id

    def _handle_anthropic(self, data: dict[str, Any], cors: dict[str, str]) -> None:
        converted = normalize_anthropic_request(data)
        prepared = prepare_chat(converted)
        model = converted["requested_model"]
        ident = _completion_id("msg")
        streaming = data.get("stream", False)
        if not isinstance(streaming, bool):
            raise RequestError("stream must be boolean")
        if not streaming:
            self._json(200, _anthropic_message(self._run(prepared), model, ident, prepared.prompt), cors)
            return
        tool_mode = bool(prepared.tools and prepared.tool_choice != "none")
        result = self._run(prepared) if tool_mode else None
        self._start_sse(cors)
        initial = _anthropic_message(Completion("", None), model, ident, prepared.prompt)
        initial["content"] = []
        initial["stop_reason"] = None
        initial["usage"]["output_tokens"] = 0
        self._event({"type": "message_start", "message": initial}, "message_start")
        try:
            if tool_mode and result.calls:
                for i, call in enumerate(result.calls):
                    name, args = call["function"]["name"], call["function"]["arguments"]
                    self._event({"type": "content_block_start", "index": i,
                                 "content_block": {"type": "tool_use", "id": call["id"],
                                                   "name": name, "input": {}}},
                                "content_block_start")
                    self._event({"type": "content_block_delta", "index": i,
                                 "delta": {"type": "input_json_delta", "partial_json": args}},
                                "content_block_delta")
                    self._event({"type": "content_block_stop", "index": i}, "content_block_stop")
            else:
                self._event({"type": "content_block_start", "index": 0,
                             "content_block": {"type": "text", "text": ""}},
                            "content_block_start")
                if tool_mode:
                    if result.text:
                        self._event({"type": "content_block_delta", "index": 0,
                                     "delta": {"type": "text_delta", "text": result.text}},
                                    "content_block_delta")
                else:
                    result = self._run(prepared, on_text=lambda t: self._event({
                        "type": "content_block_delta", "index": 0,
                        "delta": {"type": "text_delta", "text": t},
                    }, "content_block_delta"))
                self._event({"type": "content_block_stop", "index": 0}, "content_block_stop")
            self._event({"type": "message_delta",
                         "delta": {"stop_reason": "tool_use" if result.calls else "end_turn",
                                   "stop_sequence": None},
                         "usage": {"output_tokens": _estimate_tokens(
                             result.text or json.dumps(result.calls))}}, "message_delta")
            self._event({"type": "message_stop"}, "message_stop")
        except RateLimitPause as exc:
            self._event({"type": "error", "error": {
                "type": "rate_limit_error", "message": "DeepSeek provider cooldown is active",
                "retry_after": math.ceil(exc.retry_after),
            }}, "error")
        except RateLimitStateError:
            self._event({"type": "error", "error": {
                "type": "rate_limit_state_error",
                "message": "DeepSeek request-limit state is unavailable",
                "retry_after": 60,
            }}, "error")
        except DeepSeekAPIError as exc:
            self._event({"type": "error", "error": {
                "type": "rate_limit_error" if exc.status == 429 else "api_error",
                "message": "DeepSeek provider rate limit reached" if exc.status == 429
                           else "DeepSeek Web generation failed",
            }}, "error")
        except Exception:
            self._event({"type": "error", "error": {
                "type": "api_error", "message": "DeepSeek Web generation failed",
            }}, "error")
        self.close_connection = True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--api-key", default=None, help="Local gateway API key; prefer DEEPSEEK_BRIDGE_API_KEY")
    parser.add_argument("--max-parallel", type=int, default=1)
    parser.add_argument("--search", action="store_true", help="Enable DeepSeek Web built-in search")
    parser.add_argument("--fallback-url", default=None,
                        help="Forward other model IDs to an existing local Responses API gateway")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if not 0 <= args.port <= 65535 or args.max_parallel < 1:
        raise SystemExit("Invalid port or --max-parallel")
    api_key = args.api_key or os.environ.get("DEEPSEEK_BRIDGE_API_KEY")
    if args.host not in {"127.0.0.1", "localhost", "::1"} and not api_key:
        raise SystemExit("A gateway API key is required when binding outside localhost")
    server = Gateway((args.host, args.port), api_key=api_key,
                     max_parallel=args.max_parallel, search_enabled=args.search,
                     fallback_url=args.fallback_url,
                     fallback_api_key=os.environ.get("DEEPSEEK_FALLBACK_API_KEY") or None)
    print(f"DeepSeek Web OpenAI bridge ready at http://{args.host}:{server.server_port}/v1", flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
