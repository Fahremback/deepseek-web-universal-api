"""Create a chat and send a follow-up within the same remote DeepSeek session."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api_client import DeepSeekClient


def main() -> None:
    client = DeepSeekClient()

    first = client.chat("Liste três ideias para um pequeno programa Python.")
    if first.response_message_id is None:
        raise RuntimeError("DeepSeek did not provide response_message_id")

    followup = client.chat(
        "Desenvolva a segunda ideia.",
        chat_session_id=first.session_id,
        parent_message_id=first.response_message_id,
    )

    print(f"Session: {followup.session_id}")
    print(f"First message id: {first.response_message_id}")
    print(f"Latest message id: {followup.response_message_id}")
    print(followup.text)


if __name__ == "__main__":
    main()
