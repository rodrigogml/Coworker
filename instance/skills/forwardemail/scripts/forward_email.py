#!/usr/bin/env python3
"""Gerencia domínios e aliases no Forward Email sem expor a chave da API."""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = PROJECT_ROOT / "data" / "config" / "forwardemail.toml"
EXAMPLE_CONFIG = PROJECT_ROOT / "config" / "forwardemail.example.toml"
PROJECT_SCRIPTS = PROJECT_ROOT / "scripts"
if str(PROJECT_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(PROJECT_SCRIPTS))

from credential_vault import VaultToolError, read_entry_secret  # noqa: E402
from integration_profiles import (  # noqa: E402
    IntegrationProfileError,
    resolve_credential_ref,
)
from integration_config import missing_config_message  # noqa: E402


ALLOWED_API_HOST = "api.forwardemail.net"
DOMAIN_PATTERN = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$",
    re.IGNORECASE,
)
OBJECT_ID_PATTERN = re.compile(r"^[a-f0-9]{24}$", re.IGNORECASE)
SENSITIVE_KEYS = {
    "api_key",
    "api_token",
    "password",
    "private_key",
    "secret",
    "token",
}


class ForwardEmailToolError(Exception):
    """Erro seguro para apresentação ao agente ou à pessoa usuária."""


class ForwardEmailApiError(ForwardEmailToolError):
    """Erro sanitizado devolvido pela API."""

    def __init__(self, status: int | None, message: str) -> None:
        self.status = status
        self.message = message
        prefix = f"Forward Email HTTP {status}" if status is not None else "Forward Email"
        super().__init__(f"{prefix}: {message}")


@dataclass(frozen=True)
class ForwardEmailConfig:
    """Configuração local não confidencial."""

    api_base: str
    credential_ref: str
    timeout_seconds: int


def load_config(path: Path, profile: str | None = None) -> ForwardEmailConfig:
    """Carrega e valida a configuração TOML."""
    try:
        values = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ForwardEmailToolError(
            missing_config_message("forwardemail", path)
        ) from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ForwardEmailToolError(
            f"Não foi possível carregar a configuração '{path}'."
        ) from exc

    api_base = str(values.get("api_base", "")).rstrip("/")
    parsed = urllib.parse.urlparse(api_base)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ForwardEmailToolError("'api_base' contém uma porta inválida.") from exc
    try:
        _, credential_ref = resolve_credential_ref(values, profile)
    except IntegrationProfileError as exc:
        raise ForwardEmailToolError(str(exc)) from exc
    timeout_seconds = values.get("timeout_seconds", 30)
    if (
        parsed.scheme != "https"
        or parsed.hostname != ALLOWED_API_HOST
        or parsed.path.rstrip("/")
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ForwardEmailToolError(
            "'api_base' deve ser 'https://api.forwardemail.net'."
        )
    if not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= 120:
        raise ForwardEmailToolError("'timeout_seconds' deve estar entre 1 e 120.")
    return ForwardEmailConfig(api_base, credential_ref, timeout_seconds)


def sanitize_payload(value: Any) -> Any:
    """Remove campos que possam conter credenciais de respostas inesperadas."""
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, child in value.items():
            normalized = re.sub(r"[^a-z0-9]+", "_", str(key).lower()).strip("_")
            if normalized in SENSITIVE_KEYS or normalized.endswith("_password"):
                continue
            sanitized[str(key)] = sanitize_payload(child)
        return sanitized
    if isinstance(value, list):
        return [sanitize_payload(item) for item in value]
    return value


def api_error_message(payload: Any) -> str:
    """Extrai uma mensagem curta sem devolver o corpo integral."""
    if isinstance(payload, dict):
        for key in ("message", "error", "detail"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:500]
            if isinstance(value, dict):
                nested = api_error_message(value)
                if nested != "Erro sem detalhes.":
                    return nested
        errors = payload.get("errors")
        if isinstance(errors, list):
            messages = [
                str(item.get("message")).strip()
                for item in errors
                if isinstance(item, dict) and item.get("message")
            ]
            if messages:
                return "; ".join(messages)[:500]
    return "Erro sem detalhes."


def form_value(value: Any) -> Any:
    """Converte valores Python para o formato de formulário da API."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return ",".join(str(item) for item in value)
    return value


class ForwardEmailClient:
    """Cliente mínimo para a API HTTP do Forward Email."""

    def __init__(
        self,
        config: ForwardEmailConfig,
        token: str,
        *,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        if not token:
            raise ForwardEmailToolError("A chave do Forward Email está vazia.")
        encoded = base64.b64encode(f"{token}:".encode("utf-8")).decode("ascii")
        self.config = config
        self._token = token
        self._authorization = f"Basic {encoded}"
        self._opener = opener

    def close(self) -> None:
        """Descarta referências à credencial mantidas pelo processo."""
        self._token = ""
        self._authorization = ""

    def redact(self, message: str) -> str:
        """Remove a credencial de mensagens defensivamente."""
        redacted = message
        if self._token:
            redacted = redacted.replace(self._token, "[REDACTED]")
        if self._authorization:
            redacted = redacted.replace(self._authorization, "[REDACTED]")
        return redacted[:500]

    def request_with_headers(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> tuple[Any, Mapping[str, str]]:
        """Executa uma chamada e devolve JSON e cabeçalhos."""
        encoded_query = urllib.parse.urlencode(
            {
                key: form_value(value)
                for key, value in (query or {}).items()
                if value is not None
            },
            doseq=True,
        )
        url = f"{self.config.api_base}{path}"
        if encoded_query:
            url = f"{url}?{encoded_query}"
        data = None
        if body is not None:
            data = urllib.parse.urlencode(
                {
                    key: form_value(value)
                    for key, value in body.items()
                    if value is not None
                },
                doseq=True,
            ).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Accept": "application/json",
                "Accept-Language": "en",
                "Authorization": self._authorization,
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "Coworker-ForwardEmail/1",
            },
        )
        try:
            with self._opener(
                request,
                timeout=self.config.timeout_seconds,
            ) as response:
                raw_payload = response.read()
                status = getattr(response, "status", 200)
                headers = getattr(response, "headers", {})
        except urllib.error.HTTPError as exc:
            raw_payload = exc.read()
            try:
                payload = json.loads(raw_payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = None
            message = self.redact(api_error_message(payload))
            raise ForwardEmailApiError(exc.code, message) from None
        except urllib.error.URLError as exc:
            reason = self.redact(str(exc.reason))
            raise ForwardEmailToolError(
                f"Não foi possível conectar ao Forward Email: {reason}"
            ) from None

        if not raw_payload:
            return {}, headers
        try:
            payload = json.loads(raw_payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ForwardEmailToolError(
                f"O Forward Email devolveu JSON inválido (HTTP {status})."
            ) from exc
        if status >= 400:
            raise ForwardEmailApiError(
                status,
                self.redact(api_error_message(payload)),
            )
        return payload, headers

    def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        """Executa uma chamada sem expor cabeçalhos."""
        payload, _ = self.request_with_headers(
            method,
            path,
            query=query,
            body=body,
        )
        return payload

    def get_all(
        self,
        path: str,
        *,
        query: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Percorre as páginas de um endpoint de listagem."""
        page = 1
        items: list[dict[str, Any]] = []
        while True:
            page_query = dict(query or {})
            page_query.update({"page": page, "limit": 50, "pagination": True})
            payload, headers = self.request_with_headers(
                "GET",
                path,
                query=page_query,
            )
            if isinstance(payload, list):
                result = payload
            elif isinstance(payload, dict) and isinstance(payload.get("results"), list):
                result = payload["results"]
            else:
                raise ForwardEmailToolError(
                    "A API não devolveu uma lista válida."
                )
            items.extend(item for item in result if isinstance(item, dict))
            raw_pages = headers.get("X-Page-Count") if headers else None
            try:
                total_pages = int(raw_pages) if raw_pages is not None else None
            except (TypeError, ValueError):
                total_pages = None
            if total_pages is not None:
                if page >= total_pages:
                    break
            elif len(result) < 50:
                break
            page += 1
        return items

    def get_account(self) -> dict[str, Any]:
        """Consulta a conta autenticada."""
        payload = self.request("GET", "/v1/account")
        if not isinstance(payload, dict):
            raise ForwardEmailToolError("A API não devolveu uma conta válida.")
        return payload

    def list_domains(self, *, name: str | None = None) -> list[dict[str, Any]]:
        """Lista domínios acessíveis."""
        return self.get_all("/v1/domains", query={"name": name})

    def get_domain(self, domain: str) -> dict[str, Any]:
        """Consulta um domínio."""
        payload = self.request(
            "GET",
            f"/v1/domains/{urllib.parse.quote(domain, safe='')}",
        )
        if not isinstance(payload, dict):
            raise ForwardEmailToolError("A API não devolveu um domínio válido.")
        return payload

    def create_domain(self, body: dict[str, Any]) -> dict[str, Any]:
        """Cria um domínio."""
        payload = self.request("POST", "/v1/domains", body=body)
        if not isinstance(payload, dict):
            raise ForwardEmailToolError("A API não devolveu o domínio criado.")
        return payload

    def update_domain(self, domain: str, body: dict[str, Any]) -> dict[str, Any]:
        """Atualiza um domínio."""
        payload = self.request(
            "PUT",
            f"/v1/domains/{urllib.parse.quote(domain, safe='')}",
            body=body,
        )
        if not isinstance(payload, dict):
            raise ForwardEmailToolError("A API não devolveu o domínio atualizado.")
        return payload

    def delete_domain(self, domain: str) -> Any:
        """Exclui um domínio."""
        return self.request(
            "DELETE",
            f"/v1/domains/{urllib.parse.quote(domain, safe='')}",
        )

    def verify_domain_records(self, domain: str) -> Any:
        """Verifica MX e TXT do domínio."""
        return self.request(
            "GET",
            f"/v1/domains/{urllib.parse.quote(domain, safe='')}/verify-records",
        )

    def verify_domain_smtp(self, domain: str) -> Any:
        """Verifica registros SMTP do domínio."""
        return self.request(
            "GET",
            f"/v1/domains/{urllib.parse.quote(domain, safe='')}/verify-smtp",
        )

    def list_aliases(
        self,
        domain: str,
        *,
        name: str | None = None,
        recipient: str | None = None,
        query: str | None = None,
    ) -> list[dict[str, Any]]:
        """Lista aliases de um domínio."""
        return self.get_all(
            f"/v1/domains/{urllib.parse.quote(domain, safe='')}/aliases",
            query={"name": name, "recipient": recipient, "q": query},
        )

    def get_alias(self, domain: str, alias_reference: str) -> dict[str, Any]:
        """Consulta um alias por ID ou nome."""
        payload = self.request(
            "GET",
            f"/v1/domains/{urllib.parse.quote(domain, safe='')}/aliases/"
            f"{urllib.parse.quote(alias_reference, safe='')}",
        )
        if not isinstance(payload, dict):
            raise ForwardEmailToolError("A API não devolveu um alias válido.")
        return payload

    def create_alias(
        self,
        domain: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        """Cria um alias."""
        payload = self.request(
            "POST",
            f"/v1/domains/{urllib.parse.quote(domain, safe='')}/aliases",
            body=body,
        )
        if not isinstance(payload, dict):
            raise ForwardEmailToolError("A API não devolveu o alias criado.")
        return payload

    def update_alias(
        self,
        domain: str,
        alias_id: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        """Atualiza um alias."""
        payload = self.request(
            "PUT",
            f"/v1/domains/{urllib.parse.quote(domain, safe='')}/aliases/"
            f"{urllib.parse.quote(alias_id, safe='')}",
            body=body,
        )
        if not isinstance(payload, dict):
            raise ForwardEmailToolError("A API não devolveu o alias atualizado.")
        return payload

    def delete_alias(self, domain: str, alias_id: str) -> Any:
        """Exclui um alias."""
        return self.request(
            "DELETE",
            f"/v1/domains/{urllib.parse.quote(domain, safe='')}/aliases/"
            f"{urllib.parse.quote(alias_id, safe='')}",
        )


def normalize_domain(value: str) -> str:
    """Valida e normaliza um FQDN."""
    normalized = value.strip().rstrip(".").lower()
    if not DOMAIN_PATTERN.fullmatch(normalized):
        raise ForwardEmailToolError("Informe um domínio completo válido.")
    return normalized


def normalize_alias_name(value: str, domain: str) -> str:
    """Aceita nome local ou endereço completo no domínio."""
    normalized = value.strip().lower()
    suffix = f"@{domain}"
    if normalized.endswith(suffix):
        normalized = normalized[: -len(suffix)]
    if not normalized or "@" in normalized or any(
        character in normalized for character in ("/", "\\", "\r", "\n")
    ):
        raise ForwardEmailToolError("Informe um nome de alias válido.")
    return normalized


def account_summary(account: dict[str, Any]) -> dict[str, Any]:
    """Seleciona campos não confidenciais da conta."""
    keys = (
        "id",
        "email",
        "display_name",
        "given_name",
        "family_name",
        "plan",
        "plan_expires_at",
        "created_at",
        "updated_at",
    )
    return sanitize_payload({key: account.get(key) for key in keys if key in account})


def domain_summary(domain: dict[str, Any]) -> dict[str, Any]:
    """Seleciona campos operacionais de um domínio."""
    keys = (
        "id",
        "name",
        "plan",
        "is_global",
        "is_verified",
        "has_mx_record",
        "has_txt_record",
        "has_dkim_record",
        "has_return_path_record",
        "has_dmarc_record",
        "has_smtp",
        "is_smtp_suspended",
        "smtp_port",
        "retention_days",
        "max_quota_per_alias",
        "has_adult_content_protection",
        "has_phishing_protection",
        "has_executable_protection",
        "has_virus_protection",
        "has_recipient_verification",
        "ignore_mx_check",
        "created_at",
        "updated_at",
    )
    return sanitize_payload({key: domain.get(key) for key in keys if key in domain})


def alias_summary(alias: dict[str, Any], *, domain: str | None = None) -> dict[str, Any]:
    """Seleciona campos operacionais de um alias."""
    keys = (
        "id",
        "name",
        "description",
        "labels",
        "recipients",
        "is_enabled",
        "error_code_if_disabled",
        "has_imap",
        "has_pgp",
        "has_recipient_verification",
        "max_quota",
        "storage_used",
        "created_at",
        "updated_at",
    )
    summary = {key: alias.get(key) for key in keys if key in alias}
    if domain and alias.get("name"):
        summary["address"] = f"{alias['name']}@{domain}"
    return sanitize_payload(summary)


def exact_domain(
    client: ForwardEmailClient,
    domain_name: str,
) -> dict[str, Any]:
    """Resolve um domínio por igualdade exata."""
    normalized = normalize_domain(domain_name)
    matches = [
        domain
        for domain in client.list_domains(name=normalized)
        if str(domain.get("name", "")).lower() == normalized
    ]
    if not matches:
        raise ForwardEmailToolError(f"Domínio '{normalized}' não encontrado.")
    if len(matches) > 1:
        raise ForwardEmailToolError(
            f"O domínio '{normalized}' é ambíguo."
        )
    return matches[0]


def exact_alias(
    client: ForwardEmailClient,
    domain: str,
    *,
    alias_id: str | None,
    name: str | None,
) -> dict[str, Any]:
    """Resolve um alias por ID ou nome exato."""
    if alias_id:
        if not OBJECT_ID_PATTERN.fullmatch(alias_id):
            raise ForwardEmailToolError("O alias_id informado é inválido.")
        return client.get_alias(domain, alias_id)
    if not name:
        raise ForwardEmailToolError("Informe alias_id ou nome do alias.")
    normalized = normalize_alias_name(name, domain)
    matches = [
        alias
        for alias in client.list_aliases(domain, name=normalized)
        if str(alias.get("name", "")).lower() == normalized
    ]
    if not matches:
        raise ForwardEmailToolError(
            f"Alias '{normalized}@{domain}' não encontrado."
        )
    if len(matches) > 1:
        raise ForwardEmailToolError(
            f"O alias '{normalized}@{domain}' é ambíguo; informe alias_id."
        )
    return matches[0]


def selector_arguments(parser: argparse.ArgumentParser) -> None:
    """Adiciona seleção exclusiva de alias."""
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--alias-id")
    selector.add_argument("--name")


def add_dry_run(parser: argparse.ArgumentParser) -> None:
    """Adiciona simulação a uma operação de escrita."""
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Valida e mostra a alteração sem enviá-la.",
    )


def add_optional_boolean(
    parser: argparse.ArgumentParser,
    name: str,
    destination: str,
    help_text: str,
) -> None:
    """Adiciona --opção e --no-opção com padrão ausente."""
    parser.add_argument(
        f"--{name}",
        dest=destination,
        action=argparse.BooleanOptionalAction,
        default=None,
        help=help_text,
    )


def safe_preview(body: dict[str, Any]) -> dict[str, Any]:
    """Oculta URLs de webhook que podem carregar parâmetros sensíveis."""
    preview = sanitize_payload(body)
    if "bounce_webhook" in preview and preview["bounce_webhook"] is not False:
        preview["bounce_webhook"] = "[configured]"
    return preview


def execute_doctor(
    client: ForwardEmailClient,
    _: argparse.Namespace,
) -> dict[str, Any]:
    """Valida autenticação e acesso de leitura."""
    account = client.get_account()
    domains = client.list_domains()
    return {
        "ok": True,
        "account": account_summary(account),
        "domain_count": len(domains),
        "domain_read": True,
    }


def execute_account_show(
    client: ForwardEmailClient,
    _: argparse.Namespace,
) -> dict[str, Any]:
    """Mostra a conta autenticada."""
    return {"ok": True, "account": account_summary(client.get_account())}


def execute_domains_list(
    client: ForwardEmailClient,
    args: argparse.Namespace,
) -> dict[str, Any]:
    """Lista domínios."""
    name = normalize_domain(args.name) if args.name else None
    domains = client.list_domains(name=name)
    if name:
        domains = [
            domain
            for domain in domains
            if str(domain.get("name", "")).lower() == name
        ]
    return {
        "ok": True,
        "count": len(domains),
        "domains": [domain_summary(domain) for domain in domains],
    }


def execute_domains_show(
    client: ForwardEmailClient,
    args: argparse.Namespace,
) -> dict[str, Any]:
    """Mostra um domínio."""
    domain = exact_domain(client, args.domain)
    detailed = client.get_domain(str(domain["name"]))
    return {"ok": True, "domain": domain_summary(detailed)}


def execute_domains_verify(
    client: ForwardEmailClient,
    args: argparse.Namespace,
) -> dict[str, Any]:
    """Verifica registros de recebimento e SMTP."""
    domain = exact_domain(client, args.domain)
    name = str(domain["name"])
    records = sanitize_payload(client.verify_domain_records(name))
    smtp = sanitize_payload(client.verify_domain_smtp(name))
    refreshed = client.get_domain(name)
    return {
        "ok": True,
        "domain": domain_summary(refreshed),
        "records": records,
        "smtp": smtp,
    }


def execute_domains_create(
    client: ForwardEmailClient,
    args: argparse.Namespace,
) -> dict[str, Any]:
    """Cria um domínio de forma idempotente."""
    name = normalize_domain(args.domain)
    existing = [
        domain
        for domain in client.list_domains(name=name)
        if str(domain.get("name", "")).lower() == name
    ]
    if existing:
        if len(existing) > 1:
            raise ForwardEmailToolError(f"O domínio '{name}' está duplicado.")
        return {
            "ok": True,
            "changed": False,
            "reason": "domain_already_exists",
            "domain": domain_summary(existing[0]),
        }
    body: dict[str, Any] = {
        "domain": name,
        "catchall": args.catchall_recipient or False,
    }
    if args.plan is not None:
        body["plan"] = args.plan
    if args.team_domain is not None:
        body["team_domain"] = normalize_domain(args.team_domain)
    if args.dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "operation": "domains.create",
            "request": safe_preview(body),
        }
    created = client.create_domain(body)
    return {"ok": True, "changed": True, "domain": domain_summary(created)}


def domain_update_body(args: argparse.Namespace) -> dict[str, Any]:
    """Monta somente campos explicitamente informados."""
    body = {
        "smtp_port": args.smtp_port,
        "retention_days": args.retention_days,
        "has_adult_content_protection": args.adult_content_protection,
        "has_phishing_protection": args.phishing_protection,
        "has_executable_protection": args.executable_protection,
        "has_virus_protection": args.virus_protection,
        "has_recipient_verification": args.recipient_verification,
        "ignore_mx_check": args.ignore_mx_check,
        "max_quota_per_alias": args.max_quota_per_alias,
    }
    if args.disable_bounce_webhook:
        body["bounce_webhook"] = False
    elif args.bounce_webhook is not None:
        body["bounce_webhook"] = args.bounce_webhook
    return {key: value for key, value in body.items() if value is not None}


def execute_domains_update(
    client: ForwardEmailClient,
    args: argparse.Namespace,
) -> dict[str, Any]:
    """Atualiza configurações de domínio."""
    domain = exact_domain(client, args.domain)
    name = str(domain["name"])
    body = domain_update_body(args)
    if not body:
        raise ForwardEmailToolError("Informe pelo menos um campo para atualização.")
    if args.dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "operation": "domains.update",
            "domain": domain_summary(domain),
            "request": safe_preview(body),
        }
    updated = client.update_domain(name, body)
    return {"ok": True, "changed": True, "domain": domain_summary(updated)}


def execute_domains_delete(
    client: ForwardEmailClient,
    args: argparse.Namespace,
) -> dict[str, Any]:
    """Exclui um domínio resolvido de forma exata."""
    domain = exact_domain(client, args.domain)
    name = str(domain["name"])
    if args.dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "operation": "domains.delete",
            "domain": domain_summary(domain),
        }
    client.delete_domain(name)
    return {"ok": True, "changed": True, "deleted_domain": name}


def execute_aliases_list(
    client: ForwardEmailClient,
    args: argparse.Namespace,
) -> dict[str, Any]:
    """Lista aliases de um domínio."""
    domain = str(exact_domain(client, args.domain)["name"])
    name = normalize_alias_name(args.name, domain) if args.name else None
    aliases = client.list_aliases(
        domain,
        name=name,
        recipient=args.recipient,
        query=args.query,
    )
    if name:
        aliases = [
            alias
            for alias in aliases
            if str(alias.get("name", "")).lower() == name
        ]
    return {
        "ok": True,
        "domain": domain,
        "count": len(aliases),
        "aliases": [alias_summary(alias, domain=domain) for alias in aliases],
    }


def execute_aliases_show(
    client: ForwardEmailClient,
    args: argparse.Namespace,
) -> dict[str, Any]:
    """Mostra um alias único."""
    domain = str(exact_domain(client, args.domain)["name"])
    alias = exact_alias(
        client,
        domain,
        alias_id=args.alias_id,
        name=args.name,
    )
    detailed = client.get_alias(domain, str(alias["id"]))
    return {
        "ok": True,
        "domain": domain,
        "alias": alias_summary(detailed, domain=domain),
    }


def alias_create_body(args: argparse.Namespace, domain: str) -> dict[str, Any]:
    """Monta o corpo de criação do alias."""
    body: dict[str, Any] = {
        "name": normalize_alias_name(args.name, domain),
        "recipients": args.recipient,
    }
    optional = {
        "description": args.description,
        "labels": args.label,
        "is_enabled": args.enabled,
        "has_recipient_verification": args.recipient_verification,
        "has_imap": args.imap,
        "error_code_if_disabled": args.error_code_if_disabled,
    }
    body.update({key: value for key, value in optional.items() if value is not None})
    return body


def execute_aliases_create(
    client: ForwardEmailClient,
    args: argparse.Namespace,
) -> dict[str, Any]:
    """Cria um alias sem aceitar padrões implícitos."""
    domain = str(exact_domain(client, args.domain)["name"])
    body = alias_create_body(args, domain)
    existing = [
        alias
        for alias in client.list_aliases(domain, name=body["name"])
        if str(alias.get("name", "")).lower() == body["name"]
    ]
    if existing:
        if len(existing) > 1:
            raise ForwardEmailToolError(
                f"O alias '{body['name']}@{domain}' está duplicado."
            )
        current_recipients = sorted(existing[0].get("recipients") or [])
        desired_recipients = sorted(body["recipients"])
        if current_recipients == desired_recipients:
            return {
                "ok": True,
                "changed": False,
                "reason": "alias_already_exists",
                "alias": alias_summary(existing[0], domain=domain),
            }
        raise ForwardEmailToolError(
            "O alias já existe com outros destinatários. Use 'aliases update'."
        )
    if args.dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "operation": "aliases.create",
            "domain": domain,
            "request": safe_preview(body),
        }
    created = client.create_alias(domain, body)
    return {
        "ok": True,
        "changed": True,
        "domain": domain,
        "alias": alias_summary(created, domain=domain),
    }


def alias_update_body(args: argparse.Namespace, domain: str) -> dict[str, Any]:
    """Monta o corpo de atualização do alias."""
    body: dict[str, Any] = {}
    if args.new_name is not None:
        body["name"] = normalize_alias_name(args.new_name, domain)
    values = {
        "recipients": args.recipient,
        "description": args.description,
        "labels": args.label,
        "is_enabled": args.enabled,
        "has_recipient_verification": args.recipient_verification,
        "has_imap": args.imap,
        "error_code_if_disabled": args.error_code_if_disabled,
        "max_quota": args.max_quota,
    }
    body.update({key: value for key, value in values.items() if value is not None})
    return body


def execute_aliases_update(
    client: ForwardEmailClient,
    args: argparse.Namespace,
) -> dict[str, Any]:
    """Atualiza um alias resolvido de forma única."""
    domain = str(exact_domain(client, args.domain)["name"])
    alias = exact_alias(
        client,
        domain,
        alias_id=args.alias_id,
        name=args.name,
    )
    body = alias_update_body(args, domain)
    if not body:
        raise ForwardEmailToolError("Informe pelo menos um campo para atualização.")
    if args.dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "operation": "aliases.update",
            "domain": domain,
            "alias": alias_summary(alias, domain=domain),
            "request": safe_preview(body),
        }
    updated = client.update_alias(domain, str(alias["id"]), body)
    return {
        "ok": True,
        "changed": True,
        "domain": domain,
        "alias": alias_summary(updated, domain=domain),
    }


def execute_aliases_delete(
    client: ForwardEmailClient,
    args: argparse.Namespace,
) -> dict[str, Any]:
    """Exclui um alias resolvido de forma única."""
    domain = str(exact_domain(client, args.domain)["name"])
    alias = exact_alias(
        client,
        domain,
        alias_id=args.alias_id,
        name=args.name,
    )
    if args.dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "operation": "aliases.delete",
            "domain": domain,
            "alias": alias_summary(alias, domain=domain),
        }
    client.delete_alias(domain, str(alias["id"]))
    return {
        "ok": True,
        "changed": True,
        "domain": domain,
        "deleted_alias_id": alias["id"],
        "deleted_address": f"{alias.get('name')}@{domain}",
    }


def build_parser() -> argparse.ArgumentParser:
    """Constrói a CLI da skill."""
    parser = argparse.ArgumentParser(
        description="Gerencia domínios e aliases no Forward Email."
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--profile")
    resources = parser.add_subparsers(dest="resource", required=True)

    doctor = resources.add_parser("doctor", help="Valida token e leitura.")
    doctor.set_defaults(handler=execute_doctor)

    account = resources.add_parser("account", help="Consulta a conta.")
    account_commands = account.add_subparsers(dest="operation", required=True)
    account_show = account_commands.add_parser("show", help="Mostra a conta.")
    account_show.set_defaults(handler=execute_account_show)

    domains = resources.add_parser("domains", help="Gerencia domínios.")
    domain_commands = domains.add_subparsers(dest="operation", required=True)

    domains_list = domain_commands.add_parser("list", help="Lista domínios.")
    domains_list.add_argument("--name")
    domains_list.set_defaults(handler=execute_domains_list)

    domains_show = domain_commands.add_parser("show", help="Mostra um domínio.")
    domains_show.add_argument("--domain", required=True)
    domains_show.set_defaults(handler=execute_domains_show)

    domains_verify = domain_commands.add_parser(
        "verify", help="Verifica DNS e SMTP."
    )
    domains_verify.add_argument("--domain", required=True)
    domains_verify.set_defaults(handler=execute_domains_verify)

    domains_create = domain_commands.add_parser("create", help="Cria um domínio.")
    domains_create.add_argument("--domain", required=True)
    domains_create.add_argument(
        "--plan",
        choices=("free", "enhanced_protection", "team"),
    )
    domains_create.add_argument("--team-domain")
    domains_create.add_argument("--catchall-recipient", action="append")
    add_dry_run(domains_create)
    domains_create.set_defaults(handler=execute_domains_create)

    domains_update = domain_commands.add_parser(
        "update", help="Atualiza um domínio."
    )
    domains_update.add_argument("--domain", required=True)
    domains_update.add_argument("--smtp-port", type=int)
    domains_update.add_argument(
        "--retention-days",
        type=int,
        choices=range(0, 31),
    )
    domains_update.add_argument("--max-quota-per-alias")
    add_optional_boolean(
        domains_update,
        "adult-content-protection",
        "adult_content_protection",
        "Ativa ou desativa proteção de conteúdo adulto.",
    )
    add_optional_boolean(
        domains_update,
        "phishing-protection",
        "phishing_protection",
        "Ativa ou desativa proteção contra phishing.",
    )
    add_optional_boolean(
        domains_update,
        "executable-protection",
        "executable_protection",
        "Ativa ou desativa proteção contra executáveis.",
    )
    add_optional_boolean(
        domains_update,
        "virus-protection",
        "virus_protection",
        "Ativa ou desativa proteção contra vírus.",
    )
    add_optional_boolean(
        domains_update,
        "recipient-verification",
        "recipient_verification",
        "Ativa ou desativa verificação de destinatários.",
    )
    add_optional_boolean(
        domains_update,
        "ignore-mx-check",
        "ignore_mx_check",
        "Ativa ou desativa a dispensa da validação de MX.",
    )
    webhook = domains_update.add_mutually_exclusive_group()
    webhook.add_argument("--bounce-webhook")
    webhook.add_argument("--disable-bounce-webhook", action="store_true")
    add_dry_run(domains_update)
    domains_update.set_defaults(handler=execute_domains_update)

    domains_delete = domain_commands.add_parser(
        "delete", help="Exclui um domínio."
    )
    domains_delete.add_argument("--domain", required=True)
    add_dry_run(domains_delete)
    domains_delete.set_defaults(handler=execute_domains_delete)

    aliases = resources.add_parser("aliases", help="Gerencia aliases.")
    alias_commands = aliases.add_subparsers(dest="operation", required=True)

    aliases_list = alias_commands.add_parser("list", help="Lista aliases.")
    aliases_list.add_argument("--domain", required=True)
    aliases_list.add_argument("--name")
    aliases_list.add_argument("--recipient")
    aliases_list.add_argument("--query")
    aliases_list.set_defaults(handler=execute_aliases_list)

    aliases_show = alias_commands.add_parser("show", help="Mostra um alias.")
    aliases_show.add_argument("--domain", required=True)
    selector_arguments(aliases_show)
    aliases_show.set_defaults(handler=execute_aliases_show)

    aliases_create = alias_commands.add_parser("create", help="Cria um alias.")
    aliases_create.add_argument("--domain", required=True)
    aliases_create.add_argument("--name", required=True)
    aliases_create.add_argument("--recipient", action="append", required=True)
    aliases_create.add_argument("--description")
    aliases_create.add_argument("--label", action="append")
    add_optional_boolean(
        aliases_create,
        "enabled",
        "enabled",
        "Cria o alias habilitado ou desabilitado.",
    )
    add_optional_boolean(
        aliases_create,
        "recipient-verification",
        "recipient_verification",
        "Exige ou dispensa verificação dos destinatários.",
    )
    add_optional_boolean(
        aliases_create,
        "imap",
        "imap",
        "Ativa ou desativa armazenamento IMAP.",
    )
    aliases_create.add_argument(
        "--error-code-if-disabled",
        type=int,
        choices=(250, 421, 550),
    )
    add_dry_run(aliases_create)
    aliases_create.set_defaults(handler=execute_aliases_create)

    aliases_update = alias_commands.add_parser(
        "update", help="Atualiza um alias."
    )
    aliases_update.add_argument("--domain", required=True)
    selector_arguments(aliases_update)
    aliases_update.add_argument("--new-name")
    aliases_update.add_argument("--recipient", action="append")
    aliases_update.add_argument("--description")
    aliases_update.add_argument("--label", action="append")
    aliases_update.add_argument("--max-quota")
    add_optional_boolean(
        aliases_update,
        "enabled",
        "enabled",
        "Habilita ou desabilita o alias.",
    )
    add_optional_boolean(
        aliases_update,
        "recipient-verification",
        "recipient_verification",
        "Exige ou dispensa verificação dos destinatários.",
    )
    add_optional_boolean(
        aliases_update,
        "imap",
        "imap",
        "Ativa ou desativa armazenamento IMAP.",
    )
    aliases_update.add_argument(
        "--error-code-if-disabled",
        type=int,
        choices=(250, 421, 550),
    )
    add_dry_run(aliases_update)
    aliases_update.set_defaults(handler=execute_aliases_update)

    aliases_delete = alias_commands.add_parser(
        "delete", help="Exclui um alias."
    )
    aliases_delete.add_argument("--domain", required=True)
    selector_arguments(aliases_delete)
    add_dry_run(aliases_delete)
    aliases_delete.set_defaults(handler=execute_aliases_delete)

    return parser


def print_json(payload: Any, *, stream: Any = sys.stdout) -> None:
    """Imprime JSON UTF-8 para consumo por agentes."""
    print(
        json.dumps(
            sanitize_payload(payload),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        file=stream,
    )


def main() -> int:
    """Carrega a credencial, executa o comando e sanitiza falhas."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    args = build_parser().parse_args()
    client: ForwardEmailClient | None = None
    try:
        config = load_config(Path(args.config).expanduser().resolve(), args.profile)
        token = read_entry_secret(config.credential_ref)
        client = ForwardEmailClient(config, token)
        token = ""
        result = args.handler(client, args)
    except (ForwardEmailToolError, VaultToolError, OSError) as exc:
        print_json(
            {"ok": False, "error": str(exc), "error_type": type(exc).__name__},
            stream=sys.stderr,
        )
        return 1
    finally:
        if client is not None:
            client.close()
    print_json(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
