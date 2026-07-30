#!/usr/bin/env python3
"""Cadastra e renova contas Google sem expor tokens ao agente."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import subprocess
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
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


class GoogleAccountError(Exception):
    """Erro seguro do fluxo de contas Google."""


@dataclass(frozen=True)
class GoogleProfile:
    """Perfil não confidencial de uma conta autorizada."""

    name: str
    credential_ref: str
    scopes: tuple[str, ...]


@dataclass(frozen=True)
class GoogleConfig:
    """Configuração local dos endpoints e perfis Google."""

    authorization_endpoint: str
    token_endpoint: str
    userinfo_endpoint: str
    client_credential_ref: str
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
        raise GoogleAccountError(
            f"O perfil Google '{access.profile}' não possui os escopos exigidos "
            f"por {service}: {', '.join(missing)}. Ajuste google.toml e execute "
            f"'python scripts/google_accounts.py enroll --profile {access.profile}'."
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
            f"Configuração Google não encontrada em '{path}'. Copie "
            f"'{EXAMPLE_CONFIG}' para '{DEFAULT_CONFIG}'."
        ) from exc
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise GoogleAccountError(
            f"Não foi possível carregar a configuração Google '{path}'."
        ) from exc

    client_ref = str(values.get("client_credential_ref", "")).strip()
    default_profile = str(values.get("default_profile", "")).strip()
    timeout = values.get("timeout_seconds", 30)
    authorization_timeout = values.get("authorization_timeout_seconds", 300)
    raw_profiles = values.get("profiles")
    if not client_ref:
        raise GoogleAccountError("'client_credential_ref' não pode ficar vazio.")
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
        raw_scopes = raw_profile.get("scopes")
        if not credential_ref:
            raise GoogleAccountError(
                f"'profiles.{name}.credential_ref' não pode ficar vazio."
            )
        if (
            not isinstance(raw_scopes, list)
            or not raw_scopes
            or not all(isinstance(scope, str) and scope.strip() for scope in raw_scopes)
        ):
            raise GoogleAccountError(
                f"'profiles.{name}.scopes' deve conter textos não vazios."
            )
        scopes = tuple(dict.fromkeys(scope.strip() for scope in raw_scopes))
        if not REQUIRED_IDENTITY_SCOPES.issubset(scopes):
            raise GoogleAccountError(
                f"'profiles.{name}.scopes' deve incluir 'openid' e 'email'."
            )
        profiles[name] = GoogleProfile(name, credential_ref, scopes)
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
        client_ref,
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
        safe_code = str(code)[:100] if code else "http_error"
        raise GoogleAccountError(
            f"Google HTTP {exc.code}: {safe_code}."
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
) -> dict[str, Any]:
    request = urllib.request.Request(
        endpoint,
        data=urllib.parse.urlencode(values).encode("ascii"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "BOTina-Google/1.0",
        },
        method="POST",
    )
    return _json_request(request, timeout=timeout, opener=opener)


def _userinfo(
    config: GoogleConfig,
    access_token: str,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any]:
    request = urllib.request.Request(
        config.userinfo_endpoint,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}",
            "User-Agent": "BOTina-Google/1.0",
        },
        method="GET",
    )
    return _json_request(
        request,
        timeout=config.timeout_seconds,
        opener=opener,
    )


def refresh_google_access(
    config: GoogleConfig,
    requested_profile: str | None,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> GoogleAccess:
    """Troca um refresh token do cofre por acesso efêmero."""
    profile = config.select(requested_profile)
    client_id, client_secret = read_entry_credentials(
        config.client_credential_ref
    )
    account_email, refresh_token = read_entry_credentials(profile.credential_ref)
    try:
        response = _post_form(
            config.token_endpoint,
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=config.timeout_seconds,
            opener=opener,
        )
    finally:
        client_secret = ""
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
        body = (
            "<html><body><h1>BOTina</h1>"
            "<p>Autorização recebida. Você pode fechar esta janela.</p>"
            "</body></html>"
        ).encode("utf-8")
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
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any]:
    """Executa consentimento no navegador e persiste somente o refresh token."""
    profile = config.select(requested_profile)
    client_id, client_secret = read_entry_credentials(
        config.client_credential_ref
    )
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    server = _OAuthServer(("127.0.0.1", 0), _OAuthCallbackHandler)
    server.timeout = config.authorization_timeout_seconds
    redirect_uri = f"http://127.0.0.1:{server.server_port}/"
    authorization_url = (
        f"{config.authorization_endpoint}?"
        + urllib.parse.urlencode(
            {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": " ".join(profile.scopes),
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
        client_secret = ""
        raise GoogleAccountError("Não foi possível abrir o navegador.")
    try:
        server.handle_request()
    finally:
        server.server_close()
    parameters = server.parameters or {}
    if parameters.get("state") != state:
        client_secret = ""
        raise GoogleAccountError("A resposta OAuth não corresponde à solicitação.")
    if parameters.get("error"):
        client_secret = ""
        raise GoogleAccountError(
            f"O consentimento Google falhou: {parameters['error'][:100]}."
        )
    code = parameters.get("code")
    if not code:
        client_secret = ""
        raise GoogleAccountError("O Google não devolveu o código de autorização.")
    try:
        token_response = _post_form(
            config.token_endpoint,
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
            timeout=config.timeout_seconds,
            opener=opener,
        )
    finally:
        client_secret = ""
        code = ""
        verifier = ""
    refresh_token = token_response.get("refresh_token")
    access_token = token_response.get("access_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise GoogleAccountError("O Google não devolveu um refresh token.")
    if not isinstance(access_token, str) or not access_token:
        refresh_token = ""
        raise GoogleAccountError("O Google não devolveu um access token.")
    identity = _userinfo(config, access_token, opener=opener)
    email = identity.get("email")
    access_token = ""
    if not isinstance(email, str) or not email:
        refresh_token = ""
        raise GoogleAccountError("Não foi possível identificar a conta autorizada.")
    try:
        write_entry_credentials(profile.credential_ref, email, refresh_token)
    finally:
        refresh_token = ""
    return {
        "ok": True,
        "profile": profile.name,
        "account": email,
        "scopes": list(profile.scopes),
    }


def launch_enrollment(config_path: Path, profile: str | None) -> dict[str, Any]:
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
    process = subprocess.Popen(
        arguments,
        creationflags=subprocess.CREATE_NEW_CONSOLE,
    )
    return {
        "ok": True,
        "interactive": True,
        "process_id": process.pid,
        "message": "Conclua o consentimento no navegador e aguarde o console fechar.",
    }


def print_json(value: Any, *, stream: Any = sys.stdout) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2), file=stream)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Gerencia perfis OAuth do Google.")
    commands = parser.add_subparsers(dest="command", required=True)

    list_parser = commands.add_parser("list")
    list_parser.add_argument("--config", default=str(DEFAULT_CONFIG))

    enroll = commands.add_parser("enroll")
    enroll.add_argument("--config", default=str(DEFAULT_CONFIG))
    enroll.add_argument("--profile")

    doctor = commands.add_parser("doctor")
    doctor.add_argument("--config", default=str(DEFAULT_CONFIG))
    doctor.add_argument("--profile")

    internal = commands.add_parser("_enroll", help=argparse.SUPPRESS)
    internal.add_argument("--config", default=str(DEFAULT_CONFIG))
    internal.add_argument("--profile")
    return parser


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    args = build_parser().parse_args()
    access: GoogleAccess | None = None
    try:
        config_path = Path(args.config).expanduser().resolve()
        config = load_google_config(config_path)
        if args.command == "list":
            result = {
                "default_profile": config.default_profile,
                "profiles": [
                    {"name": item.name, "scopes": list(item.scopes)}
                    for item in config.profiles.values()
                ],
            }
        elif args.command == "enroll":
            result = launch_enrollment(config_path, args.profile)
        elif args.command == "_enroll":
            result = enroll_google_profile(config, args.profile)
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
    print_json(result)
    if args.command == "_enroll":
        input("Autorização concluída. Pressione Enter para fechar.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
