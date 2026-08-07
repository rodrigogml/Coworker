#!/usr/bin/env python3
"""Inicializa configurações privadas de integrações a partir de modelos públicos."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INTEGRATIONS = {
    "bis2": "bis2",
    "calendar": "calendar",
    "cloudflare": "cloudflare",
    "contacts": "contacts",
    "drive": "drive",
    "forwardemail": "forwardemail",
    "gmail": "gmail",
    "google": "google",
    "notion": "notion",
    "omie": "omie",
    "todoist": "todoist",
}


class IntegrationConfigError(RuntimeError):
    """Indica um bootstrap inválido sem expor dados privados."""


def initialization_command(integration: str) -> str:
    """Devolve o único comando público de inicialização da integração."""
    if integration not in INTEGRATIONS:
        raise IntegrationConfigError(f"Integração desconhecida: {integration}.")
    return f"python scripts/integration_config.py init {integration}"


def missing_config_message(integration: str, path: Path) -> str:
    """Produz orientação executável para uma configuração ausente."""
    return (
        f"Configuração {integration} não encontrada em '{path}'. Execute "
        f"'{initialization_command(integration)}'."
    )


def _paths(integration: str, project_root: Path) -> tuple[Path, Path]:
    try:
        filename = INTEGRATIONS[integration]
    except KeyError as exc:
        raise IntegrationConfigError(
            f"Integração desconhecida: {integration}."
        ) from exc
    root = project_root.resolve(strict=True)
    public_dir = (root / "config").resolve(strict=True)
    source = (public_dir / f"{filename}.example.toml").resolve(strict=True)
    try:
        source.relative_to(public_dir)
    except ValueError as exc:
        raise IntegrationConfigError("Modelo público fora de config/.") from exc
    data_dir = root / "data"
    private_dir = data_dir / "config"
    data_dir.mkdir(exist_ok=True)
    resolved_data = data_dir.resolve(strict=True)
    try:
        resolved_data.relative_to(root)
    except ValueError as exc:
        raise IntegrationConfigError(
            "O diretório privado deve permanecer dentro de data/config/."
        ) from exc
    private_dir.mkdir(exist_ok=True)
    resolved_private = private_dir.resolve(strict=True)
    try:
        resolved_private.relative_to(resolved_data)
    except ValueError as exc:
        raise IntegrationConfigError(
            "O diretório privado deve permanecer dentro de data/config/."
        ) from exc
    return source, resolved_private / f"{filename}.toml"


def initialize_integration(
    integration: str,
    *,
    project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    """Copia um modelo conhecido uma única vez, sem sobrescrever o destino."""
    try:
        source, destination = _paths(integration, project_root)
        payload = source.read_bytes()
    except FileNotFoundError as exc:
        raise IntegrationConfigError(
            f"Modelo público da integração {integration} não encontrado."
        ) from exc
    try:
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError:
        status = "already_exists"
    except OSError as exc:
        raise IntegrationConfigError(
            f"Não foi possível criar a configuração {integration}."
        ) from exc
    else:
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
        except OSError as exc:
            destination.unlink(missing_ok=True)
            raise IntegrationConfigError(
                f"Não foi possível concluir a configuração {integration}."
            ) from exc
        status = "created"
    return {
        "ok": True,
        "integration": integration,
        "status": status,
        "path": f"data/config/{INTEGRATIONS[integration]}.toml",
    }


def list_integrations(*, project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    """Lista o catálogo fechado e a presença de cada configuração privada."""
    root = project_root.resolve(strict=True)
    return {
        "ok": True,
        "integrations": [
            {
                "name": name,
                "configured": (
                    root / "data" / "config" / f"{filename}.toml"
                ).is_file(),
                "command": initialization_command(name),
            }
            for name, filename in INTEGRATIONS.items()
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inicializa configurações locais de integrações conhecidas."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="Lista integrações e configurações presentes.")
    init = commands.add_parser("init", help="Cria uma configuração sem sobrescrever.")
    init.add_argument("integration", choices=tuple(INTEGRATIONS))
    return parser


def print_json(value: Any, *, stream: Any = sys.stdout) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2), file=stream)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    args = build_parser().parse_args()
    try:
        result = (
            list_integrations()
            if args.command == "list"
            else initialize_integration(args.integration)
        )
    except (IntegrationConfigError, OSError) as exc:
        print_json(
            {"ok": False, "error": str(exc), "error_type": type(exc).__name__},
            stream=sys.stderr,
        )
        return 1
    print_json(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
