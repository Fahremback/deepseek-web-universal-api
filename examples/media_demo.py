"""Envia um ou mais arquivos à conversa DeepSeek Web, sem navegador.

Uso: python examples/media_demo.py "foto.png" ["relatorio.pdf" ...]
Credencial: consulte docs/INSTALACAO.md.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from api_client import DeepSeekClient


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+", help="Caminhos locais de arquivos")
    parser.add_argument("--prompt", default="Analise os arquivos anexados.")
    parser.add_argument("--thinking", action="store_true")
    args = parser.parse_args()

    client = DeepSeekClient()
    result = client.chat(
        args.prompt,
        attachments=args.files,
        thinking_enabled=args.thinking,
        on_chunk=lambda chunk: print(
            f"[Arquivo pronto] {chunk.filename}: {chunk.file_id}",
            file=sys.stderr,
        )
        if chunk.event == "file"
        else None,
    )
    print(result.text)
    print("IDs:", ", ".join(result.file_ids), file=sys.stderr)


if __name__ == "__main__":
    main()
