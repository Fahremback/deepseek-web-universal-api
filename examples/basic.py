"""One message over HTTP using credentials already configured for DeepSeek Web."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api_client import DeepSeekClient


def main() -> None:
    client = DeepSeekClient()
    result = client.chat("Responda em duas frases: o que é programação?")
    print(result.text)
    print(f"session_id={result.session_id}", file=sys.stderr)
    print(f"response_message_id={result.response_message_id}", file=sys.stderr)


if __name__ == "__main__":
    main()
