#!/usr/bin/env python3
"""Solicita ao gateway uma captura protegida sem receber o valor confidencial."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from interfaces.telegram.credential_broker import (  # noqa: E402
    CredentialBrokerError,
    create_request,
    parse_field_spec,
)


def print_json(value: object, *, stream: object = sys.stdout) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2), file=stream)


def request_credential(args: argparse.Namespace) -> dict[str, object]:
    fields = [parse_field_spec(value) for value in args.field]
    request = create_request(args.entry, args.prompt, fields, args.timeout)
    deadline = time.monotonic() + args.timeout
    try:
        while time.monotonic() < deadline:
            if request.response_path.is_file():
                try:
                    response = json.loads(request.response_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError) as exc:
                    raise CredentialBrokerError("Resposta protegida inválida.") from exc
                if response.get("request_id") != request.request_id:
                    raise CredentialBrokerError("Resposta protegida não corresponde à solicitação.")
                if response.get("ok") is not True:
                    raise CredentialBrokerError(
                        str(response.get("error") or "Captura protegida não concluída.")
                    )
                return {
                    "ok": True,
                    "credential_stored": True,
                    "entry": request.entry,
                    "fields": [field.name for field in request.fields],
                    "secret_exposed": False,
                }
            time.sleep(0.25)
        raise CredentialBrokerError("A captura protegida expirou.")
    finally:
        request.request_path.unlink(missing_ok=True)
        request.response_path.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Solicita campos protegidos ao usuário por meio do gateway."
    )
    parser.add_argument("--entry", required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument(
        "--field",
        action="append",
        required=True,
        help="Campo protegido no formato username:Rótulo ou password:Rótulo.",
    )
    parser.add_argument("--timeout", type=int, default=600, choices=range(60, 1801))
    return parser


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        result = request_credential(build_parser().parse_args())
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
