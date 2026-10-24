"""Optional real-account smoke test of the OpenAI-compatible bridge.

Runs a temporary localhost listener and makes safe synthetic requests.
Usage: python examples/live_gateway_smoke.py
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import threading

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from openai_server import Gateway


TOOL = {
    "type": "function",
    "function": {
        "name": "sum_two_integers",
        "description": "Return the exact integer sum of a and b. Use this tool for arithmetic.",
        "parameters": {
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
            "additionalProperties": False,
        },
    },
}


def main() -> int:
    gateway = Gateway(("127.0.0.1", 0), max_parallel=2)
    worker = threading.Thread(target=gateway.serve_forever, daemon=True)
    worker.start()
    url = f"http://127.0.0.1:{gateway.server_port}/v1/chat/completions"
    try:
        plain = requests.post(url, json={
            "model": "deepseek-web",
            "messages": [{"role": "user", "content": "Responda somente: PONTE_OK"}],
        }, timeout=150)
        print("chat_http_status:", plain.status_code)
        print("chat_content:", plain.json().get("choices", [{}])[0].get("message", {}).get("content"))
        if plain.status_code != 200:
            return 1
        tool = requests.post(url, json={
            "model": "deepseek-web",
            "messages": [{"role": "user", "content": "Use obrigatoriamente a ferramenta sum_two_integers com a=7 e b=11."}],
            "tools": [TOOL],
            "tool_choice": "required",
        }, timeout=220)
        print("tool_http_status:", tool.status_code)
        result = tool.json()
        if tool.status_code != 200:
            print("tool_error:", result.get("error", {}).get("message"))
            return 2
        calls = result["choices"][0]["message"].get("tool_calls", [])
        print("tool_calls:", json.dumps(calls, ensure_ascii=False))
        if not calls or calls[0]["function"]["name"] != "sum_two_integers":
            return 3
        args = json.loads(calls[0]["function"]["arguments"])
        computed = args["a"] + args["b"]
        followup = requests.post(url, json={
            "model": "deepseek-web", "stream": True,
            "messages": [
                {"role": "user", "content": "Use a soma de 7 e 11 e responda apenas com o número."},
                {"role": "assistant", "content": None, "tool_calls": calls},
                {"role": "tool", "tool_call_id": calls[0]["id"], "content": str(computed)},
            ],
            "tools": [TOOL], "tool_choice": "none",
        }, timeout=150)
        chunks = [json.loads(line[6:]) for line in followup.text.splitlines()
                  if line.startswith("data: {")]
        answer = "".join(x["choices"][0]["delta"].get("content", "") for x in chunks)
        print("tool_result_stream_http_status:", followup.status_code)
        print("tool_result_stream_answer:", answer)
        if followup.status_code != 200 or str(computed) not in answer:
            return 4
        structured = requests.post(url, json={
            "model": "deepseek-web",
            "messages": [{"role": "user", "content":
                          "Responda com um JSON contendo exatamente a propriedade answer igual a PONTE_OK."}],
            "response_format": {"type": "json_schema", "json_schema": {
                "name": "answer", "schema": {"type": "object", "properties": {
                    "answer": {"type": "string", "enum": ["PONTE_OK"]}},
                    "required": ["answer"], "additionalProperties": False},
            }},
        }, timeout=150)
        print("structured_http_status:", structured.status_code)
        if structured.status_code != 200:
            print("structured_error:", structured.json().get("error", {}).get("message"))
            return 5
        parsed = json.loads(structured.json()["choices"][0]["message"]["content"])
        print("structured_result:", json.dumps(parsed, ensure_ascii=False))
        if parsed != {"answer": "PONTE_OK"}:
            return 6
        return 0
    finally:
        gateway.shutdown()
        gateway.server_close()
        worker.join(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
