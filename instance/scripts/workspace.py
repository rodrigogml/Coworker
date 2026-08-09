#!/usr/bin/env python3
"""Grava arquivos de trabalho da instância por uma interface confinada."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORK_ROOT = (PROJECT_ROOT / "data" / "work").resolve()
MAX_BYTES = 128 * 1024


class WorkspaceError(ValueError):
    """Erro de validação de uma operação do workspace."""


def _workspace_path(raw: str) -> Path:
    value = str(raw or "").strip()
    if not value:
        raise WorkspaceError("O caminho do workspace não pode ficar vazio.")
    candidate = Path(value)
    if candidate.is_absolute():
        raise WorkspaceError("Use um caminho relativo como data/work/arquivo.md.")
    resolved = (PROJECT_ROOT / candidate).resolve()
    try:
        resolved.relative_to(WORK_ROOT)
    except ValueError as exc:
        raise WorkspaceError("O caminho deve ficar dentro de data/work/.") from exc
    return resolved


def write_text(path: str, content: str) -> dict[str, object]:
    target = _workspace_path(path)
    encoded = content.encode("utf-8")
    if not encoded:
        raise WorkspaceError("O conteúdo não pode ficar vazio.")
    if len(encoded) > MAX_BYTES:
        raise WorkspaceError(f"O conteúdo excede {MAX_BYTES} bytes.")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(encoded)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return {"ok": True, "path": str(target), "bytes": len(encoded)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Grava texto somente no workspace da instância.")
    commands = parser.add_subparsers(dest="command", required=True)
    write = commands.add_parser("write", help="Cria ou substitui um arquivo textual em data/work/.")
    write.add_argument("--path", required=True)
    source = write.add_mutually_exclusive_group(required=True)
    source.add_argument("--content")
    source.add_argument("--content-file")
    args = parser.parse_args(argv)
    try:
        if args.content_file:
            source_path = _workspace_path(args.content_file)
            content = source_path.read_text(encoding="utf-8")
        else:
            content = args.content
        result = write_text(args.path, content)
    except (OSError, UnicodeError, WorkspaceError) as exc:
        print(f"Falha ao gravar workspace: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
