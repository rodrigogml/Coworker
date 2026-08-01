"""Carregamento e validação da configuração local do gateway Telegram."""

from __future__ import annotations

import os
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PROJECT_ROOT / "data" / "config" / "telegram.toml"
EXAMPLE_CONFIG = PROJECT_ROOT / "config" / "telegram.example.toml"


class TelegramConfigError(RuntimeError):
    """Representa uma configuração local ausente ou inválida."""


@dataclass(frozen=True)
class PairingConfig:
    ttl_seconds: int
    max_attempts: int


@dataclass(frozen=True)
class CodexConfig:
    executable: Path
    home_dir: Path
    sandbox: str
    network_access: bool
    approval_policy: str
    timeout_seconds: int
    additional_directories: tuple[Path, ...]
    backend: str = "exec"
    generated_images_dir: Path | None = None


@dataclass(frozen=True)
class MediaConfig:
    inbox_dir: Path
    jobs_dir: Path
    max_download_bytes: int
    max_upload_bytes: int


@dataclass(frozen=True)
class ProcessorConfig:
    max_extracted_characters: int
    max_archive_members: int
    max_uncompressed_bytes: int
    max_pages: int
    max_duration_seconds: int
    max_frames: int


@dataclass(frozen=True)
class WebhookConfig:
    public_url: str
    secret_credential_ref: str
    listen_host: str
    listen_port: int


@dataclass(frozen=True)
class TelegramConfig:
    transport: str
    credential_ref: str
    project_root: Path
    state_dir: Path
    poll_timeout_seconds: int
    request_timeout_seconds: int
    pairing: PairingConfig
    codex: CodexConfig
    media: MediaConfig
    processors: ProcessorConfig
    webhook: WebhookConfig


def _mapping(values: dict[str, Any], name: str) -> dict[str, Any]:
    section = values.get(name)
    if not isinstance(section, dict):
        raise TelegramConfigError(f"A seção [{name}] é obrigatória.")
    return section


def _resolve(raw: Any, base: Path, *, allow_empty: bool = False) -> Path | None:
    value = str(raw or "").strip()
    if not value:
        if allow_empty:
            return None
        raise TelegramConfigError("Um caminho obrigatório está vazio.")
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def default_state_dir() -> Path:
    """Mantém o estado volátil fora do diretório sincronizado do projeto."""
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data) / "BOTina" / "telegram"
    return Path.home() / ".botina" / "telegram"


def default_codex_home() -> Path:
    """Isola autenticação, configuração e sessões usadas pela interface remota."""
    return default_state_dir().parent / "codex"


def discover_codex(raw: Any) -> Path:
    """Localiza uma instalação autônoma do Codex CLI."""
    configured = str(raw or "").strip()
    if configured:
        path = Path(configured).expanduser()
        path = path.resolve() if path.is_absolute() else (PROJECT_ROOT / path).resolve()
        if path.is_file():
            return path
        raise TelegramConfigError(f"Codex CLI não encontrado em '{path}'.")
    discovered = shutil.which("codex") or shutil.which("codex.exe")
    if not discovered:
        raise TelegramConfigError(
            "Codex CLI autônomo não encontrado no PATH. Instale-o ou preencha "
            "'codex.executable' em data/config/telegram.toml."
        )
    return Path(discovered).resolve()


def load_config(path: Path = DEFAULT_CONFIG, *, require_codex: bool = True) -> TelegramConfig:
    """Carrega a configuração privada sem criar ou sobrescrever a instância."""
    resolved = path.expanduser().resolve()
    try:
        with resolved.open("rb") as stream:
            values = tomllib.load(stream)
    except FileNotFoundError as exc:
        raise TelegramConfigError(
            f"Configuração ausente. Copie '{EXAMPLE_CONFIG}' para '{DEFAULT_CONFIG}'."
        ) from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise TelegramConfigError("A configuração do Telegram não pôde ser lida.") from exc

    pairing_values = _mapping(values, "pairing")
    codex_values = _mapping(values, "codex")
    media_values = _mapping(values, "media")
    webhook_values = _mapping(values, "webhook")
    transport = str(values.get("transport", "polling")).strip().casefold()
    if transport not in {"polling", "webhook", "auto"}:
        raise TelegramConfigError("'transport' deve ser polling, webhook ou auto.")
    credential_ref = str(values.get("credential_ref", "")).strip()
    if not credential_ref:
        raise TelegramConfigError("'credential_ref' é obrigatório.")
    project_root = _resolve(values.get("project_root", "."), PROJECT_ROOT)
    assert project_root is not None
    if not project_root.is_dir():
        raise TelegramConfigError(f"Projeto do Codex não encontrado em '{project_root}'.")
    raw_state = str(values.get("state_dir", "")).strip()
    state_dir = (
        _resolve(raw_state, PROJECT_ROOT) if raw_state else default_state_dir().resolve()
    )
    assert state_dir is not None
    inbox_dir = _resolve(media_values.get("inbox_dir"), PROJECT_ROOT)
    assert inbox_dir is not None
    jobs_dir = _resolve(media_values.get("jobs_dir", "data/telegram/jobs"), PROJECT_ROOT)
    assert jobs_dir is not None
    executable = (
        discover_codex(codex_values.get("executable"))
        if require_codex
        else Path(str(codex_values.get("executable") or "codex"))
    )
    raw_codex_home = str(codex_values.get("home_dir", "")).strip()
    codex_home = (
        _resolve(raw_codex_home, PROJECT_ROOT)
        if raw_codex_home
        else default_codex_home().resolve()
    )
    assert codex_home is not None
    raw_generated_images = str(codex_values.get("generated_images_dir", "")).strip()
    generated_images = (
        _resolve(raw_generated_images, PROJECT_ROOT)
        if raw_generated_images
        else (codex_home / "generated_images").resolve()
    )
    additional = tuple(
        path
        for item in codex_values.get("additional_directories", [])
        if (path := _resolve(item, PROJECT_ROOT)) is not None
    )
    sandbox = str(codex_values.get("sandbox", "workspace-write")).strip()
    if sandbox not in {"read-only", "workspace-write", "danger-full-access"}:
        raise TelegramConfigError("Sandbox do Codex inválido.")
    network_access = codex_values.get("network_access", False)
    if not isinstance(network_access, bool):
        raise TelegramConfigError("'codex.network_access' deve ser true ou false.")
    approval = str(codex_values.get("approval_policy", "never")).strip()
    if approval not in {"untrusted", "on-request", "never"}:
        raise TelegramConfigError("Política de aprovação do Codex inválida.")
    backend = str(codex_values.get("backend", "exec")).strip().casefold()
    if backend not in {"exec", "app-server"}:
        raise TelegramConfigError("'codex.backend' deve ser exec ou app-server.")
    ttl = int(pairing_values.get("ttl_seconds", 600))
    attempts = int(pairing_values.get("max_attempts", 5))
    poll_timeout = int(values.get("poll_timeout_seconds", 45))
    request_timeout = int(values.get("request_timeout_seconds", 60))
    codex_timeout = int(codex_values.get("timeout_seconds", 1800))
    max_download = int(media_values.get("max_download_bytes", 20 * 1024 * 1024))
    max_upload = int(media_values.get("max_upload_bytes", 20 * 1024 * 1024))
    processor_values = values.get("processors", {})
    if not isinstance(processor_values, dict):
        raise TelegramConfigError("A seção [processors] deve ser uma tabela TOML.")
    listen_port = int(webhook_values.get("listen_port", 8787))
    if not 60 <= ttl <= 3600 or not 1 <= attempts <= 10:
        raise TelegramConfigError("Limites de pareamento inválidos.")
    if not 5 <= poll_timeout <= 50 or not 10 <= request_timeout <= 180:
        raise TelegramConfigError("Tempos da API Telegram inválidos.")
    if (
        not 30 <= codex_timeout <= 86400
        or not 1 <= max_download <= 50 * 1024 * 1024
        or not 1 <= max_upload <= 50 * 1024 * 1024
    ):
        raise TelegramConfigError("Limites de execução ou mídia inválidos.")
    if not 1 <= listen_port <= 65535:
        raise TelegramConfigError("Porta do webhook inválida.")
    processor_limits = (
        int(processor_values.get("max_extracted_characters", 200_000)),
        int(processor_values.get("max_archive_members", 500)),
        int(processor_values.get("max_uncompressed_bytes", 100 * 1024 * 1024)),
        int(processor_values.get("max_pages", 200)),
        int(processor_values.get("max_duration_seconds", 3600)),
        int(processor_values.get("max_frames", 100)),
    )
    if any(value <= 0 for value in processor_limits):
        raise TelegramConfigError("Os limites de [processors] devem ser positivos.")
    return TelegramConfig(
        transport=transport,
        credential_ref=credential_ref,
        project_root=project_root,
        state_dir=state_dir,
        poll_timeout_seconds=poll_timeout,
        request_timeout_seconds=request_timeout,
        pairing=PairingConfig(ttl, attempts),
        codex=CodexConfig(
            executable,
            codex_home,
            sandbox,
            network_access,
            approval,
            codex_timeout,
            additional,
            backend,
            generated_images,
        ),
        media=MediaConfig(inbox_dir, jobs_dir, max_download, max_upload),
        processors=ProcessorConfig(*processor_limits),
        webhook=WebhookConfig(
            str(webhook_values.get("public_url", "")).strip(),
            str(webhook_values.get("secret_credential_ref", "")).strip(),
            str(webhook_values.get("listen_host", "127.0.0.1")).strip(),
            listen_port,
        ),
    )
