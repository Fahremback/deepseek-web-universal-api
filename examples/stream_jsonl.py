"""Forward real-time thinking/text/title/continue/done as JSONL on stdout."""
from pathlib import Path
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api_client import DeepSeekClient, StreamChunk


def send(event: dict) -> None:
    print(json.dumps(event, ensure_ascii=False, separators=(",", ":")), flush=True)


def main() -> None:
    client = DeepSeekClient()

    def on_chunk(chunk: StreamChunk) -> None:
        if chunk.thinking:
            send({"type": "thinking", "delta": chunk.thinking})
        if chunk.text:
            send({"type": "text", "delta": chunk.text})
        if chunk.title:
            send({"type": "title", "title": chunk.title})
        if chunk.event == "continue":
            send({"type": "continue", "attempt": chunk.attempt})

    result = client.chat(
        "Explique a diferença entre IPv4 e IPv6.",
        thinking_enabled=True,
        on_chunk=on_chunk,
    )
    send({
        "type": "done",
        "session_id": result.session_id,
        "response_message_id": result.response_message_id,
        "status": result.status,
        "continuations": result.continuations,
    })


if __name__ == "__main__":
    main()
