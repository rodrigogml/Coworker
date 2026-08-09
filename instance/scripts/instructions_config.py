#!/usr/bin/env python3
"""Atualiza as instruções privadas da instância em caminho fixo e controlado."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TARGET = PROJECT_ROOT / "data" / "config" / "INSTRUCTIONS.md"
MAX_BYTES = 32 * 1024


def replace_from_stdin() -> int:
    content = sys.stdin.buffer.read(MAX_BYTES + 1)
    if len(content) > MAX_BYTES:
        raise ValueError("As instruções privadas excedem 32 KiB.")
    text = content.decode("utf-8")
    if not text.strip():
        raise ValueError("As instruções privadas não podem ficar vazias.")
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    temporary = TARGET.with_suffix(".md.tmp")
    temporary.write_bytes(text.encode("utf-8"))
    temporary.replace(TARGET)
    return len(content)


def main() -> int:
    parser = argparse.ArgumentParser(description="Atualiza INSTRUCTIONS.md privado sem aceitar caminho de destino.")
    parser.add_argument("action", choices=("replace",))
    args = parser.parse_args()
    try:
        size = replace_from_stdin() if args.action == "replace" else 0
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        print(f"Falha ao atualizar instruções privadas: {exc}", file=sys.stderr)
        return 1
    print(f"Instruções privadas atualizadas ({size} bytes).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
