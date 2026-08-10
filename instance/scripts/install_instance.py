#!/usr/bin/env python3
"""Configura e valida uma instância local do Coworker sem configurar suas skills."""

from __future__ import annotations

import argparse
import csv
import getpass
import importlib.util
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
INSTRUCTIONS_CONFIG = DATA_DIR / "config" / "INSTRUCTIONS.md"
INSTRUCTIONS_EXAMPLE = PROJECT_ROOT / "config" / "INSTRUCTIONS.example.md"
GATEWAY = PROJECT_ROOT / "interfaces" / "telegram" / "gateway.py"
VAULT_TOOL = PROJECT_ROOT / "scripts" / "credential_vault.py"
MEMORY_TOOL = PROJECT_ROOT / "scripts" / "memory.py"
BIS2_CONFIG = DATA_DIR / "config" / "bis2.toml"
BIS2_TOOL = PROJECT_ROOT / "skills" / "bis2" / "scripts" / "bis2.py"

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


def _toml_array(values: list[str] | tuple[str, ...]) -> str:
    return "[" + ", ".join(_toml_string(value) for value in values) + "]"


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


def _migrate_legacy_data() -> list[str]:
    """Copia dados do layout antigo sem sobrescrever a nova instância."""
    legacy_root = PROJECT_ROOT.parent / "data"
    if legacy_root.resolve() == DATA_DIR.resolve() or not legacy_root.is_dir():
        return []
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    migrated: list[str] = []
    for source in legacy_root.iterdir():
        if source.name == ".gitkeep" or source.is_symlink():
            continue
        destination = DATA_DIR / source.name
        if destination.exists():
            continue
        if source.is_dir():
            shutil.copytree(source, destination)
        else:
            shutil.copy2(source, destination)
        migrated.append(source.name)
    return migrated


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


def _default_codex_home(instance_id: str) -> Path:
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from interfaces.telegram.config import default_codex_home

    return default_codex_home(instance_id).resolve()


def _default_telegram_values(instance_id: str) -> dict[str, Any]:
    executable = shutil.which("codex") or shutil.which("codex.exe") or ""
    return {
        "executable": str(Path(executable).resolve()) if executable else "",
        "home_dir": str(_default_codex_home(instance_id)),
        "backend": "app-server",
        "generated_images_dir": "",
        "access_mode": "restricted",
        "sandbox": "workspace-write",
        "network_access": False,
        "approval_policy": "never",
        "model": "",
        "reasoning_effort": "",
        "speed": "standard",
        "verbosity": "",
        "timeout_seconds": 1800,
        "additional_directories": [],
        "writable_directories": ["data/work"],
    }


def _load_telegram_values(instance_id: str) -> dict[str, Any]:
    values = _default_telegram_values(instance_id)
    if not TELEGRAM_CONFIG.is_file():
        return values
    try:
        with TELEGRAM_CONFIG.open("rb") as stream:
            root = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise InstallError(
            f"Não foi possível ler a configuração '{TELEGRAM_CONFIG}'."
        ) from exc
    codex = root.get("codex", {})
    if isinstance(codex, dict):
        for key in values:
            if key in codex:
                values[key] = codex[key]
    if not str(values.get("home_dir", "")).strip():
        values["home_dir"] = str(_default_codex_home(instance_id))
    values["writable_directories"] = _normalize_writable_directories(
        values.get("writable_directories", ["data/work"])
    )
    return values


def _portable_workspace_path(raw: Any) -> str | None:
    """Converte raízes de escrita antigas para o caminho relativo da instância.

    Configurações privadas podem ter sido criadas antes de a instância ser
    movida. Um absoluto que termine em ``data/work`` é seguro e deve virar um
    caminho relativo; absolutos fora dessa área não podem ser reaproveitados.
    """
    value = str(raw or "").strip().strip('"')
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        candidate = (PROJECT_ROOT / path).resolve()
    else:
        candidate = path.resolve()
    work_root = (DATA_DIR / "work").resolve()
    try:
        relative = candidate.relative_to(work_root)
    except ValueError:
        parts = [part.casefold() for part in candidate.parts]
        try:
            index = next(
                index
                for index in range(len(parts) - 1)
                if parts[index:index + 2] == ["data", "work"]
            )
        except StopIteration:
            return None
        relative = Path(*candidate.parts[index + 2:])
    return Path("data", "work", relative).as_posix()


def _normalize_writable_directories(raw: Any) -> list[str]:
    """Retorna somente raízes de escrita válidas, com fallback restrito."""
    if not isinstance(raw, list):
        return ["data/work"]
    normalized = [
        path
        for item in raw
        if (path := _portable_workspace_path(item)) is not None
    ]
    return normalized or ["data/work"]


def _telegram_content(
    instance_id: str, codex_values: dict[str, Any] | None = None
) -> str:
    settings = _default_telegram_values(instance_id)
    if codex_values:
        settings.update(codex_values)
    settings["writable_directories"] = _normalize_writable_directories(
        settings.get("writable_directories", ["data/work"])
    )
    credential_ref = f"APIs/Telegram/{instance_id}"
    webhook_ref = f"APIs/Telegram/{instance_id}-webhook"
    return f'''# Configuração privada da interface Telegram.
transport = "polling"
credential_ref = {_toml_string(credential_ref)}
project_root = "."
    state_dir = "data/telegram/state"
poll_timeout_seconds = 45
request_timeout_seconds = 60

[pairing]
ttl_seconds = 600
max_attempts = 5

[codex]
executable = {_toml_string(str(settings['executable']))}
home_dir = "data/codex"
backend = {_toml_string(str(settings['backend']))}
generated_images_dir = "data/codex/generated_images"
access_mode = {_toml_string(str(settings['access_mode']))}
sandbox = {_toml_string(str(settings['sandbox']))}
network_access = {str(bool(settings['network_access'])).lower()}
approval_policy = {_toml_string(str(settings['approval_policy']))}
model = {_toml_string(str(settings['model']))}
reasoning_effort = {_toml_string(str(settings['reasoning_effort']))}
speed = {_toml_string(str(settings['speed']))}
verbosity = {_toml_string(str(settings['verbosity']))}
timeout_seconds = {int(settings['timeout_seconds'])}
additional_directories = {_toml_array([str(value) for value in settings['additional_directories']])}
writable_directories = {_toml_array([str(value) for value in settings['writable_directories']])}

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

[processors.transcription]
enabled = false
backend = "cli"
auto_start = false
python_executable = ""
project_dir = ""
endpoint = "http://127.0.0.1:8870"
allow_remote = false
timeout_seconds = 120
language = "pt-BR"
profile = ""
model = "medium"
device = "cpu"
compute_type = "int8"
prompt = ""
terms = []
aliases = []
minimum_confidence = 0.55

[webhook]
public_url = ""
secret_credential_ref = {_toml_string(webhook_ref)}
listen_host = "127.0.0.1"
listen_port = 8787
'''


def _save_codex_values(instance_id: str, values: dict[str, Any]) -> None:
    """Atualiza somente [codex], preservando as demais opções privadas."""
    generated = _telegram_content(instance_id, values)
    codex_block = generated.split("[codex]\n", 1)[1].split("\n[media]", 1)[0]
    if not TELEGRAM_CONFIG.is_file():
        _write_new(TELEGRAM_CONFIG, generated)
        return
    current = TELEGRAM_CONFIG.read_text(encoding="utf-8")
    pattern = re.compile(r"(?ms)^\[codex\]\n.*?(?=^\[[^]]+\]\n|\Z)")
    replacement = f"[codex]\n{codex_block}\n"
    if pattern.search(current):
        updated = pattern.sub(lambda _match: replacement, current, count=1)
    else:
        updated = current.rstrip() + "\n\n" + replacement
    _replace_config(TELEGRAM_CONFIG, updated)


def _load_transcription_values() -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "enabled": False,
        "backend": "http",
        "auto_start": False,
        "python_executable": "C:/opt/EccoVox/.venv/Scripts/python.exe",
        "project_dir": "C:/opt/EccoVox",
        "endpoint": "http://127.0.0.1:8870",
        "allow_remote": False,
        "timeout_seconds": 120,
        "language": "pt-BR",
        "profile": "",
        "model": "medium",
        "device": "cuda",
        "compute_type": "int8_float16",
        "prompt": "Transcrição fiel em português do Brasil.",
        "terms": [],
        "aliases": [],
        "minimum_confidence": 0.55,
    }
    if not TELEGRAM_CONFIG.is_file():
        return defaults
    with TELEGRAM_CONFIG.open("rb") as stream:
        root = tomllib.load(stream)
    processors = root.get("processors", {})
    transcription = processors.get("transcription", {}) if isinstance(processors, dict) else {}
    if isinstance(transcription, dict):
        for key in defaults:
            if key in transcription:
                defaults[key] = transcription[key]
    return defaults


def _save_transcription_values(instance_id: str, values: dict[str, Any]) -> None:
    block = f'''[processors.transcription]
enabled = {str(bool(values['enabled'])).lower()}
backend = {_toml_string(str(values['backend']))}
auto_start = {str(bool(values['auto_start'])).lower()}
python_executable = {_toml_string(str(values['python_executable']))}
project_dir = {_toml_string(str(values['project_dir']))}
endpoint = {_toml_string(str(values['endpoint']))}
allow_remote = {str(bool(values['allow_remote'])).lower()}
timeout_seconds = {int(values['timeout_seconds'])}
language = {_toml_string(str(values['language']))}
profile = {_toml_string(str(values['profile']))}
model = {_toml_string(str(values['model']))}
device = {_toml_string(str(values['device']))}
compute_type = {_toml_string(str(values['compute_type']))}
prompt = {_toml_string(str(values['prompt']))}
terms = {_toml_array([str(value) for value in values['terms']])}
aliases = {_toml_array([str(value) for value in values['aliases']])}
minimum_confidence = {float(values['minimum_confidence']):.2f}
'''
    if not TELEGRAM_CONFIG.is_file():
        _write_new(TELEGRAM_CONFIG, _telegram_content(instance_id))
    current = TELEGRAM_CONFIG.read_text(encoding="utf-8")
    pattern = re.compile(r"(?ms)^\[processors\.transcription\]\n.*?(?=^\[[^]]+\]\n|\Z)")
    updated = pattern.sub(lambda _match: block + "\n", current, count=1)
    if updated == current:
        updated = current.rstrip() + "\n\n" + block
    _replace_config(TELEGRAM_CONFIG, updated)


def _default_bis2_values() -> dict[str, Any]:
    return {
        "java_executable": "",
        "jar_path": "C:/opt/BISCMD/BISCMD-9.0.jar",
        "working_dir": "C:/opt/BISCMD",
        "timeout_seconds": 300,
        "default_profile": "example",
        "profiles": {
            "example": {
                "host": "127.0.0.1",
                "port": 8080,
                "credential_ref": "BIS2/Example/BISCMD",
            }
        },
    }


def _bis2_content(values: dict[str, Any]) -> str:
    profiles = values.get("profiles", {})
    lines = [
        "java_executable = " + _toml_string(str(values.get("java_executable", ""))),
        "jar_path = " + _toml_string(str(values.get("jar_path", ""))),
        "working_dir = " + _toml_string(str(values.get("working_dir", ""))),
        f"timeout_seconds = {int(values.get('timeout_seconds', 300))}",
        "default_profile = " + _toml_string(str(values.get("default_profile", ""))),
        "",
    ]
    for name in sorted(profiles):
        profile = profiles[name]
        lines.extend(
            (
                f"[profiles.{name}]",
                "host = " + _toml_string(str(profile.get("host", ""))),
                f"port = {int(profile.get('port', 8080))}",
                "credential_ref = " + _toml_string(
                    str(profile.get("credential_ref", ""))
                ),
                "",
            )
        )
    return "\n".join(lines)


def _load_bis2_values() -> dict[str, Any]:
    values = _default_bis2_values()
    if not BIS2_CONFIG.is_file():
        return values
    try:
        with BIS2_CONFIG.open("rb") as stream:
            root = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise InstallError(f"Não foi possível ler a configuração '{BIS2_CONFIG}'.") from exc
    for key in ("java_executable", "jar_path", "working_dir", "timeout_seconds", "default_profile"):
        if key in root:
            values[key] = root[key]
    profiles = root.get("profiles")
    if isinstance(profiles, dict) and profiles:
        values["profiles"] = {
            str(name): {
                "host": str(profile.get("host", "")),
                "port": int(profile.get("port", 8080)),
                "credential_ref": str(profile.get("credential_ref", "")),
            }
            for name, profile in profiles.items()
            if isinstance(profile, dict)
        }
    return values


def _save_bis2_values(values: dict[str, Any]) -> None:
    _replace_config(BIS2_CONFIG, _bis2_content(values))


def _default_bis2_credential_ref(profile_name: str) -> str:
    label = re.sub(r"[^A-Za-z0-9]+", "", profile_name.title()) or profile_name
    return f"BIS2/{label}/BISCMD"


def _configure_bis2_profile(values: dict[str, Any]) -> bool:
    profiles = dict(values.get("profiles", {}))
    print("\nPerfis BIS2:")
    for name in sorted(profiles):
        profile = profiles[name]
        print(
            f"  - {name}: {profile.get('host')}:{profile.get('port')} "
            f"({profile.get('credential_ref')})"
        )
    name = _ask("Nome do perfil", str(values.get("default_profile", "example")))
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name):
        raise InstallError("O perfil deve usar de 1 a 64 letras, números, '_' ou '-'.")
    current = profiles.get(name, {})
    host = _ask("Host/IP do servidor BIS2", str(current.get("host", "127.0.0.1")))
    port = _ask("Porta HTTP remoting do WildFly", str(current.get("port", 8080)))
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        raise InstallError("A porta BIS2 deve estar entre 1 e 65535.")
    credential_ref = _ask(
        "Referência da credencial no KeePassXC",
        str(current.get("credential_ref") or _default_bis2_credential_ref(name)),
    )
    profiles[name] = {
        "host": host,
        "port": int(port),
        "credential_ref": credential_ref,
    }
    values["profiles"] = profiles
    if _yes_no(f"Usar {name} como perfil padrão", default=name == values.get("default_profile")):
        values["default_profile"] = name
    _save_bis2_values(values)
    print("Perfil BIS2 salvo.")
    if _yes_no(f"Salvar/atualizar a credencial {credential_ref} no cofre agora", default=False):
        if not _vault_operational(str(_load_identity_values()["instance_id"])):
            raise InstallError("Conclua primeiro a configuração do cofre KeePassXC.")
        username = _ask("Usuário WildFly ApplicationRealm", "biscmd")
        password = getpass.getpass("Senha do usuário WildFly (entrada mascarada): ")
        if not password:
            raise InstallError("A senha do BIS2 não pode ficar vazia.")
        try:
            from credential_vault import VaultToolError, write_entry_credentials

            write_entry_credentials(credential_ref, username, password)
        except VaultToolError as exc:
            raise InstallError("A credencial BIS2 não foi salva no cofre.") from exc
        finally:
            password = ""
        print("Credencial BIS2 salva diretamente no cofre.")
    return True


def configure_bis2(instance_id: str) -> None:
    values = _load_bis2_values()
    if not BIS2_CONFIG.is_file():
        _save_bis2_values(values)
        print("Configuração BIS2 criada em data/config/bis2.toml.")
    while True:
        profiles = values.get("profiles", {})
        print("\nBIS2 / BISCMD")
        print(f"  1. Java: {values.get('java_executable') or '(PATH)'}")
        print(f"  2. JAR: {values.get('jar_path')}")
        print(f"  3. Pasta de execução: {values.get('working_dir')}")
        print(f"  4. Timeout: {values.get('timeout_seconds')} segundos")
        print(f"  5. Perfil padrão: {values.get('default_profile')}")
        print(f"  6. Adicionar/alterar perfil ({len(profiles)} cadastrado(s))")
        print("  7. Testar conexão (doctor)")
        print("  0. Voltar")
        answer = input("Escolha uma opção: ").strip()
        if answer in {"", "0"}:
            return
        if answer == "1":
            values["java_executable"] = _ask("Caminho do java.exe vazio para PATH", str(values.get("java_executable", "")))
            _save_bis2_values(values)
        elif answer == "2":
            values["jar_path"] = _ask("Caminho do BISCMD.jar", str(values.get("jar_path", "")))
            _save_bis2_values(values)
        elif answer == "3":
            values["working_dir"] = _ask("Pasta de execução do BISCMD", str(values.get("working_dir", "")))
            _save_bis2_values(values)
        elif answer == "4":
            timeout = _ask("Timeout em segundos", str(values.get("timeout_seconds", 300)))
            if not timeout.isdigit() or int(timeout) <= 0:
                print("Informe um timeout positivo.")
                continue
            values["timeout_seconds"] = int(timeout)
            _save_bis2_values(values)
        elif answer == "5":
            profile = _ask("Perfil padrão", str(values.get("default_profile", "")))
            if profile not in profiles:
                print("Esse perfil não existe.")
                continue
            values["default_profile"] = profile
            _save_bis2_values(values)
        elif answer == "6":
            _configure_bis2_profile(values)
            values = _load_bis2_values()
        elif answer == "7":
            profile = _ask("Perfil para testar", str(values.get("default_profile", "")))
            result = _run_json(
                [sys.executable, str(BIS2_TOOL), "--profile", profile, "doctor"],
                timeout=int(values.get("timeout_seconds", 300)) + 30,
            )
            print("Conexão BIS2 OK." if result.get("ok") else "Conexão BIS2 falhou.")
        else:
            print("Escolha uma opção válida.")


def configure_skill_integrations(instance_id: str) -> None:
    while True:
        print("\nSkills e integrações")
        print("  1. BIS2 / BISCMD")
        print("  0. Voltar ao menu principal")
        answer = input("Escolha uma opção: ").strip()
        if answer in {"", "0"}:
            return
        if answer == "1":
            configure_bis2(instance_id)
        else:
            print("Escolha uma opção válida.")


def configure_transcription(instance_id: str) -> None:
    values = _load_transcription_values()
    changed = False
    while True:
        print("\nTranscrição EccoVox")
        print(f"  Estado: {'ATIVA' if values['enabled'] else 'DESATIVADA'}")
        print(f"  Endpoint: {values['endpoint']}")
        print(f"  Remoto permitido: {'sim' if values['allow_remote'] else 'não'}")
        print(f"  Início pelo gateway: {'sim' if values['auto_start'] else 'não'}")
        print(f"  Projeto local: {values['project_dir']}")
        print(f"  Modelo: {values['model']} / {values['device']} / {values['compute_type']}")
        print("  1. Ativar/desativar")
        print("  2. Definir host, porta e transporte")
        print("  3. Permitir/bloquear servidor remoto")
        print("  4. Iniciar localmente pelo gateway")
        print("  5. Definir instalação local")
        print("  6. Definir modelo e execução")
        print("  0. Salvar e voltar")
        answer = input("Escolha uma opção: ").strip()
        if answer in {"", "0"}:
            break
        if answer == "1":
            values["enabled"] = not bool(values["enabled"])
            changed = True
        elif answer == "2":
            from urllib.parse import urlparse
            current = urlparse(str(values["endpoint"]))
            host = _ask("Host do EccoVox", current.hostname or "127.0.0.1")
            port = _ask("Porta", str(current.port or 8870))
            scheme = _ask("Transporte (http ou https)", current.scheme or "http").casefold()
            if scheme not in {"http", "https"} or not port.isdigit() or not 1 <= int(port) <= 65535:
                print("Transporte ou porta inválidos.")
                continue
            values["endpoint"] = f"{scheme}://{host}:{int(port)}"
            changed = True
        elif answer == "3":
            allow = _yes_no("Permitir envio de áudio a um host remoto", default=False)
            if allow and not _yes_no("Confirmar que o endpoint remoto usa HTTPS e é confiável", default=False):
                print("Alteração cancelada.")
                continue
            values["allow_remote"] = allow
            if allow:
                values["auto_start"] = False
            changed = True
        elif answer == "4":
            values["auto_start"] = _yes_no("Gateway deve iniciar o EccoVox local", default=False)
            changed = True
        elif answer == "5":
            project = _ask("Pasta do EccoVox", str(values["project_dir"]))
            values["project_dir"] = project
            values["python_executable"] = str(Path(project) / ".venv" / "Scripts" / "python.exe")
            changed = True
        elif answer == "6":
            values["model"] = _ask("Modelo faster-whisper", str(values["model"]))
            values["device"] = _ask("Dispositivo (cpu ou cuda)", str(values["device"])).casefold()
            values["compute_type"] = _ask("Compute type", str(values["compute_type"])).casefold()
            changed = True
        else:
            print("Escolha uma opção válida.")
    if changed:
        _save_transcription_values(instance_id, values)
        if str(PROJECT_ROOT) not in sys.path:
            sys.path.insert(0, str(PROJECT_ROOT))
        from interfaces.telegram.config import load_config
        load_config(TELEGRAM_CONFIG, require_codex=False)
        print("Configuração de transcrição salva e validada.")


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
    if os.name == "nt":
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
    else:
        candidates.extend(
            Path(root) / filename
            for root in ("/usr/bin", "/usr/local/bin", "/snap/bin")
        )
    return tuple(candidates)


def _keepass_filenames() -> tuple[str, str]:
    return ("KeePassXC.exe", "keepassxc-cli.exe") if os.name == "nt" else (
        "keepassxc",
        "keepassxc-cli",
    )


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
    gui_filename, cli_filename = _keepass_filenames()
    fields = (("gui", gui_filename), ("cli", cli_filename))
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
    gui_filename, cli_filename = _keepass_filenames()
    gui = _discover_executable(values["gui"], gui_filename)
    cli = _discover_executable(values["cli"], cli_filename)
    if gui is not None and cli is None:
        sibling = str(gui.with_name(cli_filename))
        cli = _discover_executable(sibling, cli_filename)
    if cli is not None and gui is None:
        sibling = str(cli.with_name(gui_filename))
        gui = _discover_executable(sibling, gui_filename)
    if not non_interactive:
        gui = gui or _prompt_executable("KeePassXC", gui_filename)
        if gui is not None and cli is None:
            sibling = str(gui.with_name(cli_filename))
            cli = _discover_executable(sibling, cli_filename)
        cli = cli or _prompt_executable("KeePassXC CLI", cli_filename)
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
        raise InstallError(
            "Uma ferramenta de instalação devolveu uma resposta inválida."
        ) from exc
    if completed.returncode != 0 or result.get("ok") is False:
        raise InstallError(str(result.get("error") or "Uma etapa da instalação falhou."))
    return result


def _gateway(*arguments: str, timeout: int = 120) -> dict[str, Any]:
    return _run_json([sys.executable, str(GATEWAY), *arguments], timeout=timeout)


def windows_service_action(
    instance_id: str,
    action: str,
    *,
    service_name: str | None = None,
    display_name: str | None = None,
    startup: str = "automatic_delayed",
    account_mode: str = "current_user",
    non_interactive: bool = False,
) -> dict[str, Any]:
    """Executa uma operação do serviço Windows sem aceitar segredos em argumentos."""
    if os.name != "nt":
        raise InstallError("Serviços do Windows só podem ser administrados no Windows.")
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from scripts.windows_service import (
        WindowsServiceError,
        build_definition,
        control_service,
        install_service,
        remove_service,
        service_status,
    )

    name = service_name or instance_id
    try:
        if action == "status":
            return service_status(name)
        if action == "remove":
            return remove_service(name)
        if action in {"start", "stop"}:
            return control_service(name, action)
        if action != "install":
            raise InstallError(f"Ação de serviço desconhecida: {action}.")
        definition = build_definition(
            PROJECT_ROOT,
            instance_id=instance_id,
            display_name=display_name or instance_id,
            service_name=service_name,
            startup=startup,
            account_mode=account_mode,
        )
        return install_service(definition, non_interactive=non_interactive)
    except WindowsServiceError as exc:
        raise InstallError(str(exc)) from exc


def configure_vault(*, non_interactive: bool) -> bool:
    """Prepara o cofre sem receber senha ou segredo pelo processo instalador."""
    vault = DATA_DIR / "secrets" / "vault.kdbx"
    if non_interactive:
        return os.name == "nt" and vault.is_file()
    if not vault.is_file():
        if os.name == "nt":
            print("\nO cofre ainda não existe. A senha será solicitada em uma janela separada.")
            command = [sys.executable, str(VAULT_TOOL), "create"]
        else:
            values = _load_secrets_values(str(_load_identity_values()["instance_id"]))
            cli = _discover_executable(values["cli"], _keepass_filenames()[1])
            if cli is None:
                raise InstallError("O KeePassXC CLI precisa ser configurado antes do cofre.")
            print("\nO cofre ainda não existe. A senha será solicitada neste terminal.")
            command = [str(cli), "db-create", "--set-password", str(vault)]
        completed = subprocess.run(command, cwd=PROJECT_ROOT, check=False, shell=False)
        if completed.returncode != 0:
            raise InstallError("Não foi possível iniciar a criação do cofre.")
        if os.name == "nt":
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
    if os.name != "nt":
        print(
            "O cofre foi localizado, mas o desbloqueio automático para Telegram ainda "
            "não possui backend seguro implementado neste sistema."
        )
        return False
    return True


def _resolve_configured_path(raw: str) -> Path:
    path = Path(raw).expanduser()
    return path.resolve() if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def _discover_codex(configured: str = "") -> Path | None:
    candidates: list[Path] = []
    if configured.strip():
        candidates.append(_resolve_configured_path(configured.strip().strip('"')))
    discovered = shutil.which("codex") or shutil.which("codex.exe")
    if discovered:
        candidates.append(Path(discovered).resolve())
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _codex_command(
    executable: Path, home_dir: Path, *arguments: str, timeout: int = 60
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(home_dir)
    return subprocess.run(
        [str(executable), *arguments],
        cwd=PROJECT_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        shell=False,
    )


def _codex_status(values: dict[str, Any]) -> dict[str, Any]:
    executable = _discover_codex(str(values.get("executable", "")))
    home_dir = _resolve_configured_path(str(values["home_dir"]))
    result: dict[str, Any] = {
        "executable": executable,
        "home_dir": home_dir,
        "version": "não disponível",
        "authenticated": False,
        "login_detail": "Codex CLI não localizado.",
    }
    if executable is None:
        return result
    try:
        version = _codex_command(executable, home_dir, "--version", timeout=20)
        result["version"] = (
            version.stdout.strip() or version.stderr.strip() or "versão não informada"
        )
        login = _codex_command(executable, home_dir, "login", "status", timeout=30)
        detail = login.stdout.strip() or login.stderr.strip() or "status não informado"
        result["authenticated"] = login.returncode == 0 and "not logged in" not in detail.casefold()
        result["login_detail"] = detail
    except (OSError, subprocess.SubprocessError) as exc:
        result["login_detail"] = f"Falha ao executar o Codex CLI: {exc}"
    return result


def _ask_directory_list(label: str, current: list[str]) -> list[str]:
    print(f"{label} atuais: {', '.join(current) if current else '(nenhum)'}")
    answer = input(
        "Informe caminhos separados por ponto e vírgula; deixe vazio para manter: "
    ).strip()
    if not answer:
        return current
    if answer == "-":
        return []
    return [item.strip().strip('"') for item in answer.split(";") if item.strip()]


def _parse_directory_entries(raw: str) -> list[str]:
    """Parse comma-separated paths while preserving quoted commas."""
    delimiter = ";" if ";" in raw and "," not in raw else ","
    parsed = next(csv.reader([raw], delimiter=delimiter, skipinitialspace=True))
    return [item.strip().strip('"') for item in parsed if item.strip().strip('"')]


def _directory_key(raw: str) -> str:
    """Compare equivalent paths without changing their stored representation."""
    return os.path.normcase(str(_resolve_configured_path(raw)))


def _print_directory_entries(current: list[str]) -> None:
    if not current:
        print("  (nenhum)")
        return
    for index, configured in enumerate(current, start=1):
        resolved = _resolve_configured_path(configured)
        print(f"  {index}. {configured} -> {resolved}")


def _manage_directory_list(
    label: str, current: list[str], *, writable: bool
) -> list[str]:
    """Add and remove paths without replacing the complete list implicitly."""
    values = list(current)
    while True:
        print(f"\n{label}:")
        _print_directory_entries(values)
        print("  Caminhos relativos usam a raiz do Coworker como base.")
        if writable:
            print("  Escrita aceita somente dentro de data/work/.")
        else:
            print("  Use '.' para a pr\u00f3pria raiz. Caminhos absolutos tamb\u00e9m s\u00e3o aceitos.")
        print("  1. Adicionar um ou mais diret\u00f3rios")
        print("  2. Excluir um diret\u00f3rio")
        if writable:
            print("  3. Restaurar escrita somente ao workspace data/work/")
        print("  0. Voltar")
        answer = input("Escolha uma op\u00e7\u00e3o: ").strip()
        if answer in {"", "0"}:
            return values
        if answer == "1":
            raw = input(
                "Caminhos separados por v\u00edrgula (use aspas se houver v\u00edrgula no caminho): "
            ).strip()
            if not raw:
                continue
            existing = {_directory_key(value) for value in values}
            added = 0
            for item in _parse_directory_entries(raw):
                if writable:
                    normalized = _portable_workspace_path(item)
                    if normalized is None:
                        print(f"Ignorado: escrita fora de data/work/: {item}")
                        continue
                    item = normalized
                key = _directory_key(item)
                if key in existing:
                    print(f"J\u00e1 cadastrado: {item}")
                    continue
                values.append(item)
                existing.add(key)
                added += 1
            print(f"{added} diret\u00f3rio(s) adicionado(s).")
            continue
        if answer == "2":
            if not values:
                print("N\u00e3o h\u00e1 diret\u00f3rios para excluir.")
                continue
            raw_index = input("N\u00famero do diret\u00f3rio a excluir: ").strip()
            try:
                index = int(raw_index)
            except ValueError:
                print("Informe um n\u00famero da lista.")
                continue
            if not 1 <= index <= len(values):
                print("N\u00famero fora da lista.")
                continue
            removed = values.pop(index - 1)
            print(f"Diret\u00f3rio removido: {removed}")
            continue
        if writable and answer == "3":
            if _yes_no("Substituir as ra\u00edzes de escrita por data/work/", default=False):
                values = ["data/work"]
                print("Escrita restrita a data/work/.")
            continue
        print("Escolha uma op\u00e7\u00e3o v\u00e1lida.")


def _manage_read_directories(label: str, current: list[str]) -> list[str]:
    return _manage_directory_list(label, current, writable=False)


def _manage_writable_directories(label: str, current: list[str]) -> list[str]:
    return _manage_directory_list(label, current, writable=True)



def configure_codex(instance_id: str) -> dict[str, Any]:
    """Configura o Codex CLI isolado usado exclusivamente por esta instância."""
    values = _load_telegram_values(instance_id)
    changed = False
    initial_status = _codex_status(values)
    Path(initial_status["home_dir"]).mkdir(parents=True, exist_ok=True)
    values["home_dir"] = str(initial_status["home_dir"])
    if initial_status["executable"] is not None:
        values["executable"] = str(initial_status["executable"])
    changed = True
    while True:
        status = _codex_status(values)
        executable = status["executable"]
        print("\nCodex CLI (obrigatório para a interface Telegram):")
        print(f"  1. Executável: {executable or '(não localizado)'}")
        print(f"     Versão: {status['version']}")
        print(f"  2. CODEX_HOME privado: {status['home_dir']}")
        print(
            "  3. Autenticação desta instância: "
            + ("OK" if status["authenticated"] else "PENDENTE")
        )
        print(f"  4. Backend: {values['backend']}")
        print(f"  5. Sandbox: {values['sandbox']}")
        print(
            "  6. Rede dos comandos: "
            + ("habilitada" if values["network_access"] else "bloqueada")
        )
        print(
            "  10. Super instância: "
            + ("ATIVA" if values["access_mode"] == "super" else "desativada")
        )
        print(
            "  7. Diretórios adicionais de leitura: "
            f"{values['additional_directories'] or '(nenhum)'}"
        )
        print(f"  8. Diretórios com escrita: {values['writable_directories'] or '(nenhum)'}")
        print(f"  9. Tempo máximo por solicitação: {values['timeout_seconds']} segundos")
        print(f"  11. Modelo padrão: {values['model'] or '(padrão do Codex)'}")
        print(
            "  12. Reasoning padrão: "
            f"{values['reasoning_effort'] or '(padrão do modelo)'}"
        )
        print(f"  13. Velocidade padrão: {values['speed']}")
        print(f"  14. Verbosity padrão: {values['verbosity'] or '(padrão do modelo)'}")
        print("  0. Voltar ao menu principal")
        answer = input("Escolha uma opção: ").strip()
        if answer in {"", "0"}:
            break
        if answer == "1":
            raw = _ask("Caminho do executável Codex CLI", str(executable or values["executable"]))
            candidate = _discover_codex(raw)
            if candidate is None:
                print("Executável não encontrado. O valor anterior foi mantido.")
            else:
                values["executable"] = str(candidate)
                changed = True
        elif answer == "2":
            raw = _ask("CODEX_HOME privado desta instância", str(status["home_dir"]))
            home = _resolve_configured_path(raw)
            try:
                home.relative_to(DATA_DIR.resolve())
            except ValueError:
                print(f"CODEX_HOME deve ficar dentro de {DATA_DIR}.")
                continue
            home.mkdir(parents=True, exist_ok=True)
            values["home_dir"] = "data/codex"
            changed = True
        elif answer == "3":
            if executable is None:
                print("Localize primeiro o executável do Codex CLI na opção 1.")
                continue
            home = Path(status["home_dir"])
            home.mkdir(parents=True, exist_ok=True)
            print(f"A autenticação será salva somente em {home}.")
            environment = os.environ.copy()
            environment["CODEX_HOME"] = str(home)
            completed = subprocess.run(
                [str(executable), "login"],
                cwd=PROJECT_ROOT,
                env=environment,
                check=False,
                shell=False,
            )
            if completed.returncode != 0:
                print("O login do Codex não foi concluído.")
        elif answer == "4":
            backend = _ask("Backend (app-server ou exec)", str(values["backend"])).casefold()
            if backend not in {"app-server", "exec"}:
                print("Use app-server ou exec.")
            else:
                values["backend"] = backend
                changed = True
        elif answer == "5":
            sandbox = _ask(
                "Sandbox restrito (read-only ou workspace-write)",
                str(values["sandbox"]),
            ).casefold()
            if values["access_mode"] == "super":
                print("Desative primeiro a super instância na opção 10.")
            elif sandbox not in {"read-only", "workspace-write"}:
                print("Perfil de sandbox inválido.")
            else:
                values["sandbox"] = sandbox
                changed = True
        elif answer == "6":
            if values["access_mode"] == "super":
                print("A rede faz parte do perfil super; desative-o na opção 10.")
                continue
            enable = _yes_no("Permitir acesso de rede aos comandos", default=False)
            if enable and not _yes_no(
                "Confirmar a concessão de rede para esta instância", default=False
            ):
                print("Alteração cancelada.")
            else:
                values["network_access"] = enable
                changed = True
        elif answer == "7":
            values["additional_directories"] = _manage_read_directories(
                "Diretórios adicionais de leitura",
                [str(value) for value in values["additional_directories"]],
            )
            changed = True
        elif answer == "8":
            values["writable_directories"] = _manage_writable_directories(
                "Diretórios com escrita",
                [str(value) for value in values["writable_directories"]],
            )
            changed = True
        elif answer == "9":
            raw = _ask("Tempo máximo em segundos", str(values["timeout_seconds"]))
            try:
                timeout_seconds = int(raw)
            except ValueError:
                print("Informe um número inteiro.")
                continue
            if not 30 <= timeout_seconds <= 86400:
                print("Use um valor entre 30 e 86400 segundos.")
            else:
                values["timeout_seconds"] = timeout_seconds
                changed = True
        elif answer == "10":
            if values["access_mode"] == "super":
                if _yes_no(
                    "Desativar a super instância e restaurar o perfil seguro",
                    default=True,
                ):
                    values["access_mode"] = "restricted"
                    values["sandbox"] = "workspace-write"
                    values["network_access"] = False
                    values["additional_directories"] = []
                    values["writable_directories"] = ["data/work"]
                    changed = True
            else:
                print(
                    "\nATENÇÃO: este modo permite ao bot ler e alterar arquivos do "
                    "computador, acessar a rede e executar aplicações com as "
                    "permissões da conta do gateway."
                )
                if not _yes_no("Continuar com a ativação", default=False):
                    print("Ativação cancelada.")
                    continue
                phrase = input("Digite SUPER INSTANCIA para confirmar: ").strip()
                if phrase != "SUPER INSTANCIA":
                    print("Confirmação incorreta; ativação cancelada.")
                    continue
                values["access_mode"] = "super"
                values["sandbox"] = "danger-full-access"
                values["network_access"] = True
                values["approval_policy"] = "never"
                changed = True
        elif answer == "11":
            values["model"] = input(
                "Modelo padrão (vazio para herdar do Codex): "
            ).strip()
            changed = True
        elif answer == "12":
            reasoning = input(
                "Reasoning padrão (minimal, low, medium, high, xhigh, max, ultra; "
                "vazio para herdar): "
            ).strip().casefold()
            if reasoning not in {"", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}:
                print("Nível de reasoning inválido.")
            else:
                values["reasoning_effort"] = reasoning
                changed = True
        elif answer == "13":
            speed = _ask("Velocidade padrão (standard ou fast)", str(values["speed"])).casefold()
            if speed not in {"standard", "fast"}:
                print("Use standard ou fast.")
            else:
                values["speed"] = speed
                changed = True
        elif answer == "14":
            verbosity = input(
                "Verbosity padrão (low, medium, high; vazio para herdar): "
            ).strip().casefold()
            if verbosity not in {"", "low", "medium", "high"}:
                print("Verbosity inválida.")
            else:
                values["verbosity"] = verbosity
                changed = True
        else:
            print("Escolha uma opção válida.")
    if changed or not TELEGRAM_CONFIG.is_file():
        _save_codex_values(instance_id, values)
    return _codex_status(values)


def _gateway_log_path(instance_id: str) -> Path:
    return _gateway_state_dir(instance_id) / "gateway.log"


def _gateway_failure_detail(instance_id: str) -> str:
    log_path = _gateway_log_path(instance_id)
    try:
        content = log_path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        content = ""
    if not content:
        return f"Consulte o log em '{log_path}'."
    last_line = content.splitlines()[-1].strip()
    return f"Último erro: {last_line[:1000]} (log: '{log_path}')."


def _gateway_process(instance_id: str | None = None) -> subprocess.Popen[bytes]:
    resolved_instance_id = instance_id or str(_load_identity_values()["instance_id"])
    log_path = _gateway_log_path(resolved_instance_id)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with log_path.open("wb") as log_stream:
        return subprocess.Popen(
            [sys.executable, str(GATEWAY), "run"],
            cwd=PROJECT_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log_stream,
            stderr=subprocess.STDOUT,
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


def _gateway_state_dir(instance_id: str) -> Path:
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from interfaces.telegram.config import default_state_dir, load_config

    if TELEGRAM_CONFIG.is_file():
        return load_config(TELEGRAM_CONFIG, require_codex=False).state_dir
    return default_state_dir(instance_id).resolve()


def gateway_runtime_status(instance_id: str) -> dict[str, Any]:
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from interfaces.telegram.runtime import clear_stale_runtime, runtime_status

    state_dir = _gateway_state_dir(instance_id)
    clear_stale_runtime(state_dir)
    return runtime_status(state_dir)


def _service_name_for_instance(instance_id: str) -> str:
    service_root = DATA_DIR / "service"
    if service_root.is_dir():
        for definition_path in service_root.glob("*/service.json"):
            try:
                definition = json.loads(definition_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if str(definition.get("instance_id")) == str(instance_id):
                return str(definition.get("name") or definition_path.parent.name)
    return str(instance_id)


def gateway_service_status(instance_id: str) -> dict[str, Any]:
    """Consulta o serviço Windows associado sem confundi-lo com o processo manual."""
    if os.name != "nt":
        return {"ok": True, "installed": False, "platform": "linux-future"}
    service_name = _service_name_for_instance(instance_id)
    try:
        result = windows_service_action(instance_id, "status", service_name=service_name)
    except (InstallError, OSError) as exc:
        return {"ok": False, "installed": False, "service_name": service_name, "error": str(exc)}
    return {**result, "service_name": service_name}


def _require_runtime_mode(mode: str) -> str:
    normalized = str(mode or "process").strip().casefold()
    if normalized not in {"process", "service"}:
        raise InstallError("Escolha de execução inválida; use process ou service.")
    return normalized


def start_gateway(instance_id: str, *, mode: str = "process") -> dict[str, Any]:
    mode = _require_runtime_mode(mode)
    if mode == "service":
        service = gateway_service_status(instance_id)
        if not service.get("installed"):
            raise InstallError("Não há serviço Windows instalado para esta instância.")
        process = gateway_runtime_status(instance_id)
        if process.get("running"):
            raise InstallError("O gateway já está em execução como processo; pare-o antes de iniciar o serviço.")
        return windows_service_action(
            instance_id, "start", service_name=str(service["service_name"])
        )
    current = gateway_runtime_status(instance_id)
    if current["running"]:
        return {**current, "started": False, "already_running": True}
    service = gateway_service_status(instance_id)
    if service.get("installed") and service.get("state_name") == "running":
        raise InstallError("O gateway já está em execução como serviço; pare-o antes de iniciar um processo.")
    process = _gateway_process(instance_id)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise InstallError(
                "O gateway encerrou durante a inicialização. "
                + _gateway_failure_detail(instance_id)
            )
        current = gateway_runtime_status(instance_id)
        if current["running"] and current["pid"] == process.pid:
            time.sleep(1)
            if process.poll() is not None:
                raise InstallError(
                    "O gateway encerrou logo após a inicialização. "
                    + _gateway_failure_detail(instance_id)
                )
            return {**current, "started": True, "already_running": False}
        time.sleep(0.2)
    _stop_gateway(process)
    raise InstallError("O gateway não confirmou a inicialização em 10 segundos.")


def stop_gateway(
    instance_id: str, *, timeout_seconds: int = 60, mode: str = "process"
) -> dict[str, Any]:
    mode = _require_runtime_mode(mode)
    if mode == "service":
        service = gateway_service_status(instance_id)
        if not service.get("installed"):
            raise InstallError("Não há serviço Windows instalado para esta instância.")
        return windows_service_action(
            instance_id, "stop", service_name=str(service["service_name"])
        )
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from interfaces.telegram.runtime import request_stop, runtime_status

    state_dir = _gateway_state_dir(instance_id)
    requested = request_stop(state_dir)
    if not requested["requested"]:
        return {**requested, "stopped": False, "already_stopped": True}
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        current = runtime_status(state_dir)
        if not current["running"]:
            return {**current, "stopped": True, "already_stopped": False}
        time.sleep(0.2)
    raise InstallError(
        "O gateway não respondeu à parada cooperativa. Ele pode ser uma versão "
        "anterior sem controle persistente; finalize esse processo manualmente."
    )


def restart_gateway(instance_id: str, *, mode: str = "process") -> dict[str, Any]:
    mode = _require_runtime_mode(mode)
    stop_gateway(instance_id, mode=mode)
    return start_gateway(instance_id, mode=mode)


def _choose_gateway_mode(action: str, service: dict[str, Any]) -> str:
    if not service.get("installed"):
        return "process"
    while True:
        answer = _ask(
            f"Executar {action} pelo serviço ou processo (service/process)",
            "service",
        ).casefold()
        if answer in {"service", "serviço", "s"}:
            return "service"
        if answer in {"process", "processo", "p"}:
            return "process"
        print("Escolha service ou process.")


def manage_gateway(instance_id: str) -> None:
    while True:
        status = gateway_runtime_status(instance_id)
        service = gateway_service_status(instance_id)
        state = (
            f"EM EXECUÇÃO (PID {status['pid']})"
            if status["running"]
            else "PARADO"
        )
        service_state = (
            f"{service.get('service_name', instance_id)}: {service.get('state_name', 'indisponível')}"
            if service.get("installed")
            else "não instalado"
        )
        print(f"\nGateway Telegram: processo={state}; serviço={service_state}")
        print("  1. Atualizar status")
        print("  2. Iniciar")
        print("  3. Finalizar")
        print("  4. Reiniciar")
        print("  5. Instalar como serviço Windows")
        print("  6. Remover serviço Windows")
        print("  0. Voltar")
        answer = input("Escolha uma opção: ").strip()
        if answer in {"", "0"}:
            return
        if answer == "1":
            continue
        if answer == "2":
            mode = _choose_gateway_mode("iniciar", service)
            try:
                result = start_gateway(instance_id, mode=mode)
            except (InstallError, OSError) as exc:
                print(f"Não foi possível iniciar o gateway: {exc}")
                continue
            if result.get("already_running"):
                print(f"O gateway já estava em execução (PID {result['pid']}).")
            elif mode == "service":
                print(f"Serviço iniciado: {service.get('service_name', instance_id)}.")
            else:
                print(f"Gateway iniciado (PID {result['pid']}).")
            continue
        if answer == "3":
            if not status["running"] and not service.get("installed"):
                print("O gateway já está parado.")
                continue
            mode = _choose_gateway_mode("parar", service)
            target = (
                f"o serviço {service.get('service_name', instance_id)}"
                if mode == "service"
                else f"o processo PID {status.get('pid')}"
            )
            if _yes_no(f"Finalizar {target}", default=False):
                try:
                    stop_gateway(instance_id, mode=mode)
                except (InstallError, OSError) as exc:
                    print(f"Não foi possível finalizar o gateway: {exc}")
                    continue
                print("Serviço finalizado." if mode == "service" else "Gateway finalizado.")
            continue
        if answer == "4":
            mode = _choose_gateway_mode("reiniciar", service)
            if _yes_no("Reiniciar o gateway agora", default=False):
                try:
                    result = restart_gateway(instance_id, mode=mode)
                except (InstallError, OSError) as exc:
                    print(f"Não foi possível reiniciar o gateway: {exc}")
                    continue
                if mode == "service":
                    print(f"Serviço reiniciado: {service.get('service_name', instance_id)}.")
                else:
                    print(f"Gateway reiniciado (PID {result['pid']}).")
            continue
        if answer == "5":
            if os.name != "nt":
                print("A instalação de serviço Linux permanece planejada para um MVP futuro.")
                continue
            service_name = _ask("Nome do serviço", instance_id)
            startup = _ask(
                "Inicialização (automatic_delayed, automatic ou manual)",
                "automatic_delayed",
            ).casefold()
            account_mode = _ask("Conta (current_user ou local_system)", "current_user").casefold()
            if _yes_no("Instalar e iniciar o serviço agora", default=True):
                try:
                    result = windows_service_action(
                        instance_id, "install", service_name=service_name,
                        display_name=str(_load_identity_values()["display_name"]),
                        startup=startup, account_mode=account_mode,
                    )
                    print(f"Serviço instalado: {result.get('name', service_name)}")
                    windows_service_action(instance_id, "start", service_name=service_name)
                except (InstallError, OSError) as exc:
                    print(f"Não foi possível instalar/iniciar o serviço: {exc}")
                    continue
                print("Serviço iniciado.")
            continue
        if answer == "6":
            if os.name != "nt":
                print("A remoção de serviço Linux permanece planejada para um MVP futuro.")
                continue
            service_name = _ask("Nome do serviço", instance_id)
            if _yes_no(f"Parar e remover o serviço '{service_name}'", default=False):
                try:
                    windows_service_action(instance_id, "remove", service_name=service_name)
                except (InstallError, OSError) as exc:
                    print(f"Não foi possível remover o serviço: {exc}")
                    continue
                print("Serviço removido. Os dados da instância foram preservados.")
            continue
        print("Escolha uma opção válida.")


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
    instance_id: str, *, non_interactive: bool, should_start_gateway: bool
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
        # Corrige configurações privadas antigas antes de o gateway validá-las.
        _save_codex_values(instance_id, _load_telegram_values(instance_id))
        _gateway("profile", "sync")
        _gateway("commands", "sync")
        _gateway("permissions", "sync")
        print("Nome, bio, comandos e sandbox do bot foram sincronizados.")
    paired = pair_owner_interactively()
    process_id = None
    if should_start_gateway:
        runtime = start_gateway(instance_id)
        process_id = runtime["pid"]
        action = "já estava em execução" if runtime["already_running"] else "foi iniciado"
        print(f"Gateway {action} em segundo plano (PID {process_id}).")
    return {"configured": True, "paired": paired, "process_id": process_id}


def _validation_item(
    status: str, component: str, detail: str, cause: str = ""
) -> dict[str, str]:
    return {
        "status": status,
        "component": component,
        "detail": detail,
        "cause": cause,
    }


def validate_installation(instance_id: str) -> list[dict[str, str]]:
    """Valida a instância sem solicitar senhas, tokens ou alterar estado externo."""
    items: list[dict[str, str]] = []
    items.append(
        _validation_item(
            "OK",
            "Python",
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        )
    )
    pdf_available = importlib.util.find_spec("pypdf") is not None
    items.append(
        _validation_item(
            "OK" if pdf_available else "PENDENTE",
            "Extração local de PDF",
            "pypdf disponível" if pdf_available else "pypdf não instalado",
            "Execute python -m pip install -r requirements.txt."
            if not pdf_available
            else "",
        )
    )
    try:
        identity = _load_identity_values()
        items.append(
            _validation_item(
                "OK",
                "Identidade",
                f"{identity['display_name']} ({identity['instance_id']})",
            )
        )
    except (OSError, RuntimeError) as exc:
        items.append(_validation_item("ERRO", "Identidade", "inválida", str(exc)))

    vault_values = _load_secrets_values(instance_id)
    gui = _discover_executable(vault_values["gui"], _keepass_filenames()[0])
    cli = _discover_executable(vault_values["cli"], _keepass_filenames()[1])
    items.append(
        _validation_item(
            "OK" if gui else "PENDENTE",
            "KeePassXC",
            str(gui or "não localizado"),
            "É necessário para abrir e administrar o cofre." if not gui else "",
        )
    )
    items.append(
        _validation_item(
            "OK" if cli else "PENDENTE",
            "KeePassXC CLI",
            str(cli or "não localizado"),
            "É necessário para o agente ler e gravar credenciais." if not cli else "",
        )
    )
    raw_vault = vault_values["vault_path"] or "data/secrets/vault.kdbx"
    vault_path = _resolve_configured_path(raw_vault)
    items.append(
        _validation_item(
            "OK" if vault_path.is_file() else "PENDENTE",
            "Cofre",
            str(vault_path),
            "O arquivo criptografado ainda não foi criado." if not vault_path.is_file() else "",
        )
    )
    master_enrolled = False
    if os.name == "nt" and cli and vault_path.is_file():
        try:
            status = _run_json([sys.executable, str(VAULT_TOOL), "status"], timeout=30)
            master_enrolled = bool(status.get("master_password_enrolled"))
        except InstallError:
            master_enrolled = False
        items.append(
            _validation_item(
                "OK" if master_enrolled else "PENDENTE",
                "Desbloqueio local do cofre",
                "cadastrado no Windows" if master_enrolled else "não cadastrado",
                "Sem ele, o Telegram não consegue salvar ou usar segredos automaticamente."
                if not master_enrolled
                else "",
            )
        )
    elif os.name != "nt":
        items.append(
            _validation_item(
                "LIMITAÇÃO",
                "Desbloqueio local do cofre",
                "backend seguro ainda não implementado neste sistema",
                "O menu é compartilhado, mas a automação de segredos do Telegram "
                "permanece indisponível.",
            )
        )

    items.append(
        _validation_item(
            "OK" if (DATA_DIR / "memory.sqlite3").is_file() else "PENDENTE",
            "Memória SQLite",
            str(DATA_DIR / "memory.sqlite3"),
            "Execute a seção Memória para inicializar o banco."
            if not (DATA_DIR / "memory.sqlite3").is_file()
            else "",
        )
    )

    telegram_configured = TELEGRAM_CONFIG.is_file()
    if not telegram_configured:
        items.append(
            _validation_item(
                "PENDENTE",
                "Telegram",
                "configuração ausente",
                "Abra a seção Telegram para criar a configuração.",
            )
        )

    try:
        codex_values = _load_telegram_values(instance_id)
        codex = _codex_status(codex_values)
        executable = codex["executable"]
        items.append(
            _validation_item(
                "OK" if executable else "PENDENTE",
                "Codex CLI obrigatório",
                f"{executable or 'não localizado'} ({codex['version']})",
                "Instale o Codex CLI ou informe seu executável na seção Codex CLI."
                if not executable
                else "",
            )
        )
        home = Path(codex["home_dir"])
        items.append(
            _validation_item(
                "OK" if home.is_dir() else "PENDENTE",
                "CODEX_HOME privado",
                str(home),
                "A pasta isolada ainda não foi criada; abra a seção Codex CLI."
                if not home.is_dir()
                else "",
            )
        )
        items.append(
            _validation_item(
                "OK" if codex["authenticated"] else "PENDENTE",
                "Autenticação do Codex",
                "conta própria autenticada" if codex["authenticated"] else "não autenticada",
                codex["login_detail"] if not codex["authenticated"] else "",
            )
        )
        items.append(
            _validation_item(
                "OK",
                "Permissões do Codex",
                f"sandbox={codex_values['sandbox']}; "
                f"rede={'sim' if codex_values['network_access'] else 'não'}; "
                f"escrita={codex_values['writable_directories']}",
            )
        )
        if os.name == "nt" and master_enrolled:
            credential_ref = f"APIs/Telegram/{instance_id}"
            try:
                token = _run_json(
                    [sys.executable, str(VAULT_TOOL), "check", credential_ref],
                    timeout=30,
                )
                exists = bool(token.get("entry_exists"))
            except InstallError:
                exists = False
            items.append(
                _validation_item(
                    "OK" if exists else "PENDENTE",
                    "Token do Telegram",
                    "presente no cofre" if exists else "não encontrado no cofre",
                    "Cadastre o token na seção Telegram." if not exists else "",
                )
            )
        else:
            items.append(
                _validation_item(
                    "NÃO VERIFICADO",
                    "Token do Telegram",
                    "o cofre não pode ser destrancado automaticamente",
                    "Conclua primeiro a configuração do cofre.",
                )
            )
        if telegram_configured:
            try:
                pairing = _gateway("pairing", "status", timeout=30)
                owner = pairing.get("owner")
                paired = isinstance(owner, dict)
                items.append(
                    _validation_item(
                        "OK" if paired else "PENDENTE",
                        "Pareamento do Telegram",
                        "pessoa proprietária vinculada"
                        if paired
                        else "sem proprietária vinculada",
                        "Abra a seção Telegram e conclua o pareamento local."
                        if not paired
                        else "",
                    )
                )
            except (InstallError, OSError, subprocess.SubprocessError) as exc:
                items.append(
                    _validation_item(
                        "ERRO",
                        "Pareamento do Telegram",
                        "não foi possível consultar o estado",
                        str(exc),
                    )
                )
        else:
            items.append(
                _validation_item(
                    "PENDENTE",
                    "Pareamento do Telegram",
                    "configuração do Telegram ausente",
                    "Crie a configuração antes de iniciar o pareamento.",
                )
            )
    except (InstallError, OSError, ValueError, TypeError) as exc:
        items.append(
            _validation_item(
                "ERRO", "Configuração Telegram/Codex", "inválida", str(exc)
            )
        )
    if BIS2_CONFIG.is_file():
        try:
            bis2_values = _load_bis2_values()
            jar_path = _resolve_configured_path(str(bis2_values["jar_path"]))
            profiles = bis2_values.get("profiles", {})
            items.append(
                _validation_item(
                    "OK" if jar_path.is_file() and profiles else "PENDENTE",
                    "BIS2 / BISCMD",
                    f"jar={jar_path}; perfis={len(profiles)}",
                    "Configure o caminho do JAR e ao menos um perfil BIS2."
                    if not jar_path.is_file() or not profiles
                    else "",
                )
            )
        except (InstallError, OSError, ValueError, TypeError) as exc:
            items.append(_validation_item("ERRO", "BIS2 / BISCMD", "inválido", str(exc)))
    return items


def print_validation_report(instance_id: str) -> list[dict[str, str]]:
    items = validate_installation(instance_id)
    print(f"\nRelatório da instalação — {instance_id}")
    print("=" * 72)
    for item in items:
        print(f"[{item['status']:^13}] {item['component']}: {item['detail']}")
        if item["cause"]:
            print(f"                Motivo/ação: {item['cause']}")
    incomplete = sum(item["status"] != "OK" for item in items)
    print("-" * 72)
    if incomplete:
        print(f"Resultado: {incomplete} item(ns) requer(em) atenção.")
    else:
        print("Resultado: instalação válida e pronta.")
    return items


def _initialize_memory() -> dict[str, Any]:
    memory_path = DATA_DIR / "memory.sqlite3"
    return (
        {"ok": True, "existing": True}
        if memory_path.is_file()
        else _run_json([sys.executable, str(MEMORY_TOOL), "init"])
    )


def _vault_operational(instance_id: str) -> bool:
    values = _load_secrets_values(instance_id)
    tools_ready = bool(
        _discover_executable(values["gui"], _keepass_filenames()[0])
        and _discover_executable(values["cli"], _keepass_filenames()[1])
    )
    if not tools_ready or not _resolve_configured_path(values["vault_path"]).is_file():
        return False
    if os.name != "nt":
        return False
    try:
        status = _run_json([sys.executable, str(VAULT_TOOL), "status"], timeout=30)
    except InstallError:
        return False
    return bool(status.get("master_password_enrolled"))


def run_configurator(args: argparse.Namespace, identity_values: dict[str, Any]) -> dict[str, Any]:
    """Executa o menu principal e mantém cada seção independente das demais."""
    current = dict(identity_values)
    last_telegram: dict[str, Any] | None = None
    requested_service = getattr(args, "service_action", "none")
    if requested_service != "none":
        service_result = windows_service_action(
            str(current["instance_id"]), requested_service,
            service_name=getattr(args, "service_name", "") or None,
            display_name=str(current["display_name"]),
            startup=getattr(args, "service_startup", "automatic_delayed"),
            account_mode=getattr(args, "service_account_mode", "current_user"),
            non_interactive=False,
        )
        return {
            "ok": True,
            "instance_id": str(current["instance_id"]),
            "display_name": str(current["display_name"]),
            "service": service_result,
        }
    while True:
        instance_id = str(current["instance_id"])
        print(f"\nConfiguração da instância {current['display_name']} ({instance_id})")
        print("  0. Validar/verificar a instalação")
        print("  1. Identidade e personalidade")
        print("  2. Cofre KeePassXC")
        print("  3. Codex CLI, CODEX_HOME e permissões")
        print("  4. Telegram, token e pareamento")
        print("  5. Memória local")
        print("  6. Gerenciar gateway Telegram")
        print("  7. Transcrição local/remota com EccoVox")
        print("  8. Skills e integrações")
        print("  9. Sair do configurador")
        answer = input("Escolha uma seção: ").strip()
        try:
            if answer == "0":
                print_validation_report(instance_id)
            elif answer == "1":
                locked = (
                    (DATA_DIR / "secrets" / "vault.kdbx").is_file()
                    or TELEGRAM_CONFIG.is_file()
                )
                updated = edit_identity(current, allow_instance_id_change=not locked)
                if updated != current:
                    _replace_config(IDENTITY_CONFIG, _identity_content(updated))
                    current = updated
                    print("Identidade atualizada.")
            elif answer == "2":
                _created, tools_ready = configure_vault_executables(
                    instance_id, non_interactive=False
                )
                if tools_ready:
                    configure_vault(non_interactive=False)
            elif answer == "3":
                if not TELEGRAM_CONFIG.is_file():
                    _write_new(TELEGRAM_CONFIG, _telegram_content(instance_id))
                configure_codex(instance_id)
            elif answer == "4":
                if args.skip_telegram:
                    print("A interface Telegram foi desabilitada por --skip-telegram.")
                    continue
                if not TELEGRAM_CONFIG.is_file():
                    _write_new(TELEGRAM_CONFIG, _telegram_content(instance_id))
                if not _vault_operational(instance_id):
                    print(
                        "O Telegram depende do cofre com desbloqueio automático. "
                        "Conclua primeiro a seção 2."
                    )
                    continue
                last_telegram = configure_telegram(
                    instance_id,
                    non_interactive=False,
                    should_start_gateway=not args.no_start,
                )
            elif answer == "5":
                result = _initialize_memory()
                print(
                    "Memória local pronta."
                    if result.get("ok")
                    else "A memória não foi inicializada."
                )
            elif answer == "6":
                manage_gateway(instance_id)
            elif answer == "7":
                if not TELEGRAM_CONFIG.is_file():
                    _write_new(TELEGRAM_CONFIG, _telegram_content(instance_id))
                configure_transcription(instance_id)
            elif answer == "8":
                configure_skill_integrations(instance_id)
            elif answer == "9":
                break
            else:
                print("Escolha uma opção válida do menu.")
        except (InstallError, OSError, subprocess.SubprocessError, ValueError) as exc:
            print(f"A seção não pôde ser concluída: {exc}")
            print("As demais seções continuam disponíveis no menu principal.")
    return {
        "ok": True,
        "instance_id": str(current["instance_id"]),
        "display_name": str(current["display_name"]),
        "telegram": last_telegram,
        "configuration_complete": all(
            item["status"] == "OK"
            for item in validate_installation(str(current["instance_id"]))
        ),
    }


def install(args: argparse.Namespace) -> dict[str, Any]:
    _migrate_legacy_data()
    for relative in (
        "config",
        "memory",
        "secrets",
        "telegram/inbox",
        "telegram/jobs",
    ):
        (DATA_DIR / relative).mkdir(parents=True, exist_ok=True)
    if not INSTRUCTIONS_CONFIG.exists():
        _write_new(INSTRUCTIONS_CONFIG, INSTRUCTIONS_EXAMPLE.read_text(encoding="utf-8"))
    identity_created = False
    previous_instance_id: str | None = None
    if IDENTITY_CONFIG.exists():
        identity_values = _load_identity_values()
        previous_instance_id = str(identity_values["instance_id"])
    elif args.non_interactive:
        raise InstallError(
            "data/config/identity.toml é obrigatório no modo não interativo."
        )
    else:
        collected = collect_identity()
        identity_created = _write_new(IDENTITY_CONFIG, _identity_content(collected))
        identity_values = collected
    instance_id = str(identity_values["instance_id"])
    if not args.non_interactive:
        return run_configurator(args, identity_values)
    requested_service = getattr(args, "service_action", "none")
    if requested_service != "none":
        result = windows_service_action(
            instance_id, requested_service,
            service_name=getattr(args, "service_name", "") or None,
            display_name=str(identity_values["display_name"]),
            startup=getattr(args, "service_startup", "automatic_delayed"),
            account_mode=getattr(args, "service_account_mode", "current_user"),
            non_interactive=True,
        )
        return {
            "ok": True,
            "instance_id": instance_id,
            "display_name": identity_values["display_name"],
            "service": result,
        }

    secrets_created, vault_tools_ready = configure_vault_executables(
        instance_id,
        non_interactive=args.non_interactive,
        previous_instance_id=previous_instance_id,
    )
    vault_ready = vault_tools_ready and configure_vault(
        non_interactive=args.non_interactive
    )
    memory = _initialize_memory()
    telegram_created = False
    telegram_result: dict[str, Any] | None = None
    if not args.skip_telegram:
        telegram_created = _write_new(TELEGRAM_CONFIG, _telegram_content(instance_id))
        if vault_ready:
            telegram_result = configure_telegram(
                instance_id,
                non_interactive=args.non_interactive,
                should_start_gateway=not args.no_start,
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
        "skills_configured": BIS2_CONFIG.is_file(),
        "service": None,
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
    parser.add_argument(
        "--service-action",
        choices=("none", "install", "remove", "start", "stop", "status"),
        default="none",
        help="Administra o serviço Windows da instância; Linux permanece MVP futuro.",
    )
    parser.add_argument("--service-name", default="")
    parser.add_argument(
        "--service-startup",
        choices=("automatic_delayed", "automatic", "manual"),
        default="automatic_delayed",
    )
    parser.add_argument(
        "--service-account-mode",
        choices=("current_user", "local_system"),
        default="current_user",
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
