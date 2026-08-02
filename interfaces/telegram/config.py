"""Carregamento e validação da configuração local do gateway Telegram."""

from __future__ import annotations

import os
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from interfaces.telegram.identity import InstanceIdentity, load_identity
from interfaces.telegram.feedback import IMMEDIATE_MESSAGES, QUEUED_MESSAGES


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
    writable_directories: tuple[Path, ...] = ()
    access_mode: str = "restricted"
    model: str | None = None
    reasoning_effort: str | None = None
    speed: str = "standard"
    verbosity: str | None = None


@dataclass(frozen=True)
class MediaConfig:
    inbox_dir: Path
    jobs_dir: Path
    max_download_bytes: int
    max_upload_bytes: int


@dataclass(frozen=True)
class TranscriptionConfig:
    enabled: bool = False
    backend: str = "cli"
    auto_start: bool = False
    python_executable: Path | None = None
    project_dir: Path | None = None
    endpoint: str = "http://127.0.0.1:8870"
    allow_remote: bool = False
    timeout_seconds: int = 120
    language: str = "pt-BR"
    profile: str | None = None
    model: str = "medium"
    device: str = "cpu"
    compute_type: str = "int8"
    prompt: str = ""
    terms: tuple[str, ...] = ()
    aliases: tuple[tuple[str, str], ...] = ()
    minimum_confidence: float = 0.55


@dataclass(frozen=True)
class ProcessorConfig:
    max_extracted_characters: int
    max_archive_members: int
    max_uncompressed_bytes: int
    max_pages: int
    max_duration_seconds: int
    max_frames: int
    transcription: TranscriptionConfig = TranscriptionConfig()


@dataclass(frozen=True)
class WebhookConfig:
    public_url: str
    secret_credential_ref: str
    listen_host: str
    listen_port: int


@dataclass(frozen=True)
class FeedbackConfig:
    immediate_messages: tuple[str, ...] = IMMEDIATE_MESSAGES
    queued_messages: tuple[str, ...] = QUEUED_MESSAGES
    typing_interval_seconds: float = 4.0


@dataclass(frozen=True)
class TelegramConfig:
    identity: InstanceIdentity
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
    feedback: FeedbackConfig = FeedbackConfig()


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


def _feedback_messages(raw: Any, default: tuple[str, ...], name: str) -> tuple[str, ...]:
    if raw is None:
        return default
    if not isinstance(raw, list):
        raise TelegramConfigError(f"'feedback.{name}' deve ser uma lista TOML.")
    messages = tuple(str(item).strip() for item in raw)
    if not messages or any(not item or len(item) > 200 for item in messages):
        raise TelegramConfigError(
            f"'feedback.{name}' deve conter mensagens de 1 a 200 caracteres."
        )
    return messages


def _string_list(raw: Any, name: str, *, max_items: int = 100) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise TelegramConfigError(f"'{name}' deve ser uma lista TOML.")
    values = tuple(str(item).strip() for item in raw)
    if len(values) > max_items or any(not item or len(item) > 80 for item in values):
        raise TelegramConfigError(f"'{name}' deve conter até {max_items} valores de 1 a 80 caracteres.")
    return values


def _transcription_config(values: dict[str, Any]) -> TranscriptionConfig:
    raw = values.get("transcription", {})
    if not isinstance(raw, dict):
        raise TelegramConfigError("A seção [processors.transcription] deve ser uma tabela TOML.")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise TelegramConfigError("'processors.transcription.enabled' deve ser true ou false.")
    backend = str(raw.get("backend", "cli")).strip().casefold()
    if backend not in {"cli", "http"}:
        raise TelegramConfigError("'processors.transcription.backend' deve ser cli ou http.")
    auto_start = raw.get("auto_start", False)
    if not isinstance(auto_start, bool):
        raise TelegramConfigError("'processors.transcription.auto_start' deve ser true ou false.")
    raw_executable = str(raw.get("python_executable", "")).strip()
    executable = _resolve(raw_executable, PROJECT_ROOT, allow_empty=True)
    raw_project_dir = str(raw.get("project_dir", "")).strip()
    project_dir = _resolve(raw_project_dir, PROJECT_ROOT, allow_empty=True)
    endpoint = str(raw.get("endpoint", "http://127.0.0.1:8870")).strip()
    allow_remote = raw.get("allow_remote", False)
    if not isinstance(allow_remote, bool):
        raise TelegramConfigError("'processors.transcription.allow_remote' deve ser true ou false.")
    parsed_endpoint = urlparse(endpoint)
    local_hosts = {"127.0.0.1", "localhost", "::1"}
    if (
        parsed_endpoint.scheme not in {"http", "https"}
        or not parsed_endpoint.hostname
        or parsed_endpoint.username
        or parsed_endpoint.password
        or parsed_endpoint.path not in {"", "/"}
        or parsed_endpoint.query
        or parsed_endpoint.fragment
    ):
        raise TelegramConfigError("O endpoint EccoVox deve ser HTTP(S), sem credenciais, caminho ou parâmetros.")
    is_remote = parsed_endpoint.hostname not in local_hosts
    if is_remote and not allow_remote:
        raise TelegramConfigError("Defina allow_remote=true para enviar áudio a um EccoVox remoto.")
    if is_remote and parsed_endpoint.scheme != "https":
        raise TelegramConfigError("Um EccoVox remoto deve usar HTTPS para proteger o áudio em trânsito.")
    if is_remote and auto_start:
        raise TelegramConfigError("auto_start não pode iniciar um EccoVox em outra máquina.")
    timeout_seconds = int(raw.get("timeout_seconds", 120))
    minimum_confidence = float(raw.get("minimum_confidence", 0.55))
    if not 5 <= timeout_seconds <= 1800:
        raise TelegramConfigError("'processors.transcription.timeout_seconds' deve ficar entre 5 e 1800.")
    if not 0 <= minimum_confidence <= 1:
        raise TelegramConfigError("'processors.transcription.minimum_confidence' deve ficar entre 0 e 1.")
    language = str(raw.get("language", "pt-BR")).strip()
    profile = str(raw.get("profile", "")).strip() or None
    model = str(raw.get("model", "medium")).strip()
    device = str(raw.get("device", "cpu")).strip().casefold()
    compute_type = str(raw.get("compute_type", "int8")).strip().casefold()
    prompt = str(raw.get("prompt", "")).strip()
    if not language or not model or not device or not compute_type or len(prompt) > 4_000:
        raise TelegramConfigError("A configuração básica da transcrição está inválida.")
    terms = _string_list(raw.get("terms"), "processors.transcription.terms")
    alias_values = _string_list(raw.get("aliases"), "processors.transcription.aliases")
    aliases: list[tuple[str, str]] = []
    for value in alias_values:
        source, separator, target = value.partition("=")
        if not separator or not source.strip() or not target.strip():
            raise TelegramConfigError("Aliases de transcrição devem usar origem=destino.")
        aliases.append((source.strip(), target.strip()))
    if enabled and (backend == "cli" or auto_start):
        if executable is None or not executable.is_file():
            raise TelegramConfigError("O Python configurado para o EccoVox não foi encontrado.")
        if project_dir is None or not project_dir.is_dir():
            raise TelegramConfigError("O diretório configurado do EccoVox não foi encontrado.")
    return TranscriptionConfig(
        enabled=enabled,
        backend=backend,
        auto_start=auto_start,
        python_executable=executable,
        project_dir=project_dir,
        endpoint=endpoint.rstrip("/"),
        allow_remote=allow_remote,
        timeout_seconds=timeout_seconds,
        language=language,
        profile=profile,
        model=model,
        device=device,
        compute_type=compute_type,
        prompt=prompt,
        terms=terms,
        aliases=tuple(aliases),
        minimum_confidence=minimum_confidence,
    )


def default_state_dir(instance_id: str) -> Path:
    """Mantém o estado volátil fora do diretório sincronizado do projeto."""
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data) / "Coworker" / "instances" / instance_id / "telegram"
    return Path.home() / ".coworker" / "instances" / instance_id / "telegram"


def default_codex_home(instance_id: str) -> Path:
    """Isola autenticação, configuração e sessões usadas pela interface remota."""
    return default_state_dir(instance_id).parent / "codex"


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

    identity = load_identity(resolved.parent / "identity.toml")
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
        _resolve(raw_state, PROJECT_ROOT)
        if raw_state
        else default_state_dir(identity.instance_id).resolve()
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
        else default_codex_home(identity.instance_id).resolve()
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
    writable = tuple(
        path
        for item in codex_values.get("writable_directories", ["data"])
        if (path := _resolve(item, PROJECT_ROOT)) is not None
    )
    raw_access_mode = codex_values.get("access_mode")
    access_mode = str(raw_access_mode or "restricted").strip().casefold()
    if access_mode not in {"restricted", "super"}:
        raise TelegramConfigError(
            "'codex.access_mode' deve ser restricted ou super."
        )
    sandbox = str(codex_values.get("sandbox", "workspace-write")).strip()
    if sandbox not in {"read-only", "workspace-write", "danger-full-access"}:
        raise TelegramConfigError("Sandbox do Codex inválido.")
    network_access = codex_values.get("network_access", False)
    if not isinstance(network_access, bool):
        raise TelegramConfigError("'codex.network_access' deve ser true ou false.")
    if raw_access_mode is None and sandbox == "danger-full-access":
        access_mode = "super"
    approval = str(codex_values.get("approval_policy", "never")).strip()
    if approval not in {"untrusted", "on-request", "never"}:
        raise TelegramConfigError("Política de aprovação do Codex inválida.")
    if access_mode == "super":
        sandbox = "danger-full-access"
        network_access = True
        approval = "never"
    elif sandbox == "danger-full-access":
        raise TelegramConfigError(
            "Use 'codex.access_mode = \"super\"' para liberar acesso irrestrito."
        )
    backend = str(codex_values.get("backend", "exec")).strip().casefold()
    if backend not in {"exec", "app-server"}:
        raise TelegramConfigError("'codex.backend' deve ser exec ou app-server.")
    model = str(codex_values.get("model", "")).strip() or None
    reasoning_effort = (
        str(codex_values.get("reasoning_effort", "")).strip().casefold() or None
    )
    if reasoning_effort not in {
        None, "minimal", "low", "medium", "high", "xhigh", "max", "ultra"
    }:
        raise TelegramConfigError(
            "'codex.reasoning_effort' deve ser minimal, low, medium, high, xhigh, max ou ultra."
        )
    speed = str(codex_values.get("speed", "standard")).strip().casefold()
    if speed not in {"standard", "fast"}:
        raise TelegramConfigError("'codex.speed' deve ser standard ou fast.")
    verbosity = str(codex_values.get("verbosity", "")).strip().casefold() or None
    if verbosity not in {None, "low", "medium", "high"}:
        raise TelegramConfigError("'codex.verbosity' deve ser low, medium ou high.")
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
    feedback_values = values.get("feedback", {})
    if not isinstance(feedback_values, dict):
        raise TelegramConfigError("A seção [feedback] deve ser uma tabela TOML.")
    typing_interval = float(feedback_values.get("typing_interval_seconds", 4.0))
    if not 1.0 <= typing_interval <= 5.0:
        raise TelegramConfigError(
            "'feedback.typing_interval_seconds' deve estar entre 1 e 5 segundos."
        )
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
        identity=identity,
        transport=transport,
        credential_ref=credential_ref,
        project_root=project_root,
        state_dir=state_dir,
        poll_timeout_seconds=poll_timeout,
        request_timeout_seconds=request_timeout,
        pairing=PairingConfig(ttl, attempts),
        codex=CodexConfig(
            executable=executable,
            home_dir=codex_home,
            sandbox=sandbox,
            network_access=network_access,
            approval_policy=approval,
            timeout_seconds=codex_timeout,
            additional_directories=additional,
            backend=backend,
            generated_images_dir=generated_images,
            writable_directories=writable,
            access_mode=access_mode,
            model=model,
            reasoning_effort=reasoning_effort,
            speed=speed,
            verbosity=verbosity,
        ),
        media=MediaConfig(inbox_dir, jobs_dir, max_download, max_upload),
        processors=ProcessorConfig(*processor_limits, _transcription_config(processor_values)),
        webhook=WebhookConfig(
            str(webhook_values.get("public_url", "")).strip(),
            str(webhook_values.get("secret_credential_ref", "")).strip(),
            str(webhook_values.get("listen_host", "127.0.0.1")).strip(),
            listen_port,
        ),
        feedback=FeedbackConfig(
            _feedback_messages(
                feedback_values.get("immediate_messages"),
                IMMEDIATE_MESSAGES,
                "immediate_messages",
            ),
            _feedback_messages(
                feedback_values.get("queued_messages"),
                QUEUED_MESSAGES,
                "queued_messages",
            ),
            typing_interval,
        ),
    )
