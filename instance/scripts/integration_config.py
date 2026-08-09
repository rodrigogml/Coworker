#!/usr/bin/env python3
"""Inicializa configurações privadas de integrações a partir de modelos públicos."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tomllib
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
    "ssh": "ssh",
    "todoist": "todoist",
}
PROFILE_INTEGRATIONS = {"bis2", "ssh"}
PROFILE_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


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


def _validate_profile_name(value: str) -> str:
    name = str(value or "").strip()
    if not PROFILE_NAME_PATTERN.fullmatch(name):
        raise IntegrationConfigError(
            "O nome do perfil deve começar por letra e conter somente letras, números, hífen ou sublinhado."
        )
    return name


def _validate_profile_host(value: str) -> str:
    host = str(value or "").strip()
    if not host or len(host) > 253 or any(char in host for char in "\r\n\0 \t"):
        raise IntegrationConfigError("O host do perfil é inválido.")
    return host


def _validate_profile_port(value: int) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise IntegrationConfigError("A porta do perfil deve ser um número inteiro.") from exc
    if not 1 <= port <= 65535:
        raise IntegrationConfigError("A porta do perfil deve estar entre 1 e 65535.")
    return port


def _validate_credential_ref(value: str) -> str:
    reference = str(value or "").strip().strip("/")
    parts = reference.split("/")
    if not reference or any(
        not part or part in {".", ".."} or part.startswith("-") or any(char in part for char in "\r\n\0")
        for part in parts
    ):
        raise IntegrationConfigError("A referência da credencial é inválida.")
    return reference


def _private_config(integration: str, project_root: Path) -> Path:
    _source, destination = _paths(integration, project_root)
    if not destination.is_file():
        raise IntegrationConfigError(
            f"Configure primeiro a integração com '{initialization_command(integration)}'."
        )
    return destination


def list_profiles(integration: str, *, project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    """Lista perfis não confidenciais da integração privada."""
    if integration not in PROFILE_INTEGRATIONS:
        raise IntegrationConfigError(f"Gerenciamento de perfis ainda não está disponível para {integration}.")
    destination = _private_config(integration, project_root)
    try:
        values = tomllib.loads(destination.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise IntegrationConfigError("A configuração privada da integração é inválida.") from exc
    profiles = values.get("profiles", {})
    if not isinstance(profiles, dict):
        raise IntegrationConfigError("A seção de perfis da integração é inválida.")
    return {
        "ok": True,
        "integration": integration,
        "profiles": [
            {
                "name": name,
                "host": str(profile.get("host", "")),
                "port": int(profile.get("port", 0)),
                "credential_ref": str(profile.get("credential_ref", "")),
                **({"attachment_name": str(profile.get("attachment_name", ""))}
                   if integration == "ssh" else {}),
            }
            for name, profile in profiles.items()
            if isinstance(profile, dict)
        ],
        "path": f"data/config/{INTEGRATIONS[integration]}.toml",
    }


def add_profile(
    integration: str,
    name: str,
    host: str,
    port: int,
    credential_ref: str,
    attachment_name: str | None = None,
    *,
    project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    """Adiciona um perfil tipado sem aceitar TOML ou JSON arbitrário."""
    if integration not in PROFILE_INTEGRATIONS:
        raise IntegrationConfigError(f"Gerenciamento de perfis ainda não está disponível para {integration}.")
    profile_name = _validate_profile_name(name)
    profile_host = _validate_profile_host(host)
    profile_port = _validate_profile_port(port)
    profile_credential = _validate_credential_ref(credential_ref)
    attachment = str(attachment_name or "").strip()
    if integration == "ssh" and attachment and any(char in attachment for char in "\\/\r\n\0"):
        raise IntegrationConfigError("O nome do anexo SSH é inválido.")
    destination = _private_config(integration, project_root)
    try:
        original = destination.read_bytes()
        values = tomllib.loads(original.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise IntegrationConfigError("A configuração privada da integração é inválida.") from exc
    profiles = values.get("profiles", {})
    if not isinstance(profiles, dict):
        raise IntegrationConfigError("A seção de perfis da integração é inválida.")
    if profile_name in profiles:
        raise IntegrationConfigError(f"O perfil '{profile_name}' já existe.")
    section = (
        f"\n[profiles.{profile_name}]\n"
        f"host = {json.dumps(profile_host, ensure_ascii=False)}\n"
        f"port = {profile_port}\n"
        f"credential_ref = {json.dumps(profile_credential, ensure_ascii=False)}\n"
    )
    if integration == "ssh":
        section += f"attachment_name = {json.dumps(attachment, ensure_ascii=False)}\n"
    updated = original.decode("utf-8")
    if not updated.endswith("\n"):
        updated += "\n"
    updated += section
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(updated, encoding="utf-8", newline="\n")
        if destination.read_bytes() != original:
            raise IntegrationConfigError("A configuração mudou durante a operação; tente novamente.")
        temporary.replace(destination)
    except IntegrationConfigError:
        temporary.unlink(missing_ok=True)
        raise
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise IntegrationConfigError("Não foi possível salvar o novo perfil.") from exc
    return {
        "ok": True,
        "integration": integration,
        "profile": profile_name,
        "created": True,
        "path": f"data/config/{INTEGRATIONS[integration]}.toml",
    }


def set_profile(
    integration: str,
    name: str,
    host: str,
    port: int,
    credential_ref: str,
    attachment_name: str | None = None,
    *,
    project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    """Cria ou atualiza um perfil tipado, preservando os demais perfis."""
    if integration not in PROFILE_INTEGRATIONS:
        raise IntegrationConfigError(
            f"Gerenciamento de perfis ainda não está disponível para {integration}."
        )
    profile_name = _validate_profile_name(name)
    profile_host = _validate_profile_host(host)
    profile_port = _validate_profile_port(port)
    profile_credential = _validate_credential_ref(credential_ref)
    attachment = str(attachment_name or "").strip()
    if integration == "ssh" and attachment and any(char in attachment for char in "\\/\r\n\0"):
        raise IntegrationConfigError("O nome do anexo SSH é inválido.")
    destination = _private_config(integration, project_root)
    try:
        original = destination.read_bytes()
        values = tomllib.loads(original.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise IntegrationConfigError("A configuração privada da integração é inválida.") from exc
    profiles = values.get("profiles", {})
    if not isinstance(profiles, dict):
        raise IntegrationConfigError("A seção de perfis da integração é inválida.")
    section = (
        f"[profiles.{profile_name}]\n"
        f"host = {json.dumps(profile_host, ensure_ascii=False)}\n"
        f"port = {profile_port}\n"
        f"credential_ref = {json.dumps(profile_credential, ensure_ascii=False)}\n"
    )
    if integration == "ssh":
        section += f"attachment_name = {json.dumps(attachment, ensure_ascii=False)}\n"
    pattern = re.compile(
        rf"(?ms)^\[profiles\.{re.escape(profile_name)}\]\r?\n.*?(?=^\[|\Z)"
    )
    updated = original.decode("utf-8")
    if pattern.search(updated):
        updated = pattern.sub(section, updated, count=1)
    else:
        if updated and not updated.endswith("\n"):
            updated += "\n"
        updated += "\n" + section
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(updated, encoding="utf-8", newline="\n")
        if destination.read_bytes() != original:
            raise IntegrationConfigError("A configuração mudou durante a operação; tente novamente.")
        temporary.replace(destination)
    except IntegrationConfigError:
        temporary.unlink(missing_ok=True)
        raise
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise IntegrationConfigError("Não foi possível salvar o perfil.") from exc
    return {
        "ok": True,
        "integration": integration,
        "profile": profile_name,
        "created": profile_name not in profiles,
        "updated": profile_name in profiles,
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
    profile = commands.add_parser("profile", help="Administra perfis tipados de integrações.")
    profile_commands = profile.add_subparsers(dest="profile_command", required=True)
    profile_list = profile_commands.add_parser("list", help="Lista perfis não confidenciais.")
    profile_list.add_argument("integration", choices=tuple(sorted(PROFILE_INTEGRATIONS)))
    profile_add = profile_commands.add_parser("add", help="Adiciona um perfil sem editar TOML livremente.")
    profile_add.add_argument("integration", choices=tuple(sorted(PROFILE_INTEGRATIONS)))
    profile_add.add_argument("--name", required=True)
    profile_add.add_argument("--host", required=True)
    profile_add.add_argument("--port", required=True, type=int)
    profile_add.add_argument("--credential-ref", required=True)
    profile_add.add_argument("--attachment-name", default="")
    profile_set = profile_commands.add_parser("set", help="Cria ou atualiza um perfil tipado.")
    profile_set.add_argument("integration", choices=tuple(sorted(PROFILE_INTEGRATIONS)))
    profile_set.add_argument("--name", required=True)
    profile_set.add_argument("--host", required=True)
    profile_set.add_argument("--port", required=True, type=int)
    profile_set.add_argument("--credential-ref", required=True)
    profile_set.add_argument("--attachment-name", default="")
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
        if args.command == "list":
            result = list_integrations()
        elif args.command == "init":
            result = initialize_integration(args.integration)
        elif args.profile_command == "list":
            result = list_profiles(args.integration)
        elif args.profile_command == "add":
            result = add_profile(
                args.integration,
                args.name,
                args.host,
                args.port,
                args.credential_ref,
                args.attachment_name,
            )
        else:
            result = set_profile(
                args.integration,
                args.name,
                args.host,
                args.port,
                args.credential_ref,
                args.attachment_name,
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
