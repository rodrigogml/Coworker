#!/usr/bin/env python3
"""Executa o BIS10CMD com perfis e credenciais privadas do KeePassXC."""

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
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = PROJECT_ROOT / "data" / "config" / "bis10.toml"
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from credential_vault import read_entry_credentials  # noqa: E402
from integration_config import missing_config_message  # noqa: E402
from integration_profiles import IntegrationProfileError, validate_profile_name  # noqa: E402


class Bis10ToolError(RuntimeError):
    """Erro seguro para apresentação ao chamador."""


@dataclass(frozen=True)
class Bis10Profile:
    name: str
    host: str
    port: int
    jar_path: Path
    working_dir: Path
    locale: str
    jndi_credential_ref: str
    bis_credential_ref: str


@dataclass(frozen=True)
class Bis10Config:
    java_executable: str
    timeout_seconds: int
    default_profile: str
    profiles: dict[str, Bis10Profile]

    def resolve_profile(self, requested: str | None) -> tuple[Bis10Profile, bool]:
        explicit = requested is not None
        name = validate_profile_name(requested or self.default_profile)
        profile = self.profiles.get(name)
        if profile is None:
            raise Bis10ToolError(f"Perfil BIS10 '{name}' não encontrado.")
        return profile, explicit


def _path(value: Any, field: str) -> Path:
    raw = str(value or "").strip()
    if not raw or any(char in raw for char in "\r\n\0"):
        raise Bis10ToolError(f"O campo '{field}' não pode ficar vazio.")
    path = Path(raw).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def load_config(path: Path = CONFIG_PATH) -> Bis10Config:
    try:
        values = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise Bis10ToolError(missing_config_message("bis10", path)) from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise Bis10ToolError(f"Não foi possível carregar a configuração BIS10 '{path}'.") from exc

    timeout = int(values.get("timeout_seconds", 300))
    if timeout <= 0:
        raise Bis10ToolError("'timeout_seconds' deve ser positivo.")
    default = validate_profile_name(str(values.get("default_profile", "")).strip())
    raw_profiles = values.get("profiles")
    if not isinstance(raw_profiles, dict) or not raw_profiles:
        raise Bis10ToolError("'profiles' deve conter ao menos um perfil.")
    profiles: dict[str, Bis10Profile] = {}
    for raw_name, raw in raw_profiles.items():
        name = validate_profile_name(str(raw_name))
        if not isinstance(raw, dict):
            raise Bis10ToolError(f"'profiles.{name}' deve ser uma tabela.")
        host = str(raw.get("host", "")).strip()
        if not host:
            raise Bis10ToolError(f"'profiles.{name}.host' não pode ficar vazio.")
        try:
            port = int(raw.get("port", 0))
        except (TypeError, ValueError) as exc:
            raise Bis10ToolError(f"'profiles.{name}.port' deve ser inteiro.") from exc
        if not 1 <= port <= 65535:
            raise Bis10ToolError(f"'profiles.{name}.port' deve estar entre 1 e 65535.")
        locale = str(raw.get("locale", "pt-BR")).strip()
        if not re.fullmatch(r"[A-Za-z]{2,8}(?:[-_][A-Za-z0-9]{2,8})?", locale):
            raise Bis10ToolError(f"'profiles.{name}.locale' é inválido.")
        jndi_ref = str(raw.get("jndi_credential_ref", "")).strip()
        bis_ref = str(raw.get("bis_credential_ref", "")).strip()
        if not jndi_ref or not bis_ref:
            raise Bis10ToolError(f"O perfil '{name}' exige as duas referências de credencial.")
        profiles[name] = Bis10Profile(
            name, host, port, _path(raw.get("jar_path"), "jar_path"),
            _path(raw.get("working_dir"), "working_dir"), locale, jndi_ref, bis_ref,
        )
    if default not in profiles:
        raise Bis10ToolError(f"O perfil padrão BIS10 '{default}' não existe.")
    return Bis10Config(str(values.get("java_executable", "")).strip(), timeout, default, profiles)


def _java(config: Bis10Config) -> str:
    if config.java_executable:
        path = _path(config.java_executable, "java_executable")
        if not path.is_file():
            raise Bis10ToolError(f"Java configurado não encontrado em '{path}'.")
        return str(path)
    found = shutil.which("java")
    if not found:
        raise Bis10ToolError("Java não encontrado no PATH.")
    return found


def _sanitize(value: str, secrets: tuple[str, ...]) -> str:
    result = value
    for secret in secrets:
        if secret:
            result = result.replace(secret, "[Censurado por segurança]")
    result = re.sub(r"(?i)(biscmd\.(?:jndi\.|bis\.)?password\s*[=:]\s*)[^\s]+", r"\1[Censurado por segurança]", result)
    return result


def run_bis10cmd(config: Bis10Config, profile: Bis10Profile, arguments: list[str]) -> dict[str, Any]:
    if not profile.jar_path.is_file():
        raise Bis10ToolError(f"JAR do BIS10CMD não encontrado em '{profile.jar_path}'.")
    if not profile.working_dir.is_dir():
        raise Bis10ToolError(f"Diretório do BIS10CMD não encontrado em '{profile.working_dir}'.")
    jndi_user, jndi_password = read_entry_credentials(profile.jndi_credential_ref)
    bis_user, bis_password = read_entry_credentials(profile.bis_credential_ref)
    env = os.environ.copy()
    env.update({
        "BISCMD_HOST": profile.host, "BISCMD_PORT": str(profile.port),
        "BISCMD_LOCALE": profile.locale,
        "BISCMD_JNDI_USER": jndi_user, "BISCMD_JNDI_PASSWORD": jndi_password,
        "BISCMD_BIS_USER": bis_user, "BISCMD_BIS_PASSWORD": bis_password,
    })
    command = [_java(config), f"-Dbiscmd.config={profile.working_dir / 'application.properties'}",
               "-jar", str(profile.jar_path), *arguments]
    try:
        completed = subprocess.run(command, cwd=str(profile.working_dir), env=env,
                                   capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", check=False, timeout=config.timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        raise Bis10ToolError("O BIS10CMD excedeu o tempo limite configurado.") from exc
    finally:
        for key in ("BISCMD_JNDI_PASSWORD", "BISCMD_BIS_PASSWORD"):
            env[key] = ""
    secrets = (jndi_user, jndi_password, bis_user, bis_password)
    return {"ok": completed.returncode == 0, "profile": profile.name, "host": profile.host,
            "port": profile.port, "returncode": completed.returncode,
            "stdout": _sanitize(completed.stdout, secrets),
            "stderr": _sanitize(completed.stderr, secrets)}


def _positive_decimal(value: str) -> str:
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("O valor deve ser decimal.") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("O valor deve ser positivo.")
    return value


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--profile")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Executa operações permitidas do BIS10 via BIS10CMD.")
    parser.add_argument("--profile")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("doctor", "ping", "session", "help"):
        commands.add_parser(name)

    create = commands.add_parser("account-create")
    create.add_argument("--account-id", type=int, required=True); create.add_argument("--category-id", type=int, required=True)
    create.add_argument("--date", required=True); create.add_argument("--value", required=True, type=_positive_decimal)
    create.add_argument("--display-line"); create.add_argument("--notes"); create.add_argument("--audited", choices=("true", "false"), default="false")
    create.add_argument("--confirm", action="store_true", required=True)

    transfer = commands.add_parser("transfer-create")
    transfer.add_argument("--debit-account-id", type=int, required=True); transfer.add_argument("--credit-account-id", type=int, required=True)
    transfer.add_argument("--date", required=True); transfer.add_argument("--value", required=True, type=_positive_decimal)
    transfer.add_argument("--display-line"); transfer.add_argument("--notes"); transfer.add_argument("--audited", choices=("true", "false"), default="false")
    transfer.add_argument("--confirm", action="store_true", required=True)

    for name, transfer_mode in (("account-update", False), ("transfer-update", True)):
        item = commands.add_parser(name); item.add_argument("--id", type=int, required=True)
        if transfer_mode:
            item.add_argument("--debit-account-id", type=int); item.add_argument("--credit-account-id", type=int)
        else:
            item.add_argument("--account-id", type=int); item.add_argument("--category-id", type=int)
        item.add_argument("--date"); item.add_argument("--value", type=_positive_decimal); item.add_argument("--display-line"); item.add_argument("--notes")
        item.add_argument("--audited", choices=("true", "false")); item.add_argument("--confirm", action="store_true", required=True)
    delete = commands.add_parser("account-delete"); delete.add_argument("--id", type=int, required=True); delete.add_argument("--confirm", action="store_true", required=True)
    return parser


def build_arguments(args: argparse.Namespace) -> tuple[list[str], bool]:
    command = args.command
    if command == "help": return ["-h"], False
    if command == "doctor": return ["-connect", "-ping"], False
    if command == "ping": return ["-connect", "-ping"], False
    if command == "session": return ["-connect", "-session"], False
    mutating = True
    if command == "account-create":
        values = [("accountId", args.account_id), ("categoryId", args.category_id), ("date", args.date), ("value", args.value), ("displayLine", args.display_line), ("notes", args.notes), ("audited", args.audited)]
        action = "create"
    elif command == "transfer-create":
        values = [("debitAccountId", args.debit_account_id), ("creditAccountId", args.credit_account_id), ("date", args.date), ("value", args.value), ("displayLine", args.display_line), ("notes", args.notes), ("audited", args.audited)]
        action = "createTransfer"
    elif command in {"account-update", "transfer-update"}:
        fields = [("id", args.id)]
        if command == "account-update": fields += [("accountId", args.account_id), ("categoryId", args.category_id)]; action = "update"
        else: fields += [("debitAccountId", args.debit_account_id), ("creditAccountId", args.credit_account_id)]; action = "updateTransfer"
        values = fields + [("date", args.date), ("value", args.value), ("displayLine", args.display_line), ("notes", args.notes), ("audited", args.audited)]
    else:
        return ["-connect", "-accountStatement", "delete", "id", str(args.id), "confirm"], True
    result = ["-connect", "-accountStatement", action]
    for key, value in values:
        if value is not None: result += [key, str(value)]
    result.append("confirm")
    return result, mutating


def main() -> int:
    try:
        args = build_parser().parse_args()
        config = load_config(); profile, explicit = config.resolve_profile(args.profile)
        arguments, mutating = build_arguments(args)
        if mutating and not explicit:
            raise Bis10ToolError("Operações de escrita exigem --profile explícito.")
        result = run_bis10cmd(config, profile, arguments)
    except (Bis10ToolError, IntegrationProfileError, ValueError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc), "error_type": type(exc).__name__}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
