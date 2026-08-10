"""Carregamento e validação da configuração local do gateway Telegram."""

from __future__ import annotations

import re
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from interfaces.telegram.identity import InstanceIdentity, load_identity
from interfaces.telegram.feedback import IMMEDIATE_MESSAGES, QUEUED_MESSAGES


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
WORK_DIR = DATA_DIR / "work"
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
class SpeechConfig:
    enabled: bool = False
    backend: str = "cli"
    python_executable: Path | None = None
    project_dir: Path | None = None
    endpoint: str = "http://127.0.0.1:8870"
    allow_remote: bool = False
    timeout_seconds: int = 120
    voices: tuple[str, ...] = ()
    languages: tuple[str, ...] = ("pt-BR",)
    default_voice: str = ""
    default_language: str = "pt-BR"
    default_speed: float = 1.0
    max_characters: int = 4000
    format: str = "opus"


@dataclass(frozen=True)
class ProcessorConfig:
    max_extracted_characters: int
    max_archive_members: int
    max_uncompressed_bytes: int
    max_pages: int
    max_duration_seconds: int
    max_frames: int
    transcription: TranscriptionConfig = TranscriptionConfig()
    speech: SpeechConfig = SpeechConfig()


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
class RetentionConfig:
    """Retenções locais configuráveis para automação e contexto."""

    raw_messages_days: int = 180
    attachments_days: int = 180
    artifacts_days: int = 180
    summaries_days: int = 180


@dataclass(frozen=True)
class LoggingConfig:
    enabled: bool = True
    level: str = "INFO"
    directory: Path = DATA_DIR / "log"
    retention_days: int = 15


@dataclass(frozen=True)
class TelegramGroupConfig:
    """Política privada de um grupo Telegram vinculado à instância."""

    alias: str
    chat_id: int
    enabled: bool = True
    forum_required: bool = True
    default_topic_policy: str = "run"
    capture_mode: str = "mentions_and_replies"
    retention_days: int = 180
    privacy_disabled_confirmed: bool = False


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
    retention: RetentionConfig = RetentionConfig()
    logging: LoggingConfig = LoggingConfig()
    groups: tuple[TelegramGroupConfig, ...] = ()


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


def _require_data_path(path: Path, label: str) -> Path:
    """Garante que o estado persistente permaneça dentro de instance/data."""
    data_root = DATA_DIR.resolve()
    resolved = path.resolve()
    try:
        resolved.relative_to(data_root)
    except ValueError as exc:
        raise TelegramConfigError(
            f"'{label}' deve ficar dentro de '{data_root}'. "
            "Migre a instância para instance/data antes de iniciar o gateway."
        ) from exc
    return resolved


def _require_workspace_path(path: Path, label: str) -> Path:
    """Garante que a escrita manual fique na area livre da instancia."""
    resolved = path.resolve()
    work_root = WORK_DIR.resolve()
    try:
        resolved.relative_to(work_root)
    except ValueError as exc:
        raise TelegramConfigError(
            f"'{label}' deve ficar dentro da area de trabalho '{work_root}'. "
            "Use caminhos relativos como 'data/work/scripts'."
        ) from exc
    return resolved


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


def _retention_config(values: dict[str, Any]) -> RetentionConfig:
    """Lê retenções com padrão mínimo de 180 dias."""
    raw = values.get("retention", {})
    if not isinstance(raw, dict):
        raise TelegramConfigError("A seção [retention] deve ser uma tabela TOML.")
    fields = (
        "raw_messages_days",
        "attachments_days",
        "artifacts_days",
        "summaries_days",
    )
    numbers: list[int] = []
    for field in fields:
        try:
            number = int(raw.get(field, 180))
        except (TypeError, ValueError) as exc:
            raise TelegramConfigError(f"'retention.{field}' deve ser inteiro.") from exc
        if number < 180:
            raise TelegramConfigError(
                f"'retention.{field}' não pode ser menor que 180 dias."
            )
        numbers.append(number)
    return RetentionConfig(*numbers)


def _logging_config(values: dict[str, Any]) -> LoggingConfig:
    raw = values.get("logging", {})
    if not isinstance(raw, dict):
        raise TelegramConfigError("A seção [logging] deve ser uma tabela TOML.")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise TelegramConfigError("'logging.enabled' deve ser booleano.")
    level = str(raw.get("level", "INFO")).strip().upper()
    if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise TelegramConfigError(
            "'logging.level' deve ser DEBUG, INFO, WARNING, ERROR ou CRITICAL."
        )
    try:
        retention_days = int(raw.get("retention_days", 15))
    except (TypeError, ValueError) as exc:
        raise TelegramConfigError("'logging.retention_days' deve ser inteiro.") from exc
    if not 1 <= retention_days <= 3650:
        raise TelegramConfigError("'logging.retention_days' deve estar entre 1 e 3650 dias.")
    directory = _resolve(raw.get("directory", "data/log"), PROJECT_ROOT)
    assert directory is not None
    return LoggingConfig(enabled, level, _require_data_path(directory, "logging.directory"), retention_days)


def _group_configs(values: dict[str, Any], retention: RetentionConfig) -> tuple[TelegramGroupConfig, ...]:
    """Valida grupos sem aceitar permissões ou IDs ambíguos."""
    raw = values.get("groups", {})
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise TelegramConfigError("A seção [groups] deve ser uma tabela TOML.")
    groups: list[TelegramGroupConfig] = []
    for alias, item in raw.items():
        if not isinstance(alias, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{1,31}", alias):
            raise TelegramConfigError("Aliases de grupos devem usar letras minúsculas, números, '_' ou '-'.")
        if not isinstance(item, dict):
            raise TelegramConfigError(f"'groups.{alias}' deve ser uma tabela TOML.")
        try:
            chat_id = int(item.get("chat_id"))
        except (TypeError, ValueError) as exc:
            raise TelegramConfigError(f"'groups.{alias}.chat_id' deve ser inteiro.") from exc
        if chat_id >= 0:
            raise TelegramConfigError(f"'groups.{alias}.chat_id' deve identificar um grupo/supergrupo.")
        enabled = item.get("enabled", True)
        forum_required = item.get("forum_required", True)
        if not isinstance(enabled, bool) or not isinstance(forum_required, bool):
            raise TelegramConfigError(f"'groups.{alias}.enabled/forum_required' devem ser booleanos.")
        topic_policy = str(item.get("default_topic_policy", "run")).strip().casefold()
        if topic_policy not in {"task", "run", "case"}:
            raise TelegramConfigError(f"'groups.{alias}.default_topic_policy' inválida.")
        capture_mode = str(item.get("capture_mode", "mentions_and_replies")).strip().casefold()
        if capture_mode not in {"mentions_and_replies", "full_topic"}:
            raise TelegramConfigError(f"'groups.{alias}.capture_mode' inválida.")
        try:
            group_retention = int(item.get("retention_days", retention.raw_messages_days))
        except (TypeError, ValueError) as exc:
            raise TelegramConfigError(f"'groups.{alias}.retention_days' deve ser inteiro.") from exc
        if group_retention < 180:
            raise TelegramConfigError(f"'groups.{alias}.retention_days' não pode ser menor que 180 dias.")
        privacy_confirmed = item.get("privacy_disabled_confirmed", False)
        if not isinstance(privacy_confirmed, bool):
            raise TelegramConfigError(
                f"'groups.{alias}.privacy_disabled_confirmed' deve ser booleano."
            )
        groups.append(
            TelegramGroupConfig(
                alias,
                chat_id,
                enabled,
                forum_required,
                topic_policy,
                capture_mode,
                group_retention,
                privacy_confirmed,
            )
        )
    return tuple(groups)


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
    # EccoVox indisponÃ­vel degrada somente as operaÃ§Ãµes de Ã¡udio; nÃ£o invalida
    # a configuraÃ§Ã£o nem impede o gateway de iniciar.
    if False and enabled and (backend == "cli" or auto_start):
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


def _speech_config(values: dict[str, Any]) -> SpeechConfig:
    raw = values.get("speech", {})
    if not isinstance(raw, dict):
        raise TelegramConfigError("A seção [processors.speech] deve ser uma tabela TOML.")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise TelegramConfigError("'processors.speech.enabled' deve ser booleano.")
    backend = str(raw.get("backend", "cli")).strip().casefold()
    if backend not in {"cli", "http"}:
        raise TelegramConfigError("'processors.speech.backend' deve ser cli ou http.")
    endpoint = str(raw.get("endpoint", "http://127.0.0.1:8870")).strip()
    parsed = urlparse(endpoint)
    local_hosts = {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise TelegramConfigError("O endpoint EccoVox deve ser HTTP(S), sem credenciais, caminho ou parâmetros.")
    allow_remote = raw.get("allow_remote", False)
    if not isinstance(allow_remote, bool):
        raise TelegramConfigError("'processors.speech.allow_remote' deve ser booleano.")
    remote = parsed.hostname not in local_hosts
    if remote and not allow_remote:
        raise TelegramConfigError("Defina allow_remote=true para usar um EccoVox remoto.")
    if remote and parsed.scheme != "https":
        raise TelegramConfigError("Um EccoVox remoto deve usar HTTPS.")
    executable = _resolve(str(raw.get("python_executable", "")).strip(), PROJECT_ROOT, allow_empty=True)
    project_dir = _resolve(str(raw.get("project_dir", "")).strip(), PROJECT_ROOT, allow_empty=True)
    # A disponibilidade do EccoVox Ã© verificada pelo cliente no momento do uso.
    if False and enabled and backend == "cli" and (executable is None or not executable.is_file() or project_dir is None or not project_dir.is_dir()):
        raise TelegramConfigError("O Python e o diretório do EccoVox são obrigatórios para o backend CLI.")
    try:
        timeout = int(raw.get("timeout_seconds", 120)); max_chars = int(raw.get("max_characters", 4000)); speed = float(raw.get("default_speed", 1.0))
    except (TypeError, ValueError) as exc:
        raise TelegramConfigError("A configuração numérica de fala é inválida.") from exc
    if not 5 <= timeout <= 1800 or not 100 <= max_chars <= 100_000 or not 0.25 <= speed <= 4:
        raise TelegramConfigError("timeout, max_characters ou default_speed fora dos limites.")
    voices = _string_list(raw.get("voices"), "processors.speech.voices")
    languages = _string_list(raw.get("languages", ["pt-BR"]), "processors.speech.languages")
    default_voice = str(raw.get("default_voice", voices[0] if voices else "")).strip()
    default_language = str(raw.get("default_language", languages[0] if languages else "pt-BR")).strip()
    if voices and default_voice not in voices or not languages or default_language not in languages:
        raise TelegramConfigError("Os padrões de voz/idioma devem estar na allowlist.")
    fmt = str(raw.get("format", "opus")).strip().casefold()
    if fmt != "opus":
        raise TelegramConfigError("O formato de fala deve ser opus.")
    return SpeechConfig(enabled, backend, executable, project_dir, endpoint.rstrip("/"), allow_remote, timeout, voices, languages, default_voice, default_language, speed, max_chars, fmt)


def default_state_dir(instance_id: str) -> Path:
    """Retorna o estado volátil da instância dentro de instance/data."""
    del instance_id
    return DATA_DIR / "telegram" / "state"


def default_codex_home(instance_id: str) -> Path:
    """Retorna o CODEX_HOME privado dentro de instance/data."""
    del instance_id
    return DATA_DIR / "codex"


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


def load_config(
    path: Path = DEFAULT_CONFIG,
    *,
    require_codex: bool = True,
    validate_processors: bool = True,
) -> TelegramConfig:
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
    state_dir = _require_data_path(state_dir, "state_dir")
    inbox_dir = _resolve(media_values.get("inbox_dir"), PROJECT_ROOT)
    assert inbox_dir is not None
    inbox_dir = _require_data_path(inbox_dir, "media.inbox_dir")
    jobs_dir = _resolve(media_values.get("jobs_dir", "data/telegram/jobs"), PROJECT_ROOT)
    assert jobs_dir is not None
    jobs_dir = _require_data_path(jobs_dir, "media.jobs_dir")
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
    codex_home = _require_data_path(codex_home, "codex.home_dir")
    raw_generated_images = str(codex_values.get("generated_images_dir", "")).strip()
    generated_images = (
        _resolve(raw_generated_images, PROJECT_ROOT)
        if raw_generated_images
        else (codex_home / "generated_images").resolve()
    )
    generated_images = _require_data_path(generated_images, "codex.generated_images_dir")
    additional = tuple(
        path
        for item in codex_values.get("additional_directories", [])
        if (path := _resolve(item, PROJECT_ROOT)) is not None
    )
    writable = tuple(
        _require_workspace_path(path, "codex.writable_directories")
        for item in codex_values.get("writable_directories", ["data/work"])
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
    retention = _retention_config(values)
    logging_config = _logging_config(values)
    groups = _group_configs(values, retention)
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
        processors=(
            ProcessorConfig(
                *processor_limits,
                _transcription_config(processor_values),
                _speech_config(processor_values),
            )
            if validate_processors
            else ProcessorConfig(*processor_limits)
        ),
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
        retention=retention,
        logging=logging_config,
        groups=groups,
    )
