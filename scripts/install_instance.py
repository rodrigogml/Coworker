#!/usr/bin/env python3
"""Inicializa uma instância local do Coworker sem configurar suas skills."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
IDENTITY_CONFIG = DATA_DIR / "config" / "identity.toml"
TELEGRAM_CONFIG = DATA_DIR / "config" / "telegram.toml"
SECRETS_CONFIG = DATA_DIR / "config" / "secrets.toml"
GATEWAY = PROJECT_ROOT / "interfaces" / "telegram" / "gateway.py"
VAULT_TOOL = PROJECT_ROOT / "scripts" / "credential_vault.py"
MEMORY_TOOL = PROJECT_ROOT / "scripts" / "memory.py"

IDENTITY_FIELDS = (
    ("instance_id", "Identificador técnico"),
    ("display_name", "Nome público da instância"),
    ("language", "Idioma principal"),
    ("grammatical_gender", "Gênero gramatical"),
    ("pronouns", "Pronomes"),
    ("summary", "Descrição curta"),
    ("tone", "Tom"),
    ("humor", "Humor"),
    ("enthusiasm", "Empolgação"),
    ("writing_style", "Estilo de escrita"),
    ("bio", "Bio da personalidade"),
)


class InstallError(RuntimeError):
    """Indica que a instalação não pode prosseguir com segurança."""


def _slug(value: str) -> str:
    normalized = value.casefold()
    normalized = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")
    if not normalized:
        raise InstallError("Não foi possível derivar um identificador do nome informado.")
    return normalized[:64].rstrip("-")


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _ask(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    answer = input(f"{label}{suffix}: ").strip()
    return answer or default


def _yes_no(label: str, *, default: bool) -> bool:
    marker = "S/n" if default else "s/N"
    answer = input(f"{label} [{marker}]: ").strip().casefold()
    if not answer:
        return default
    return answer in {"s", "sim", "y", "yes"}


def _write_new(path: Path, content: str) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
    except FileExistsError:
        return False
    return True


def _replace_config(path: Path, content: str) -> None:
    """Substitui uma configuração privada por gravação atômica em UTF-8."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8", newline="\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _identity_content(values: dict[str, Any]) -> str:
    fields = (
        ("instance_id", values["instance_id"]),
        ("display_name", values["display_name"]),
        ("language", values["language"]),
        ("grammatical_gender", values["grammatical_gender"]),
        ("pronouns", values["pronouns"]),
    )
    lines = ["# Identidade privada desta instância.", "", "[identity]"]
    lines.extend(f"{key} = {_toml_string(str(value))}" for key, value in fields)
    lines.extend(
        (
            f"summary = {_toml_string(str(values['summary']))}",
            f"tone = {_toml_string(str(values['tone']))}",
            f"humor = {_toml_string(str(values['humor']))}",
            f"enthusiasm = {_toml_string(str(values['enthusiasm']))}",
            f"writing_style = {_toml_string(str(values['writing_style']))}",
            f"bio = {_toml_string(str(values['bio']))}",
            "",
        )
    )
    return "\n".join(lines)


def collect_identity() -> dict[str, Any]:
    display_name = _ask("Nome público da instância")
    if not display_name:
        raise InstallError("O nome público é obrigatório.")
    instance_id = _ask("Identificador técnico", _slug(display_name))
    grammatical_gender = _ask(
        "Gênero gramatical (feminine, masculine ou neutral)", "neutral"
    ).casefold()
    if grammatical_gender not in {"feminine", "masculine", "neutral"}:
        raise InstallError("O gênero gramatical deve ser feminine, masculine ou neutral.")
    default_pronouns = {
        "feminine": "ela/dela",
        "masculine": "ele/dele",
        "neutral": "",
    }[grammatical_gender]
    values: dict[str, Any] = {
        "instance_id": _slug(instance_id),
        "display_name": display_name,
        "language": _ask("Idioma principal", "pt-BR"),
        "grammatical_gender": grammatical_gender,
        "pronouns": _ask("Pronomes", default_pronouns),
        "summary": _ask(
            "Descrição curta",
            "Assistente pessoal local, organizada e prestativa.",
        ),
        "tone": _ask("Tom", "claro, cordial e direto"),
        "humor": _ask("Humor", "leve e ocasional"),
        "enthusiasm": _ask("Empolgação", "moderada"),
        "writing_style": _ask("Estilo de escrita", "prático e conciso"),
        "bio": _ask("Bio da personalidade"),
    }
    if not values["bio"]:
        raise InstallError("A bio é obrigatória.")
    return values


def edit_identity(
    values: dict[str, Any], *, allow_instance_id_change: bool = True
) -> dict[str, Any]:
    """Exibe a identidade existente e permite alterar um campo por vez."""
    updated = dict(values)
    while True:
        print("\nIdentidade configurada:")
        for index, (key, label) in enumerate(IDENTITY_FIELDS, start=1):
            value = str(updated.get(key, "")) or "(não informado)"
            print(f"  {index:>2}. {label}: {value}")
        answer = input(
            "Escolha o número do campo para alterar ou pressione Enter para continuar: "
        ).strip()
        if not answer or answer == "0":
            return updated
        try:
            key, label = IDENTITY_FIELDS[int(answer) - 1]
        except (ValueError, IndexError):
            print("Escolha um número válido da lista.")
            continue
        current = str(updated.get(key, ""))
        value = _ask(label, current)
        if key == "instance_id":
            if not allow_instance_id_change:
                print(
                    "O identificador técnico não pode ser alterado depois que o "
                    "cofre ou o Telegram foram configurados."
                )
                continue
            value = _slug(value)
        elif key == "grammatical_gender":
            value = value.casefold()
            if value not in {"feminine", "masculine", "neutral"}:
                print("Use feminine, masculine ou neutral.")
                continue
        elif key != "pronouns" and not value:
            print("Esse campo não pode ficar vazio.")
            continue
        updated[key] = value


def _load_identity_values() -> dict[str, Any]:
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from interfaces.telegram.identity import load_identity

    identity = load_identity(IDENTITY_CONFIG)
    return {key: getattr(identity, key) for key, _label in IDENTITY_FIELDS}


def _telegram_content(instance_id: str) -> str:
    executable = shutil.which("codex") or shutil.which("codex.exe") or ""
    executable_value = str(Path(executable).resolve()) if executable else ""
    credential_ref = f"APIs/Telegram/{instance_id}"
    webhook_ref = f"APIs/Telegram/{instance_id}-webhook"
    return f'''# Configuração privada da interface Telegram.
transport = "polling"
credential_ref = {_toml_string(credential_ref)}
project_root = "."
state_dir = ""
poll_timeout_seconds = 45
request_timeout_seconds = 60

[pairing]
ttl_seconds = 600
max_attempts = 5

[codex]
executable = {_toml_string(executable_value)}
home_dir = ""
backend = "app-server"
sandbox = "workspace-write"
network_access = false
approval_policy = "never"
timeout_seconds = 1800
additional_directories = []
writable_directories = ["data"]

[media]
inbox_dir = "data/telegram/inbox"
jobs_dir = "data/telegram/jobs"
max_download_bytes = 20971520
max_upload_bytes = 20971520

[processors]
max_extracted_characters = 200000
max_archive_members = 500
max_uncompressed_bytes = 104857600
max_pages = 200
max_duration_seconds = 3600
max_frames = 100

[webhook]
public_url = ""
secret_credential_ref = {_toml_string(webhook_ref)}
listen_host = "127.0.0.1"
listen_port = 8787
'''


def _secrets_content(
    instance_id: str,
    *,
    gui: str = "",
    cli: str = "",
    vault_path: str = "data/secrets/vault.kdbx",
    credential_target: str | None = None,
) -> str:
    target = credential_target or (
        f"Coworker/Instances/{instance_id}/KeePassXC/MasterPassword"
    )
    return f'''# Configuração privada do cofre desta instância.
[executables]
gui = {_toml_string(gui)}
cli = {_toml_string(cli)}

[vault]
path = {_toml_string(vault_path)}

[windows_credential]
target = {_toml_string(target)}
'''


def _load_secrets_values(instance_id: str) -> dict[str, str]:
    defaults = {
        "gui": "",
        "cli": "",
        "vault_path": "data/secrets/vault.kdbx",
        "credential_target": (
            f"Coworker/Instances/{instance_id}/KeePassXC/MasterPassword"
        ),
    }
    if not SECRETS_CONFIG.is_file():
        return defaults
    try:
        with SECRETS_CONFIG.open("rb") as stream:
            root = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise InstallError(
            f"Não foi possível ler a configuração '{SECRETS_CONFIG}'."
        ) from exc
    executables = root.get("executables", {})
    vault = root.get("vault", {})
    credential = root.get("windows_credential", {})
    if isinstance(executables, dict):
        defaults["gui"] = str(executables.get("gui", "")).strip()
        defaults["cli"] = str(executables.get("cli", "")).strip()
    if isinstance(vault, dict):
        defaults["vault_path"] = str(
            vault.get("path", defaults["vault_path"])
        ).strip()
    if isinstance(credential, dict):
        defaults["credential_target"] = str(
            credential.get("target", defaults["credential_target"])
        ).strip()
    return defaults


def _known_executable_paths(filename: str) -> tuple[Path, ...]:
    candidates: list[Path] = []
    from_path = shutil.which(filename)
    if from_path:
        candidates.append(Path(from_path))
    for environment_name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(environment_name)
        if not base:
            continue
        root = Path(base)
        candidates.extend(
            (
                root / "KeePassXC" / filename,
                root / "Programs" / "KeePassXC" / filename,
            )
        )
    return tuple(candidates)


def _discover_executable(configured: str, filename: str) -> Path | None:
    candidates = [Path(configured).expanduser()] if configured else []
    candidates.extend(_known_executable_paths(filename))
    for candidate in candidates:
        resolved = candidate if candidate.is_absolute() else PROJECT_ROOT / candidate
        if resolved.is_file():
            return resolved.resolve()
    return None


def _prompt_executable(label: str, filename: str) -> Path | None:
    while True:
        answer = input(
            f"{label} não foi localizado. Informe o caminho de {filename} "
            "ou pressione Enter para configurar depois: "
        ).strip().strip('"')
        if not answer:
            return None
        candidate = Path(answer).expanduser()
        if not candidate.is_absolute():
            candidate = PROJECT_ROOT / candidate
        if candidate.is_file():
            return candidate.resolve()
        print(f"Arquivo não encontrado: {candidate}")


def edit_vault_executables(values: dict[str, str]) -> dict[str, str]:
    """Permite revisar os caminhos locais do KeePassXC sem tocar em segredos."""
    updated = dict(values)
    fields = (
        ("gui", "KeePassXC.exe"),
        ("cli", "keepassxc-cli.exe"),
    )
    while True:
        print("\nExecutáveis do cofre:")
        for index, (key, label) in enumerate(fields, start=1):
            print(f"  {index}. {label}: {updated.get(key) or '(não localizado)'}")
        answer = input(
            "Escolha o número do caminho para alterar ou pressione Enter para continuar: "
        ).strip()
        if not answer or answer == "0":
            return updated
        try:
            key, label = fields[int(answer) - 1]
        except (ValueError, IndexError):
            print("Escolha um número válido da lista.")
            continue
        updated[key] = _ask(f"Caminho de {label}", updated.get(key, ""))


def configure_vault_executables(
    instance_id: str,
    *,
    non_interactive: bool,
    previous_instance_id: str | None = None,
) -> tuple[bool, bool]:
    """Detecta ou solicita os executáveis e persiste somente caminhos verificados."""
    existed = SECRETS_CONFIG.is_file()
    values = _load_secrets_values(instance_id)
    if previous_instance_id and previous_instance_id != instance_id:
        previous_target = (
            f"Coworker/Instances/{previous_instance_id}/KeePassXC/MasterPassword"
        )
        if values["credential_target"] == previous_target:
            values["credential_target"] = (
                f"Coworker/Instances/{instance_id}/KeePassXC/MasterPassword"
            )
    if existed and not non_interactive:
        values = edit_vault_executables(values)
    gui = _discover_executable(values["gui"], "KeePassXC.exe")
    cli = _discover_executable(values["cli"], "keepassxc-cli.exe")
    if gui is not None and cli is None:
        sibling = str(gui.with_name("keepassxc-cli.exe"))
        cli = _discover_executable(sibling, "keepassxc-cli.exe")
    if cli is not None and gui is None:
        sibling = str(cli.with_name("KeePassXC.exe"))
        gui = _discover_executable(sibling, "KeePassXC.exe")
    if not non_interactive:
        gui = gui or _prompt_executable("KeePassXC", "KeePassXC.exe")
        if gui is not None and cli is None:
            sibling = str(gui.with_name("keepassxc-cli.exe"))
            cli = _discover_executable(sibling, "keepassxc-cli.exe")
        cli = cli or _prompt_executable("KeePassXC CLI", "keepassxc-cli.exe")
    content = _secrets_content(
        instance_id,
        gui=str(gui or ""),
        cli=str(cli or ""),
        vault_path=values["vault_path"] or "data/secrets/vault.kdbx",
        credential_target=values["credential_target"] or None,
    )
    if existed:
        _replace_config(SECRETS_CONFIG, content)
    else:
        _write_new(SECRETS_CONFIG, content)
    ready = gui is not None and cli is not None
    if not ready:
        print(
            "KeePassXC ficou pendente. Execute novamente o configurador após "
            "instalá-lo ou informe os executáveis na próxima execução."
        )
    return not existed, ready


def _run_json(command: list[str], *, timeout: int = 120) -> dict[str, Any]:
    completed = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        shell=False,
    )
    output = completed.stdout.strip() or completed.stderr.strip()
    try:
        result = json.loads(output)
    except json.JSONDecodeError as exc:
        raise InstallError("Uma ferramenta de instalação devolveu uma resposta inválida.") from exc
    if completed.returncode != 0 or result.get("ok") is False:
        raise InstallError(str(result.get("error") or "Uma etapa da instalação falhou."))
    return result


def _gateway(*arguments: str, timeout: int = 120) -> dict[str, Any]:
    return _run_json([sys.executable, str(GATEWAY), *arguments], timeout=timeout)


def configure_vault(*, non_interactive: bool) -> bool:
    """Prepara o cofre sem receber senha ou segredo pelo processo instalador."""
    vault = DATA_DIR / "secrets" / "vault.kdbx"
    if non_interactive:
        return vault.is_file()
    if not vault.is_file():
        print("\nO cofre ainda não existe. A senha será solicitada em uma janela separada.")
        completed = subprocess.run(
            [sys.executable, str(VAULT_TOOL), "create"],
            cwd=PROJECT_ROOT,
            check=False,
            shell=False,
        )
        if completed.returncode != 0:
            raise InstallError("Não foi possível iniciar a criação do cofre.")
        input("Conclua a criação na janela segura e pressione Enter aqui para continuar.")
        if not vault.is_file():
            raise InstallError("O arquivo do cofre não foi criado.")
    enrolled = False
    if os.name == "nt" and vault.is_file():
        try:
            status = _run_json(
                [sys.executable, str(VAULT_TOOL), "status"], timeout=30
            )
            enrolled = bool(status.get("master_password_enrolled"))
        except InstallError:
            enrolled = False
    if os.name == "nt" and not enrolled and _yes_no(
        "Cadastrar o desbloqueio deste cofre no Windows", default=True
    ):
        completed = subprocess.run(
            [sys.executable, str(VAULT_TOOL), "enroll"],
            cwd=PROJECT_ROOT,
            check=False,
            shell=False,
        )
        if completed.returncode != 0:
            raise InstallError("Não foi possível iniciar o cadastro local do cofre.")
        input("Conclua o cadastro na janela segura e pressione Enter aqui para continuar.")
    return True


def _gateway_process() -> subprocess.Popen[bytes]:
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    return subprocess.Popen(
        [sys.executable, str(GATEWAY), "run"],
        cwd=PROJECT_ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
        creationflags=flags,
        start_new_session=os.name != "nt",
    )


def _stop_gateway(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def pair_owner_interactively() -> bool:
    status = _gateway("pairing", "status")
    if status.get("owner"):
        print("A pessoa proprietária já está vinculada.")
        return True
    pairing = _gateway("pairing", "begin")
    pin = str(pairing.get("pin") or "")
    if not pin:
        raise InstallError("O gateway não devolveu o PIN de pareamento.")
    print(f"\nEnvie /pair {pin} na conversa particular com o bot.")
    print("Aguardando a solicitação de vínculo; pressione Ctrl+C para cancelar.")
    process = _gateway_process()
    pending: dict[str, Any] | None = None
    try:
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise InstallError("O gateway encerrou enquanto aguardava o pareamento.")
            current = _gateway("pairing", "status", timeout=30)
            value = current.get("pending")
            if isinstance(value, dict):
                pending = value
                break
            time.sleep(2)
    except KeyboardInterrupt as exc:
        _gateway("pairing", "cancel")
        raise InstallError("O pareamento foi cancelado localmente.") from exc
    finally:
        _stop_gateway(process)
    if pending is None:
        _gateway("pairing", "cancel")
        raise InstallError("O tempo para pareamento expirou.")
    print("\nSolicitação recebida:")
    print(f"  Nome: {pending.get('display_name') or 'não informado'}")
    print(f"  Username: @{pending.get('username') or 'não informado'}")
    print(f"  user_id: {pending.get('user_id')}")
    print(f"  chat_id: {pending.get('chat_id')}")
    if not _yes_no("Esses IDs pertencem à pessoa proprietária", default=False):
        _gateway("pairing", "cancel")
        raise InstallError("A solicitação de vínculo não foi aprovada.")
    approval_code = str(pending.get("approval_code") or "")
    if not approval_code:
        raise InstallError("A solicitação não possui código local de aprovação.")
    _gateway("pairing", "approve", approval_code)
    return True


def configure_telegram(
    instance_id: str, *, non_interactive: bool, start_gateway: bool
) -> dict[str, Any]:
    credential_ref = f"APIs/Telegram/{instance_id}"
    if non_interactive:
        print("Telegram preparado, mas token, perfil e pareamento exigem interação local.")
        return {"configured": False, "paired": False, "process_id": None}
    print("\nA criação do bot e do username continua sendo feita no BotFather.")
    print("O instalador sincronizará nome e descrições usando a identidade local.")
    if _yes_no(f"Salvar agora o token em {credential_ref}", default=True):
        token = getpass.getpass("Token do bot Telegram (entrada mascarada): ").strip()
        if not token:
            raise InstallError("O token do Telegram não pode ficar vazio.")
        try:
            if str(PROJECT_ROOT) not in sys.path:
                sys.path.insert(0, str(PROJECT_ROOT))
            from scripts.credential_vault import VaultToolError, write_entry_secret

            write_entry_secret(credential_ref, token)
        except VaultToolError as exc:
            raise InstallError("O token do Telegram não foi salvo no cofre.") from exc
        finally:
            token = ""
        print("Token salvo diretamente no cofre.")
        _gateway("profile", "sync")
        _gateway("commands", "sync")
        _gateway("permissions", "sync")
        print("Nome, bio, comandos e sandbox do bot foram sincronizados.")
    paired = pair_owner_interactively()
    process_id = None
    if start_gateway:
        process = _gateway_process()
        time.sleep(2)
        if process.poll() is not None:
            raise InstallError("O gateway encerrou durante a inicialização final.")
        process_id = process.pid
        print(f"Gateway iniciado em segundo plano (PID {process_id}).")
    return {"configured": True, "paired": paired, "process_id": process_id}


def install(args: argparse.Namespace) -> dict[str, Any]:
    for relative in (
        "config",
        "memory",
        "secrets",
        "telegram/inbox",
        "telegram/jobs",
    ):
        (DATA_DIR / relative).mkdir(parents=True, exist_ok=True)
    identity_created = False
    previous_instance_id: str | None = None
    if IDENTITY_CONFIG.exists():
        identity_values = _load_identity_values()
        previous_instance_id = str(identity_values["instance_id"])
        if not args.non_interactive:
            vault_exists = (DATA_DIR / "secrets" / "vault.kdbx").is_file()
            edited_values = edit_identity(
                identity_values,
                allow_instance_id_change=(
                    not vault_exists and not TELEGRAM_CONFIG.is_file()
                ),
            )
            if edited_values != identity_values:
                _replace_config(IDENTITY_CONFIG, _identity_content(edited_values))
                identity_values = edited_values
    elif args.non_interactive:
        raise InstallError(
            "data/config/identity.toml é obrigatório no modo não interativo."
        )
    else:
        collected = collect_identity()
        identity_created = _write_new(IDENTITY_CONFIG, _identity_content(collected))
        identity_values = {
            "instance_id": collected["instance_id"],
            "display_name": collected["display_name"],
        }
    instance_id = str(identity_values["instance_id"])
    secrets_created, vault_tools_ready = configure_vault_executables(
        instance_id,
        non_interactive=args.non_interactive,
        previous_instance_id=previous_instance_id,
    )
    vault_ready = vault_tools_ready and configure_vault(
        non_interactive=args.non_interactive
    )
    memory_path = DATA_DIR / "memory.sqlite3"
    memory = (
        {"ok": True, "existing": True}
        if memory_path.is_file()
        else _run_json([sys.executable, str(MEMORY_TOOL), "init"])
    )
    telegram_created = False
    telegram_result: dict[str, Any] | None = None
    if not args.skip_telegram:
        telegram_created = _write_new(TELEGRAM_CONFIG, _telegram_content(instance_id))
        if vault_ready:
            telegram_result = configure_telegram(
                instance_id,
                non_interactive=args.non_interactive,
                start_gateway=not args.no_start,
            )
        else:
            print(
                "Telegram preparado, mas token e pareamento ficaram pendentes até "
                "a configuração do cofre."
            )
            telegram_result = {
                "configured": False,
                "paired": False,
                "process_id": None,
                "pending": "vault",
            }
    return {
        "ok": True,
        "instance_id": instance_id,
        "display_name": identity_values["display_name"],
        "identity_created": identity_created,
        "secrets_config_created": secrets_created,
        "vault_ready": vault_ready,
        "memory_initialized": bool(memory.get("ok", True)),
        "telegram_config_created": telegram_created,
        "telegram": telegram_result,
        "skills_configured": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepara identidade, cofre, Telegram e sandbox de uma instância."
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Não solicita dados; exige uma identidade privada já existente.",
    )
    parser.add_argument(
        "--skip-telegram",
        action="store_true",
        help="Não cria nem configura a interface Telegram.",
    )
    parser.add_argument(
        "--no-start",
        action="store_true",
        help="Configura e vincula o Telegram sem manter o gateway em execução.",
    )
    return parser


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if sys.version_info < (3, 11):
        print(
            json.dumps(
                {"ok": False, "error": "Python 3.11 ou superior é obrigatório."},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    try:
        result = install(build_parser().parse_args())
    except (InstallError, OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
