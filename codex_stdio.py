"""Run one DeepSeek Responses request over stdin/stdout without opening a port."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

from api_client import AuthenticationError, DeepSeekAPIError, DeepSeekClient, ProofOfWorkError
from harness_adapter import MODEL_IDS, RequestError, normalize_responses_input, prepare_chat
from openai_server import GatewayHandler, _completion_id, _response_object


class StdioHandler(GatewayHandler):
    def __init__(self) -> None:
        self.stream_failed = False
        self.server = SimpleNamespace(
            client_factory=DeepSeekClient,
            search_enabled=False,
            recall=lambda _ident: None,
            remember=lambda _response, _messages: None,
        )
        self.wfile = sys.stdout.buffer

    def _start_sse(self, _headers: dict[str, str]) -> None:
        pass

    def _event(self, obj: dict, event: str | None = None) -> None:
        if event == "response.failed":
            self.stream_failed = True
        super()._event(obj, event)


def _repair_unicode(value: object) -> object:
    """Combine valid surrogate pairs and replace lone halves rejected upstream."""
    if isinstance(value, str):
        return value.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
    if isinstance(value, list):
        return [_repair_unicode(item) for item in value]
    if isinstance(value, dict):
        return {key: _repair_unicode(item) for key, item in value.items()}
    return value


def main() -> int:
    try:
        data = _repair_unicode(json.load(sys.stdin))
        if not isinstance(data, dict):
            raise RequestError("Request must be a JSON object")
        if data.get("model") not in MODEL_IDS:
            raise RequestError("Unknown DeepSeek model")
        if data.get("previous_response_id"):
            raise RequestError("Previous response must be expanded by the gateway")
        if not isinstance(data.get("stream", False), bool):
            raise RequestError("stream must be boolean")
        handler = StdioHandler()
        if data.get("stream") is True:
            handler._handle_responses(data, {})
            return 1 if handler.stream_failed else 0
        converted = normalize_responses_input(data)
        prepared = prepare_chat(converted)
        result = handler._run(prepared)
        ident = _completion_id("resp").replace("resp-", "resp_")
        import time
        response = _response_object(result, prepared.model, ident, int(time.time()), None, prepared.prompt)
        json.dump(response, sys.stdout, ensure_ascii=False, separators=(",", ":"))
        sys.stdout.flush()
        return 0
    except (RequestError, AuthenticationError, DeepSeekAPIError, ProofOfWorkError) as exc:
        # Exception descriptions may include upstream responses, credentials,
        # or user-provided text. Only report the safe exception class.
        print(f"DeepSeek request failed: {type(exc).__name__}", file=sys.stderr)
    except Exception as exc:
        print(f"DeepSeek request failed: {type(exc).__name__}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
