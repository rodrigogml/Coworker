#!/usr/bin/env python3
"""Cadastra e renova contas Google sem expor tokens ao agente."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import logging.handlers
import os
import re
import secrets
import subprocess
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PROJECT_ROOT / "data" / "config" / "google.toml"
EXAMPLE_CONFIG = PROJECT_ROOT / "config" / "google.example.toml"

from credential_vault import (  # noqa: E402
    VaultToolError,
    read_entry_credentials,
    write_entry_credentials,
)
from integration_profiles import (  # noqa: E402
    IntegrationProfileError,
    validate_profile_name,
)
from integration_config import missing_config_message  # noqa: E402


ALLOWED_ENDPOINTS = {
    "authorization_endpoint": (
        "accounts.google.com",
        "/o/oauth2/v2/auth",
    ),
    "token_endpoint": ("oauth2.googleapis.com", "/token"),
    "userinfo_endpoint": (
        "openidconnect.googleapis.com",
        "/v1/userinfo",
    ),
}
REQUIRED_IDENTITY_SCOPES = {"openid", "email"}
SERVICE_SCOPES: dict[str, tuple[str, ...]] = {
    "gmail": ("https://www.googleapis.com/auth/gmail.modify",),
    "calendar": (
        "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
        "https://www.googleapis.com/auth/calendar.events",
        "https://www.googleapis.com/auth/calendar.freebusy",
    ),
    "drive": ("https://www.googleapis.com/auth/drive",),
    "contacts": ("https://www.googleapis.com/auth/contacts",),
}
SERVICE_LABELS = {
    "gmail": "Gmail",
    "calendar": "Agenda",
    "drive": "Drive",
    "contacts": "Contatos",
}
_SAFE_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SENSITIVE_ERROR_TOKEN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9._~+/=-]{24,}(?![A-Za-z0-9])")
_EMAIL_IN_ERROR = re.compile(r"\b[^\s@]+@[^\s@]+\b")
_OAUTH_LOG_MAX_BYTES = 256 * 1024
_OAUTH_LOG_BACKUPS = 3
OAUTH_CALLBACK_BODY = (
    "<html><body><h1>Coworker</h1>"
    "<p>Retorno recebido. A autorização ainda está sendo finalizada "
    "no aplicativo.</p><p>Volte ao console para confirmar o resultado.</p>"
    "</body></html>"
)


class GoogleAccountError(Exception):
    """Erro seguro do fluxo de contas Google."""


class OAuthAudit:
    """Log JSONL local, rotativo e fechado para eventos não sensíveis do OAuth."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.session = secrets.token_hex(8)
        self._logger = logging.getLogger(f"coworker.google.oauth.{id(self)}")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        handler = logging.handlers.RotatingFileHandler(
            path,
            maxBytes=_OAUTH_LOG_MAX_BYTES,
            backupCount=_OAUTH_LOG_BACKUPS,
            encoding="utf-8",
            delay=True,
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        self._handler = handler
        self._logger.addHandler(handler)

    def event(self, name: str, **fields: str | int | bool | list[str]) -> None:
        document = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "session": self.session,
            "event": name,
            **fields,
        }
        self._logger.info(
            json.dumps(document, ensure_ascii=False, sort_keys=True, allow_nan=False)
        )

    def close(self) -> None:
        self._logger.removeHandler(self._handler)
        self._handler.close()


def _safe_error_code(value: Any) -> str:
    candidate = str(value or "").strip().casefold()
    return candidate if _SAFE_ERROR_CODE.fullmatch(candidate) else "http_error"


def _safe_error_description(value: Any) -> str:
    """Preserva explicação operacional, removendo valores que possam ser segredos."""
    candidate = " ".join(str(value or "").split())[:500]
    candidate = _EMAIL_IN_ERROR.sub("[redacted]", candidate)
    candidate = _SENSITIVE_ERROR_TOKEN.sub("[redacted]", candidate)
    return candidate[:300]


def _oauth_audit_for_config(config_path: Path | None) -> OAuthAudit | None:
    if config_path is None:
        return None
    try:
        data_root = (PROJECT_ROOT / "data").resolve(strict=True)
        config_path.resolve(strict=True).relative_to(data_root)
        return OAuthAudit(data_root / "log" / "google-oauth.jsonl")
    except (OSError, ValueError):
        return None


def _audit(
    audit: OAuthAudit | None,
    name: str,
    **fields: str | int | bool | list[str],
) -> None:
    if audit is None:
        return
    try:
        audit.event(name, **fields)
    except OSError:
        return


@dataclass(frozen=True)
class GoogleProfile:
    """Perfil não confidencial de uma conta autorizada."""

    name: str
    credential_ref: str
    services: tuple[str, ...]

    @property
    def scopes(self) -> tuple[str, ...]:
        return scopes_for_services(self.services)


@dataclass(frozen=True)
class GoogleConfig:
    """Configuração local dos endpoints e perfis Google."""

    authorization_endpoint: str
    token_endpoint: str
    userinfo_endpoint: str
    client_id: str
    timeout_seconds: int
    authorization_timeout_seconds: int
    default_profile: str
    profiles: dict[str, GoogleProfile]

    def select(self, requested: str | None) -> GoogleProfile:
        try:
            name = validate_profile_name(requested or self.default_profile)
        except IntegrationProfileError as exc:
            raise GoogleAccountError(str(exc)) from exc
        profile = self.profiles.get(name)
        if profile is None:
            available = ", ".join(sorted(self.profiles))
            raise GoogleAccountError(
                f"Perfil Google '{name}' não encontrado. Disponíveis: {available}."
            )
        return profile


@dataclass
class GoogleAccess:
    """Token efêmero e identidade associada."""

    profile: str
    email: str
    access_token: str
    scopes: tuple[str, ...]
    expires_in: int | None

    def close(self) -> None:
        self.access_token = ""


def require_google_scopes(
    access: GoogleAccess,
    required: set[str],
    service: str,
) -> None:
    """Falha cedo quando o perfil não contém os escopos da integração."""
    granted = set(access.scopes)
    missing = sorted(required.difference(granted))
    if missing:
        service_name = next(
            (
                name
                for name, scopes in SERVICE_SCOPES.items()
                if required.issubset(scopes)
            ),
            None,
        )
        authorization = (
            f"--service {service_name}" if service_name else "--service NOME"
        )
        raise GoogleAccountError(
            f"O perfil Google '{access.profile}' não possui os escopos exigidos "
            f"por {service}: {', '.join(missing)}. Autorize o serviço correspondente "
            f"com 'python scripts/google_accounts.py enroll --profile "
            f"{access.profile} {authorization}'."
        )


def validate_services(raw_services: Any, *, field: str) -> tuple[str, ...]:
    """Valida nomes fechados de serviços sem aceitar scopes arbitrários."""
    if not isinstance(raw_services, list):
        raise GoogleAccountError(f"'{field}' deve ser uma lista de serviços.")
    services: list[str] = []
    for raw in raw_services:
        name = str(raw).strip().casefold()
        if name not in SERVICE_SCOPES:
            available = ", ".join(SERVICE_SCOPES)
            raise GoogleAccountError(
                f"Serviço Google inválido em '{field}'. Disponíveis: {available}."
            )
        if name not in services:
            services.append(name)
    return tuple(services)


def scopes_for_services(services: tuple[str, ...]) -> tuple[str, ...]:
    """Expande serviços conhecidos para identidade e scopes OAuth."""
    scopes = ["openid", "email"]
    for service in services:
        try:
            service_scopes = SERVICE_SCOPES[service]
        except KeyError as exc:
            raise GoogleAccountError(f"Serviço Google desconhecido: {service}.") from exc
        for scope in service_scopes:
            if scope not in scopes:
                scopes.append(scope)
    return tuple(scopes)


def services_for_granted_scopes(scopes: tuple[str, ...]) -> tuple[str, ...]:
    """Converte somente conjuntos completos de scopes em serviços utilizáveis."""
    granted = set(scopes)
    return tuple(
        service
        for service, required in SERVICE_SCOPES.items()
        if set(required).issubset(granted)
    )


def _validated_endpoint(field: str, value: Any) -> str:
    endpoint = str(value or "").rstrip("/")
    parsed = urllib.parse.urlparse(endpoint)
    expected_host, expected_path = ALLOWED_ENDPOINTS[field]
    try:
        port = parsed.port
    except ValueError as exc:
        raise GoogleAccountError(f"'{field}' contém uma porta inválida.") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != expected_host
        or parsed.path.rstrip("/") != expected_path.rstrip("/")
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise GoogleAccountError(
            f"'{field}' deve apontar para o endpoint oficial do Google."
        )
    return endpoint


def load_google_config(path: Path = DEFAULT_CONFIG) -> GoogleConfig:
    """Carrega e valida endpoints, cliente OAuth e perfis."""
    try:
        values = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise GoogleAccountError(
            missing_config_message("google", path)
        ) from exc
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise GoogleAccountError(
            f"Não foi possível carregar a configuração Google '{path}'."
        ) from exc

    client_id = str(values.get("client_id", "")).strip()
    default_profile = str(values.get("default_profile", "")).strip()
    timeout = values.get("timeout_seconds", 30)
    authorization_timeout = values.get("authorization_timeout_seconds", 300)
    raw_profiles = values.get("profiles")
    if not client_id:
        raise GoogleAccountError("'client_id' não pode ficar vazio.")
    if "client_credential_ref" in values:
        raise GoogleAccountError(
            "A configuração Google usa o formato OAuth legado. Execute a limpeza "
            "de dados obsoletos no configurador local da instância."
        )
    if not isinstance(timeout, int) or not 1 <= timeout <= 120:
        raise GoogleAccountError("'timeout_seconds' deve estar entre 1 e 120.")
    if (
        not isinstance(authorization_timeout, int)
        or not 60 <= authorization_timeout <= 900
    ):
        raise GoogleAccountError(
            "'authorization_timeout_seconds' deve estar entre 60 e 900."
        )
    if not isinstance(raw_profiles, dict) or not raw_profiles:
        raise GoogleAccountError("'profiles' deve conter ao menos um perfil.")
    try:
        default_profile = validate_profile_name(default_profile)
    except IntegrationProfileError as exc:
        raise GoogleAccountError(str(exc)) from exc

    profiles: dict[str, GoogleProfile] = {}
    for raw_name, raw_profile in raw_profiles.items():
        try:
            name = validate_profile_name(str(raw_name))
        except IntegrationProfileError as exc:
            raise GoogleAccountError(str(exc)) from exc
        if not isinstance(raw_profile, dict):
            raise GoogleAccountError(f"'profiles.{name}' deve ser uma tabela.")
        credential_ref = str(raw_profile.get("credential_ref", "")).strip()
        if not credential_ref:
            raise GoogleAccountError(
                f"'profiles.{name}.credential_ref' não pode ficar vazio."
            )
        if "scopes" in raw_profile:
            raise GoogleAccountError(
                f"'profiles.{name}.scopes' usa o formato legado. Execute a limpeza "
                "de dados obsoletos no configurador local da instância."
            )
        services = validate_services(
            raw_profile.get("services", []),
            field=f"profiles.{name}.services",
        )
        profiles[name] = GoogleProfile(name, credential_ref, services)
    if default_profile not in profiles:
        raise GoogleAccountError(
            f"O perfil padrão Google '{default_profile}' não existe."
        )
    return GoogleConfig(
        _validated_endpoint(
            "authorization_endpoint",
            values.get("authorization_endpoint"),
        ),
        _validated_endpoint("token_endpoint", values.get("token_endpoint")),
        _validated_endpoint("userinfo_endpoint", values.get("userinfo_endpoint")),
        client_id,
        timeout,
        authorization_timeout,
        default_profile,
        profiles,
    )


def _json_request(
    request: urllib.request.Request,
    *,
    timeout: int,
    opener: Callable[..., Any] = urllib.request.urlopen,
    operation: str = "request",
    audit: OAuthAudit | None = None,
) -> dict[str, Any]:
    try:
        with opener(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {}
        code = payload.get("error") if isinstance(payload, dict) else None
        description = (
            payload.get("error_description") if isinstance(payload, dict) else None
        )
        safe_code = _safe_error_code(code)
        safe_description = _safe_error_description(description)
        _audit(
            audit,
            "google_http_error",
            operation=operation,
            http_status=exc.code,
            error=safe_code,
            error_description=safe_description,
        )
        detail = f": {safe_description}" if safe_description else ""
        raise GoogleAccountError(
            f"Google {operation} HTTP {exc.code}: {safe_code}{detail}."
        ) from exc
    except urllib.error.URLError as exc:
        raise GoogleAccountError("Falha de comunicação com o Google.") from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GoogleAccountError("O Google devolveu uma resposta inválida.") from exc
    if not isinstance(payload, dict):
        raise GoogleAccountError("O Google devolveu um objeto inválido.")
    return payload


def _post_form(
    endpoint: str,
    values: dict[str, str],
    *,
    timeout: int,
    opener: Callable[..., Any] = urllib.request.urlopen,
    operation: str = "token",
    audit: OAuthAudit | None = None,
) -> dict[str, Any]:
    request = urllib.request.Request(
        endpoint,
        data=urllib.parse.urlencode(values).encode("ascii"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "Coworker-Google/1.0",
        },
        method="POST",
    )
    return _json_request(
        request,
        timeout=timeout,
        opener=opener,
        operation=operation,
        audit=audit,
    )


def _userinfo(
    config: GoogleConfig,
    access_token: str,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
    audit: OAuthAudit | None = None,
) -> dict[str, Any]:
    request = urllib.request.Request(
        config.userinfo_endpoint,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}",
            "User-Agent": "Coworker-Google/1.0",
        },
        method="GET",
    )
    return _json_request(
        request,
        timeout=config.timeout_seconds,
        opener=opener,
        operation="userinfo",
        audit=audit,
    )


def refresh_google_access(
    config: GoogleConfig,
    requested_profile: str | None,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> GoogleAccess:
    """Troca um refresh token do cofre por acesso efêmero."""
    profile = config.select(requested_profile)
    try:
        account_email, refresh_token = read_entry_credentials(profile.credential_ref)
    except VaultToolError as exc:
        suggested = profile.services[0] if profile.services else "gmail"
        raise GoogleAccountError(
            f"A conta do perfil Google '{profile.name}' ainda não está acessível. "
            "Se ela ainda não foi autorizada, execute 'python "
            f"scripts/google_accounts.py enroll --profile {profile.name} "
            f"--service {suggested}'."
        ) from exc
    try:
        token_fields = {
            "client_id": config.client_id,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
        response = _post_form(
            config.token_endpoint,
            token_fields,
            timeout=config.timeout_seconds,
            opener=opener,
        )
    finally:
        refresh_token = ""
    access_token = response.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise GoogleAccountError("O Google não devolveu um access token.")
    token_type = response.get("token_type")
    if not isinstance(token_type, str) or token_type.lower() != "bearer":
        access_token = ""
        raise GoogleAccountError("O Google devolveu um tipo de token inesperado.")
    granted = response.get("scope")
    scopes = (
        tuple(str(granted).split())
        if isinstance(granted, str) and granted.strip()
        else profile.scopes
    )
    identity = _userinfo(config, access_token, opener=opener)
    email = identity.get("email")
    if not isinstance(email, str) or not email:
        access_token = ""
        raise GoogleAccountError("Não foi possível identificar a conta Google.")
    if account_email and email.casefold() != account_email.casefold():
        access_token = ""
        raise GoogleAccountError(
            "A conta devolvida pelo Google não corresponde ao perfil selecionado."
        )
    expires_in = response.get("expires_in")
    return GoogleAccess(
        profile.name,
        email,
        access_token,
        scopes,
        expires_in if isinstance(expires_in, int) else None,
    )


class _OAuthCallbackHandler(BaseHTTPRequestHandler):
    server: "_OAuthServer"

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        self.server.parameters = {
            key: values[0]
            for key, values in urllib.parse.parse_qs(parsed.query).items()
            if values
        }
        body = OAUTH_CALLBACK_BODY.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: Any) -> None:
        return


class _OAuthServer(HTTPServer):
    parameters: dict[str, str] | None = None


def enroll_google_profile(
    config: GoogleConfig,
    requested_profile: str | None,
    *,
    requested_services: tuple[str, ...] = (),
    all_services: bool = False,
    config_path: Path | None = None,
    opener: Callable[..., Any] = urllib.request.urlopen,
    audit: OAuthAudit | None = None,
) -> dict[str, Any]:
    """Executa consentimento no navegador e persiste somente o refresh token."""
    profile = config.select(requested_profile)
    additions = tuple(SERVICE_SCOPES) if all_services else requested_services
    selected_services = tuple(dict.fromkeys((*profile.services, *additions)))
    if not selected_services:
        raise GoogleAccountError(
            "Selecione ao menos um serviço Google antes de abrir o navegador."
        )
    requested_scopes = scopes_for_services(selected_services)
    _audit(
        audit,
        "enrollment_started",
        profile=profile.name,
        services=list(selected_services),
        client_kind="oauth_public_client_unverified_type",
        client_id_fingerprint=hashlib.sha256(
            config.client_id.encode("utf-8")
        ).hexdigest()[:12],
        pkce_method="S256",
    )
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    server = _OAuthServer(("127.0.0.1", 0), _OAuthCallbackHandler)
    server.timeout = config.authorization_timeout_seconds
    redirect_uri = f"http://127.0.0.1:{server.server_port}/"
    _audit(
        audit,
        "listener_created",
        loopback_host="127.0.0.1",
        port=server.server_port,
        timeout_seconds=config.authorization_timeout_seconds,
    )
    authorization_url = (
        f"{config.authorization_endpoint}?"
        + urllib.parse.urlencode(
            {
                "client_id": config.client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": " ".join(requested_scopes),
                "access_type": "offline",
                "prompt": "consent",
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
    )
    if not webbrowser.open(authorization_url, new=1):
        server.server_close()
        raise GoogleAccountError("Não foi possível abrir o navegador.")
    try:
        server.handle_request()
    finally:
        server.server_close()
    parameters = server.parameters or {}
    _audit(
        audit,
        "callback_received",
        fields_present=sorted(
            key for key in ("code", "error", "state") if key in parameters
        ),
        state_matches=parameters.get("state") == state,
    )
    if parameters.get("state") != state:
        raise GoogleAccountError("A resposta OAuth não corresponde à solicitação.")
    if parameters.get("error"):
        raise GoogleAccountError(
            f"O consentimento Google falhou: {parameters['error'][:100]}."
        )
    code = parameters.get("code")
    if not code:
        raise GoogleAccountError("O Google não devolveu o código de autorização.")
    try:
        token_fields = {
            "client_id": config.client_id,
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
        _audit(
            audit,
            "token_exchange_started",
            fields_present=sorted(token_fields),
            redirect_matches_authorization=True,
            client_secret_sent=False,
        )
        token_response = _post_form(
            config.token_endpoint,
            token_fields,
            timeout=config.timeout_seconds,
            opener=opener,
            operation="token_exchange",
            audit=audit,
        )
    finally:
        code = ""
        verifier = ""
    _audit(
        audit,
        "token_response_received",
        fields_present=sorted(
            key
            for key in (
                "access_token",
                "expires_in",
                "refresh_token",
                "scope",
                "token_type",
            )
            if key in token_response
        ),
    )
    refresh_token = token_response.get("refresh_token")
    access_token = token_response.get("access_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise GoogleAccountError("O Google não devolveu um refresh token.")
    if not isinstance(access_token, str) or not access_token:
        refresh_token = ""
        raise GoogleAccountError("O Google não devolveu um access token.")
    granted_raw = token_response.get("scope")
    if not isinstance(granted_raw, str) or not granted_raw.strip():
        refresh_token = ""
        access_token = ""
        raise GoogleAccountError(
            "O Google não informou as permissões efetivamente concedidas."
        )
    granted_scopes = tuple(dict.fromkeys(granted_raw.split()))
    if not REQUIRED_IDENTITY_SCOPES.issubset(granted_scopes):
        refresh_token = ""
        access_token = ""
        raise GoogleAccountError("O Google não concedeu os escopos de identidade.")
    granted_services = services_for_granted_scopes(granted_scopes)
    identity = _userinfo(config, access_token, opener=opener, audit=audit)
    email = identity.get("email")
    access_token = ""
    if not isinstance(email, str) or not email:
        refresh_token = ""
        raise GoogleAccountError("Não foi possível identificar a conta autorizada.")
    try:
        write_entry_credentials(profile.credential_ref, email, refresh_token)
        _audit(audit, "refresh_token_persisted", profile=profile.name, success=True)
    except Exception:
        _audit(audit, "refresh_token_persisted", profile=profile.name, success=False)
        raise
    finally:
        refresh_token = ""
    if config_path is not None:
        _replace_profile_services(config_path, profile.name, granted_services)
    return {
        "ok": True,
        "profile": profile.name,
        "account": email,
        "services": list(granted_services),
        "requested_services": list(selected_services),
        "scopes": list(granted_scopes),
    }


def _replace_profile_services(
    path: Path,
    profile_name: str,
    services: tuple[str, ...],
) -> None:
    """Atualiza somente a lista não confidencial de serviços de um perfil."""
    lines = path.read_text(encoding="utf-8").splitlines()
    section = f"[profiles.{profile_name}]"
    section_index = next(
        (index for index, line in enumerate(lines) if line.strip() == section),
        None,
    )
    if section_index is None:
        raise GoogleAccountError(f"Perfil Google '{profile_name}' não encontrado.")
    end_index = next(
        (
            index
            for index in range(section_index + 1, len(lines))
            if lines[index].lstrip().startswith("[")
        ),
        len(lines),
    )
    replacement = "services = " + json.dumps(list(services), ensure_ascii=False)
    service_index = next(
        (
            index
            for index in range(section_index + 1, end_index)
            if lines[index].strip().startswith("services")
            and lines[index].strip().split("=", 1)[0].strip() == "services"
        ),
        None,
    )
    if service_index is None:
        lines.insert(end_index, replacement)
    else:
        lines[service_index] = replacement
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def launch_enrollment(
    config_path: Path,
    profile: str | None,
    *,
    services: tuple[str, ...] = (),
    all_services: bool = False,
) -> dict[str, Any]:
    """Abre o fluxo confidencial em um console separado."""
    if os.name != "nt":
        raise GoogleAccountError(
            "O lançador separado está disponível somente no Windows."
        )
    arguments = [
        sys.executable,
        str(Path(__file__).resolve()),
        "_enroll",
        "--config",
        str(config_path),
    ]
    if profile:
        arguments.extend(["--profile", profile])
    for service in services:
        arguments.extend(["--service", service])
    if all_services:
        arguments.append("--all-services")
    process = subprocess.Popen(
        arguments,
        creationflags=subprocess.CREATE_NEW_CONSOLE,
    )
    return {
        "ok": True,
        "interactive": True,
        "process_id": process.pid,
        "requested_services": list(services),
        "all_services": all_services,
        "message": "Conclua o consentimento no navegador e aguarde o console fechar.",
    }


def print_json(value: Any, *, stream: Any = sys.stdout) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2), file=stream)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Gerencia perfis OAuth do Google.")
    commands = parser.add_subparsers(dest="command", required=True)

    list_parser = commands.add_parser("list")
    list_parser.add_argument("--config", default=str(DEFAULT_CONFIG))

    commands.add_parser("services", help="Lista os serviços Google suportados.")

    configure = commands.add_parser(
        "configure", help="Define os serviços locais de um perfil sem abrir OAuth."
    )
    configure.add_argument("--config", default=str(DEFAULT_CONFIG))
    configure.add_argument("--profile")
    configure_group = configure.add_mutually_exclusive_group(required=True)
    configure_group.add_argument(
        "--service", action="append", choices=tuple(SERVICE_SCOPES)
    )
    configure_group.add_argument("--all-services", action="store_true")

    enroll = commands.add_parser("enroll")
    enroll.add_argument("--config", default=str(DEFAULT_CONFIG))
    enroll.add_argument("--profile")
    enroll_group = enroll.add_mutually_exclusive_group()
    enroll_group.add_argument(
        "--service", action="append", choices=tuple(SERVICE_SCOPES)
    )
    enroll_group.add_argument("--all-services", action="store_true")

    doctor = commands.add_parser("doctor")
    doctor.add_argument("--config", default=str(DEFAULT_CONFIG))
    doctor.add_argument("--profile")

    internal = commands.add_parser("_enroll", help=argparse.SUPPRESS)
    internal.add_argument("--config", default=str(DEFAULT_CONFIG))
    internal.add_argument("--profile")
    internal_group = internal.add_mutually_exclusive_group()
    internal_group.add_argument(
        "--service", action="append", choices=tuple(SERVICE_SCOPES)
    )
    internal_group.add_argument("--all-services", action="store_true")
    return parser


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    args = build_parser().parse_args()
    access: GoogleAccess | None = None
    oauth_audit: OAuthAudit | None = None
    try:
        if args.command == "services":
            print_json(
                {
                    "services": [
                        {
                            "name": name,
                            "label": SERVICE_LABELS[name],
                            "scopes": list(scopes),
                        }
                        for name, scopes in SERVICE_SCOPES.items()
                    ]
                }
            )
            return 0
        config_path = Path(args.config).expanduser().resolve()
        config = load_google_config(config_path)
        if args.command == "list":
            result = {
                "default_profile": config.default_profile,
                "profiles": [
                    {
                        "name": item.name,
                        "services": list(item.services),
                        "scopes": list(item.scopes),
                    }
                    for item in config.profiles.values()
                ],
            }
        elif args.command == "configure":
            profile = config.select(args.profile)
            services = (
                tuple(SERVICE_SCOPES)
                if args.all_services
                else tuple(dict.fromkeys(args.service or ()))
            )
            _replace_profile_services(config_path, profile.name, services)
            result = {
                "ok": True,
                "profile": profile.name,
                "services": list(services),
                "external_permissions_changed": False,
            }
        elif args.command == "enroll":
            result = launch_enrollment(
                config_path,
                args.profile,
                services=tuple(args.service or ()),
                all_services=bool(args.all_services),
            )
        elif args.command == "_enroll":
            oauth_audit = _oauth_audit_for_config(config_path)
            result = enroll_google_profile(
                config,
                args.profile,
                requested_services=tuple(args.service or ()),
                all_services=bool(args.all_services),
                config_path=config_path,
                audit=oauth_audit,
            )
        else:
            access = refresh_google_access(config, args.profile)
            result = {
                "ok": True,
                "profile": access.profile,
                "account": access.email,
                "scopes": list(access.scopes),
                "expires_in": access.expires_in,
            }
    except (
        GoogleAccountError,
        IntegrationProfileError,
        VaultToolError,
        OSError,
    ) as exc:
        print_json(
            {"ok": False, "error": str(exc), "error_type": type(exc).__name__},
            stream=sys.stderr,
        )
        if args.command == "_enroll":
            input("A autorização falhou. Pressione Enter para fechar.")
        return 1
    finally:
        if access is not None:
            access.close()
        if oauth_audit is not None:
            oauth_audit.close()
    print_json(result)
    if args.command == "_enroll":
        input("Autorização concluída. Pressione Enter para fechar.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
