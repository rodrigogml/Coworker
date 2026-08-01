#!/usr/bin/env python3
"""Publica um arquivo na saída do trabalho sem expor seu conteúdo."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path


def unique_destination(directory: Path, name: str) -> Path:
    candidate = directory / name
    if not candidate.exists():
        return candidate
    for index in range(1, 10_000):
        candidate = directory / f"{Path(name).stem}-{index}{Path(name).suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError("Não foi possível reservar um nome de saída.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("--name", default="")
    parser.add_argument("--max-bytes", type=int, default=20 * 1024 * 1024)
    args = parser.parse_args()
    output_value = os.environ.get("BOTINA_JOB_OUTPUT", "").strip()
    if not output_value:
        raise RuntimeError("BOTINA_JOB_OUTPUT não foi definido pelo gateway.")
    source = Path(args.source).expanduser().resolve(strict=True)
    output = Path(output_value).expanduser().resolve(strict=True)
    if not source.is_file() or source.is_symlink():
        raise RuntimeError("A origem não é um arquivo regular.")
    size = source.stat().st_size
    if size <= 0 or size > args.max_bytes:
        raise RuntimeError("A origem está vazia ou excede o limite permitido.")
    requested_name = Path(args.name or source.name).name
    destination = unique_destination(output, requested_name)
    shutil.copyfile(source, destination)
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    print(json.dumps({"path": destination.name, "size_bytes": size, "sha256": digest}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        raise SystemExit(1)
