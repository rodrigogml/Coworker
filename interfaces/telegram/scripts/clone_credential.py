#!/usr/bin/env python3
"""Clona campos de uma entrada KeePassXC em um trabalho Telegram.

Este ponto de entrada é não interativo: valida o contexto do job, cria a entrada
de destino exclusivamente e nunca retorna valores protegidos.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from interfaces.telegram.credential_broker import (  # noqa: E402
    CredentialBrokerError,
    job_context_from_environment,
)
from scripts.credential_vault import (  # noqa: E402
    VaultToolError,
    clone_entry_fields,
    validate_entry_path,
)


def print_json(value: object, *, stream: object = sys.stdout) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2), file=stream)


def clone(args: argparse.Namespace) -> dict[str, object]:
    if not args.confirm:
        raise CredentialBrokerError("A clonagem exige a confirmação explícita --confirm.")
    try:
        _job_root, job_id, _chat_id = job_context_from_environment()
        source = validate_entry_path(args.source)
        target = validate_entry_path(args.target)
    except (CredentialBrokerError, VaultToolError) as exc:
        raise CredentialBrokerError(str(exc)) from exc
    fields = tuple(args.field)
    try:
        clone_entry_fields(source, target, fields)
    except VaultToolError as exc:
        raise CredentialBrokerError(str(exc)) from exc
    return {
        "ok": True,
        "job_id": job_id,
        "source": source,
        "target": target,
        "fields": list(fields),
        "created": True,
        "source_preserved": True,
        "secret_exposed": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Cria uma entrada KeePassXC por cópia interna no contexto do gateway."
    )
    parser.add_argument("--source", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument(
        "--field", action="append", choices=("Username", "Password"), required=True
    )
    parser.add_argument("--confirm", action="store_true")
    return parser


def main() -> int:
    try:
        result = clone(build_parser().parse_args())
    except (CredentialBrokerError, OSError) as exc:
        print_json(
            {"ok": False, "error": str(exc), "error_type": type(exc).__name__},
            stream=sys.stderr,
        )
        return 1
    print_json(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
