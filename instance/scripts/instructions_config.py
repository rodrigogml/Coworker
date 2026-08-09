#!/usr/bin/env python3
"""Atualiza as instruções privadas da instância em caminho fixo e controlado."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TARGET = PROJECT_ROOT / "data" / "config" / "INSTRUCTIONS.md"
WORK_ROOT = (PROJECT_ROOT / "data" / "work").resolve()
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


def replace_from_workspace(source: str) -> int:
    candidate = (PROJECT_ROOT / source).resolve()
    try:
        candidate.relative_to(WORK_ROOT)
    except ValueError as exc:
        raise ValueError("A origem deve ficar dentro de data/work/.") from exc
    content = candidate.read_bytes()
    if len(content) > MAX_BYTES:
        raise ValueError("As instruções privadas excedem 32 KiB.")
    text = content.decode("utf-8")
    if not text.strip():
        raise ValueError("As instruções privadas não podem ficar vazias.")
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    temporary = TARGET.with_suffix(".md.tmp")
    temporary.write_bytes(content)
    temporary.replace(TARGET)
    return len(content)


def main() -> int:
    parser = argparse.ArgumentParser(description="Atualiza INSTRUCTIONS.md privado sem aceitar caminho de destino.")
    parser.add_argument("action", choices=("replace", "replace-file"))
    parser.add_argument("--source", help="Arquivo UTF-8 em data/work/ para replace-file.")
    args = parser.parse_args()
    try:
        if args.action == "replace-file":
            if not args.source:
                raise ValueError("replace-file exige --source.")
            size = replace_from_workspace(args.source)
        else:
            size = replace_from_stdin()
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        print(f"Falha ao atualizar instruções privadas: {exc}", file=sys.stderr)
        return 1
    print(f"Instruções privadas atualizadas ({size} bytes).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
