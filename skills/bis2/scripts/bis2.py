#!/usr/bin/env python3
"""Executa o BISCMD com perfis BIS2 e credenciais obtidas do KeePassXC."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = PROJECT_ROOT / "data" / "config" / "bis2.toml"
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from credential_vault import VaultToolError, read_entry_credentials  # noqa: E402
from integration_config import missing_config_message  # noqa: E402
from integration_profiles import IntegrationProfileError, validate_profile_name  # noqa: E402


class Bis2ToolError(RuntimeError):
    """Erro esperado e seguro para apresentação ao chamador."""


@dataclass(frozen=True)
class Bis2Profile:
    """Perfil local de acesso a um servidor BIS2."""

    name: str
    host: str
    port: int
    credential_ref: str


@dataclass(frozen=True)
class Bis2Config:
    """Configuração local da skill BIS2."""

    java_executable: str
    jar_path: Path
    working_dir: Path
    timeout_seconds: int
    default_profile: str
    profiles: dict[str, Bis2Profile]

    def resolve_profile(self, requested: str | None) -> tuple[Bis2Profile, bool]:
        """Resolve o perfil solicitado e informa se ele foi explicitado."""
        explicit = requested is not None
        profile_name = validate_profile_name(requested or self.default_profile)
        profile = self.profiles.get(profile_name)
        if profile is None:
            available = ", ".join(sorted(self.profiles))
            raise Bis2ToolError(f"Perfil BIS2 '{profile_name}' não encontrado. Disponíveis: {available}.")
        return profile, explicit


def _configured_path(raw_value: Any, field: str) -> Path:
    value = str(raw_value or "").strip()
    if not value:
        raise Bis2ToolError(f"O campo '{field}' não pode ficar vazio.")
    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def load_config(path: Path = CONFIG_PATH) -> Bis2Config:
    """Carrega e valida a configuração privada da skill."""
    try:
        values = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise Bis2ToolError(missing_config_message("bis2", path)) from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise Bis2ToolError(f"Não foi possível carregar a configuração BIS2 '{path}'.") from exc

    jar_path = _configured_path(values.get("jar_path"), "jar_path")
    working_dir = _configured_path(values.get("working_dir", jar_path.parent), "working_dir")
    timeout_seconds = int(values.get("timeout_seconds", 300))
    if timeout_seconds <= 0:
        raise Bis2ToolError("'timeout_seconds' deve ser positivo.")
    default_profile = validate_profile_name(str(values.get("default_profile", "")).strip())
    raw_profiles = values.get("profiles")
    if not isinstance(raw_profiles, dict) or not raw_profiles:
        raise Bis2ToolError("'profiles' deve conter ao menos um perfil.")
    profiles: dict[str, Bis2Profile] = {}
    for raw_name, raw_profile in raw_profiles.items():
        name = validate_profile_name(str(raw_name))
        if not isinstance(raw_profile, dict):
            raise Bis2ToolError(f"'profiles.{name}' deve ser uma tabela.")
        host = str(raw_profile.get("host", "")).strip()
        credential_ref = str(raw_profile.get("credential_ref", "")).strip()
        try:
            port = int(raw_profile.get("port", 8080))
        except (TypeError, ValueError) as exc:
            raise Bis2ToolError(f"'profiles.{name}.port' deve ser inteiro.") from exc
        if not host:
            raise Bis2ToolError(f"'profiles.{name}.host' não pode ficar vazio.")
        if not 1 <= port <= 65535:
            raise Bis2ToolError(f"'profiles.{name}.port' deve estar entre 1 e 65535.")
        if not credential_ref:
            raise Bis2ToolError(f"'profiles.{name}.credential_ref' não pode ficar vazio.")
        profiles[name] = Bis2Profile(name, host, port, credential_ref)
    if default_profile not in profiles:
        raise Bis2ToolError(f"O perfil padrão BIS2 '{default_profile}' não existe.")
    return Bis2Config(
        java_executable=str(values.get("java_executable", "")).strip(),
        jar_path=jar_path,
        working_dir=working_dir,
        timeout_seconds=timeout_seconds,
        default_profile=default_profile,
        profiles=profiles,
    )


def _java_executable(config: Bis2Config) -> str:
    if config.java_executable:
        path = _configured_path(config.java_executable, "java_executable")
        if not path.is_file():
            raise Bis2ToolError(f"Java configurado não encontrado em '{path}'.")
        return str(path)
    discovered = shutil.which("java")
    if not discovered:
        raise Bis2ToolError("Java não encontrado no PATH e 'java_executable' não foi configurado.")
    return discovered


def _sanitize(value: str, secrets: tuple[str, ...]) -> str:
    sanitized = value
    for secret in secrets:
        if secret:
            sanitized = sanitized.replace(secret, "[Censurado por segurança]")
    sanitized = re.sub(r"(?i)(biscmd\.password=)[^\s]+", r"\1[Censurado por segurança]", sanitized)
    return sanitized


def run_biscmd(
    config: Bis2Config,
    profile: Bis2Profile,
    arguments: list[str],
) -> dict[str, Any]:
    """Executa o JAR do BISCMD injetando credenciais pelo ambiente do processo."""
    if not config.jar_path.is_file():
        raise Bis2ToolError(f"JAR do BISCMD não encontrado em '{config.jar_path}'.")
    if not config.working_dir.is_dir():
        raise Bis2ToolError(f"Pasta de execução do BISCMD não encontrada em '{config.working_dir}'.")
    username, password = read_entry_credentials(profile.credential_ref)
    env = os.environ.copy()
    env.update(
        {
            "BISCMD_HOST": profile.host,
            "BISCMD_PORT": str(profile.port),
            "BISCMD_USER": username,
            "BISCMD_PASSWORD": password,
        }
    )
    command = [_java_executable(config), "-jar", str(config.jar_path), *arguments]
    try:
        completed = subprocess.run(
            command,
            cwd=str(config.working_dir),
            env=env,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=config.timeout_seconds,
        )
    finally:
        env["BISCMD_PASSWORD"] = ""
        password = ""
    stdout = _sanitize(completed.stdout, (username,))
    stderr = _sanitize(completed.stderr, (username,))
    return {
        "ok": completed.returncode == 0,
        "profile": profile.name,
        "host": profile.host,
        "port": profile.port,
        "returncode": completed.returncode,
        "stdout": stdout,
        "stderr": stderr,
    }


def _base_arguments() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Executa operações permitidas do BIS2 via BISCMD.")
    parser.add_argument("--profile", help="Perfil BIS2 configurado em data/config/bis2.toml.")
    return parser


def build_parser() -> argparse.ArgumentParser:
    parser = _base_arguments()
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("doctor", help="Valida autenticação e lookup das fachadas remotas.")
    commands.add_parser("help", help="Exibe ajuda do BISCMD.")

    list_keys = commands.add_parser("nfce-listagem-chaves", help="Lista chaves NFC-e de um período.")
    list_keys.add_argument("--company-id", required=True)
    list_keys.add_argument("--certificate-id", required=True)
    list_keys.add_argument("--start", required=True)
    list_keys.add_argument("--end", required=True)

    download = commands.add_parser("nfce-download-xml", help="Consulta ou persiste XML autorizado de NFC-e.")
    download.add_argument("--company-id", required=True)
    download.add_argument("--certificate-id", required=True)
    download.add_argument("--key", required=True)
    download.add_argument("--output")
    download.add_argument("--confirm", action="store_true")

    inutilize = commands.add_parser("nfce-inutilize-number", help="Inutiliza faixa de NFC-e na SEFAZ.")
    inutilize.add_argument("--company-id", required=True)
    inutilize.add_argument("--certificate-id", required=True)
    inutilize.add_argument("--serie", required=True)
    inutilize.add_argument("--number-start", required=True)
    inutilize.add_argument("--number-end", required=True)
    inutilize.add_argument("--confirm", action="store_true", required=True)

    send = commands.add_parser("nfce-send-offline", help="Envia NFC-e em contingência offline.")
    mode = send.add_mutually_exclusive_group(required=True)
    mode.add_argument("--doc-id")
    mode.add_argument("--ids")
    mode.add_argument("--all", action="store_true")
    send.add_argument("--confirm", action="store_true", required=True)

    validate = commands.add_parser("validate-doc-fiscal", help="Revalida documento fiscal por ID.")
    validate.add_argument("--doc-id", required=True)

    update = commands.add_parser("update-doc-fiscal-status", help="Atualiza status operacional de documento fiscal.")
    update.add_argument("--serie", required=True)
    update.add_argument("--number", required=True)
    update.add_argument("--status", required=True)

    return parser


def build_biscmd_arguments(args: argparse.Namespace) -> tuple[list[str], bool]:
    """Converte comandos seguros do wrapper para argumentos do BISCMD."""
    if args.command == "doctor":
        return ["-facade"], False
    if args.command == "help":
        return ["-h"], False
    if args.command == "nfce-listagem-chaves":
        return [
            "-facade",
            "-nfceListagemChaves",
            "companyId",
            args.company_id,
            "certificateId",
            args.certificate_id,
            "start",
            args.start,
            "end",
            args.end,
        ], False
    if args.command == "nfce-download-xml":
        command = [
            "-facade",
            "-nfceDownloadXML",
            "companyId",
            args.company_id,
            "certificateId",
            args.certificate_id,
            "key",
            args.key,
        ]
        if args.output:
            command.extend(["output", args.output])
        command.append("confirm" if args.confirm else "dryRun")
        return command, bool(args.confirm)
    if args.command == "nfce-inutilize-number":
        return [
            "-facade",
            "-nfceInutilizeNumber",
            "companyId",
            args.company_id,
            "certificateId",
            args.certificate_id,
            "serie",
            args.serie,
            "numberStart",
            args.number_start,
            "numberEnd",
            args.number_end,
            "confirm",
        ], True
    if args.command == "nfce-send-offline":
        command = ["-facade", "-nfceSendOffline"]
        if args.all:
            command.append("all")
        elif args.ids:
            command.extend(["ids", args.ids])
        else:
            command.extend(["docId", args.doc_id])
        command.append("confirm")
        return command, True
    if args.command == "validate-doc-fiscal":
        return ["-facade", "-validateDocFiscal", args.doc_id], True
    if args.command == "update-doc-fiscal-status":
        return ["-facade", "-updateDocFiscalStatus", args.serie, args.number, args.status], True
    raise Bis2ToolError(f"Comando não suportado: {args.command}.")


def print_json(payload: Any, *, stream: Any = sys.stdout) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2), file=stream)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    args = build_parser().parse_args()
    try:
        config = load_config()
        profile, explicit_profile = config.resolve_profile(args.profile)
        biscmd_args, mutating = build_biscmd_arguments(args)
        if mutating and not explicit_profile:
            raise Bis2ToolError(
                "Operações com efeito externo ou alteração no BIS2 exigem --profile explícito."
            )
        result = run_biscmd(config, profile, biscmd_args)
    except (Bis2ToolError, VaultToolError, IntegrationProfileError, OSError, subprocess.SubprocessError) as exc:
        print_json({"ok": False, "error": str(exc), "error_type": type(exc).__name__}, stream=sys.stderr)
        return 1
    print_json(result)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
