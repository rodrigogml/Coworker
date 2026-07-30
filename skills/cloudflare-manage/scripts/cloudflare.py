#!/usr/bin/env python3
"""Gerencia zonas e registros DNS da Cloudflare sem expor o token de API."""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = PROJECT_ROOT / "data" / "config" / "cloudflare.toml"
EXAMPLE_CONFIG = PROJECT_ROOT / "config" / "cloudflare.example.toml"
PROJECT_SCRIPTS = PROJECT_ROOT / "scripts"
if str(PROJECT_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(PROJECT_SCRIPTS))

from credential_vault import VaultToolError, read_entry_secret  # noqa: E402


ZONE_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$", re.IGNORECASE)
RECORD_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$", re.IGNORECASE)
ACCOUNT_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$", re.IGNORECASE)
RECORD_TYPES = (
    "A",
    "AAAA",
    "CAA",
    "CERT",
    "CNAME",
    "DNSKEY",
    "DS",
    "HTTPS",
    "LOC",
    "MX",
    "NAPTR",
    "NS",
    "OPENPGPKEY",
    "PTR",
    "SMIMEA",
    "SRV",
    "SSHFP",
    "SVCB",
    "TLSA",
    "TXT",
    "URI",
)


class CloudflareToolError(Exception):
    """Erro seguro para apresentação ao agente ou usuário."""


class CloudflareApiError(CloudflareToolError):
    """Erro devolvido pela API da Cloudflare sem dados de autenticação."""

    def __init__(self, status: int | None, errors: list[dict[str, Any]]) -> None:
        self.status = status
        self.errors = errors
        messages = "; ".join(
            f"{error.get('code', 'unknown')}: {error.get('message', 'Erro da API')}"
            for error in errors
        )
        prefix = f"Cloudflare HTTP {status}" if status is not None else "Cloudflare"
        super().__init__(f"{prefix}: {messages or 'resposta sem detalhes'}")


@dataclass(frozen=True)
class CloudflareConfig:
    """Configuração não confidencial do cliente Cloudflare."""

    api_base: str
    credential_ref: str
    timeout_seconds: int


def load_config(path: Path) -> CloudflareConfig:
    """Carrega e valida a configuração TOML."""
    try:
        values = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CloudflareToolError(
            f"Configuração local não encontrada em '{path}'. Copie "
            f"'{EXAMPLE_CONFIG}' para "
            f"'{PROJECT_ROOT / 'data' / 'config' / 'cloudflare.toml'}'."
        ) from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise CloudflareToolError(
            f"Não foi possível carregar a configuração '{path}'."
        ) from exc

    api_base = str(values.get("api_base", "")).rstrip("/")
    credential_ref = str(values.get("credential_ref", "")).strip()
    timeout_seconds = values.get("timeout_seconds", 30)
    if not api_base.startswith("https://"):
        raise CloudflareToolError("'api_base' deve usar HTTPS.")
    if not credential_ref:
        raise CloudflareToolError("'credential_ref' não pode ficar vazio.")
    if not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= 120:
        raise CloudflareToolError("'timeout_seconds' deve estar entre 1 e 120.")
    return CloudflareConfig(api_base, credential_ref, timeout_seconds)


def safe_api_errors(payload: Any) -> list[dict[str, Any]]:
    """Extrai apenas código e mensagem dos erros da Cloudflare."""
    if not isinstance(payload, dict):
        return [{"code": "invalid_response", "message": "Resposta JSON inválida."}]
    errors = payload.get("errors")
    if not isinstance(errors, list):
        return [{"code": "unknown", "message": "Erro sem detalhes."}]
    return [
        {
            "code": error.get("code", "unknown"),
            "message": error.get("message", "Erro da API"),
        }
        for error in errors
        if isinstance(error, dict)
    ]


class CloudflareClient:
    """Cliente mínimo para a API v4 da Cloudflare."""

    def __init__(
        self,
        config: CloudflareConfig,
        token: str,
        *,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        if not token:
            raise CloudflareToolError("O token da Cloudflare está vazio.")
        self.config = config
        self._token = token
        self._opener = opener

    def close(self) -> None:
        """Descarta a referência ao token mantida pelo processo."""
        self._token = ""

    def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Executa uma chamada e valida o envelope padrão da API."""
        encoded_query = urllib.parse.urlencode(
            {
                key: value
                for key, value in (query or {}).items()
                if value is not None
            },
            doseq=True,
        )
        url = f"{self.config.api_base}{path}"
        if encoded_query:
            url = f"{url}?{encoded_query}"
        data = (
            json.dumps(body, ensure_ascii=False).encode("utf-8")
            if body is not None
            else None
        )
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "User-Agent": "BOTina-Cloudflare/1",
            },
        )
        try:
            with self._opener(
                request,
                timeout=self.config.timeout_seconds,
            ) as response:
                raw_payload = response.read()
                status = getattr(response, "status", 200)
        except urllib.error.HTTPError as exc:
            raw_payload = exc.read()
            try:
                payload = json.loads(raw_payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = None
            raise CloudflareApiError(exc.code, safe_api_errors(payload)) from None
        except urllib.error.URLError as exc:
            raise CloudflareToolError(
                f"Não foi possível conectar à Cloudflare: {exc.reason}"
            ) from None

        try:
            payload = json.loads(raw_payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CloudflareToolError(
                f"A Cloudflare devolveu JSON inválido (HTTP {status})."
            ) from exc
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise CloudflareApiError(status, safe_api_errors(payload))
        return payload

    def get_all(
        self,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        per_page: int = 100,
    ) -> list[dict[str, Any]]:
        """Percorre todas as páginas de um endpoint de listagem."""
        page = 1
        items: list[dict[str, Any]] = []
        while True:
            page_query = dict(query or {})
            page_query.update({"page": page, "per_page": per_page})
            payload = self.request("GET", path, query=page_query)
            result = payload.get("result")
            if not isinstance(result, list):
                raise CloudflareToolError("A API não devolveu uma lista válida.")
            items.extend(item for item in result if isinstance(item, dict))
            result_info = payload.get("result_info")
            total_pages = (
                result_info.get("total_pages")
                if isinstance(result_info, dict)
                else None
            )
            if isinstance(total_pages, int):
                if page >= total_pages:
                    break
            elif len(result) < per_page:
                break
            page += 1
        return items

    def verify_token(self) -> dict[str, Any]:
        """Valida o token configurado."""
        return self.request("GET", "/user/tokens/verify")["result"]

    def list_zones(
        self,
        *,
        name: str | None = None,
        account_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Lista zonas visíveis ao token."""
        return self.get_all(
            "/zones",
            query={"name": name, "account.id": account_id},
            per_page=50,
        )

    def get_zone(self, zone_id: str) -> dict[str, Any]:
        """Consulta uma zona pelo identificador."""
        return self.request("GET", f"/zones/{zone_id}")["result"]

    def create_zone(self, body: dict[str, Any]) -> dict[str, Any]:
        """Cria uma zona."""
        return self.request("POST", "/zones", body=body)["result"]

    def list_records(
        self,
        zone_id: str,
        *,
        name: str | None = None,
        record_type: str | None = None,
        proxied: bool | None = None,
    ) -> list[dict[str, Any]]:
        """Lista registros DNS de uma zona."""
        return self.get_all(
            f"/zones/{zone_id}/dns_records",
            query={
                "name.exact": name,
                "type": record_type,
                "proxied": str(proxied).lower() if proxied is not None else None,
            },
            per_page=100,
        )

    def get_record(self, zone_id: str, record_id: str) -> dict[str, Any]:
        """Consulta um registro DNS pelo identificador."""
        return self.request(
            "GET",
            f"/zones/{zone_id}/dns_records/{record_id}",
        )["result"]

    def create_record(
        self,
        zone_id: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        """Cria um registro DNS."""
        return self.request(
            "POST",
            f"/zones/{zone_id}/dns_records",
            body=body,
        )["result"]

    def update_record(
        self,
        zone_id: str,
        record_id: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        """Atualiza parcialmente um registro DNS."""
        return self.request(
            "PATCH",
            f"/zones/{zone_id}/dns_records/{record_id}",
            body=body,
        )["result"]

    def delete_record(self, zone_id: str, record_id: str) -> dict[str, Any]:
        """Exclui um registro DNS."""
        return self.request(
            "DELETE",
            f"/zones/{zone_id}/dns_records/{record_id}",
        )["result"]


def zone_summary(zone: dict[str, Any]) -> dict[str, Any]:
    """Seleciona campos úteis e não confidenciais de uma zona."""
    account = zone.get("account")
    return {
        "id": zone.get("id"),
        "name": zone.get("name"),
        "status": zone.get("status"),
        "paused": zone.get("paused"),
        "type": zone.get("type"),
        "account": (
            {"id": account.get("id"), "name": account.get("name")}
            if isinstance(account, dict)
            else None
        ),
        "name_servers": zone.get("name_servers"),
        "created_on": zone.get("created_on"),
        "modified_on": zone.get("modified_on"),
    }


def record_summary(record: dict[str, Any]) -> dict[str, Any]:
    """Seleciona os campos operacionais de um registro DNS."""
    keys = (
        "id",
        "type",
        "name",
        "content",
        "ttl",
        "priority",
        "proxied",
        "proxiable",
        "comment",
        "tags",
        "created_on",
        "modified_on",
    )
    return {key: record.get(key) for key in keys if key in record}


def normalize_zone_name(value: str) -> str:
    """Normaliza um nome de zona informado pelo usuário."""
    normalized = value.strip().rstrip(".").lower()
    if not normalized or "." not in normalized:
        raise CloudflareToolError("Informe um nome de zona completo.")
    return normalized


def canonical_record_name(value: str, zone_name: str) -> str:
    """Converte @ e nomes relativos em nomes DNS completos."""
    normalized = value.strip().rstrip(".").lower()
    if not normalized:
        raise CloudflareToolError("O nome do registro não pode ficar vazio.")
    if normalized == "@":
        return zone_name
    if normalized == zone_name or normalized.endswith(f".{zone_name}"):
        return normalized
    return f"{normalized}.{zone_name}"


def resolve_zone(
    client: CloudflareClient,
    zone_reference: str,
) -> dict[str, Any]:
    """Resolve de forma exata uma zona por ID ou nome."""
    reference = zone_reference.strip()
    if ZONE_ID_PATTERN.fullmatch(reference):
        return client.get_zone(reference)
    name = normalize_zone_name(reference)
    matches = [
        zone
        for zone in client.list_zones(name=name)
        if str(zone.get("name", "")).lower() == name
    ]
    if not matches:
        raise CloudflareToolError(f"Zona '{name}' não encontrada.")
    if len(matches) > 1:
        raise CloudflareToolError(
            f"A zona '{name}' é ambígua; informe o zone_id."
        )
    return matches[0]


def resolve_record(
    client: CloudflareClient,
    zone: dict[str, Any],
    *,
    record_id: str | None,
    name: str | None,
    record_type: str | None,
) -> dict[str, Any]:
    """Resolve um único registro por ID ou por nome e tipo."""
    zone_id = str(zone["id"])
    if record_id:
        if not RECORD_ID_PATTERN.fullmatch(record_id):
            raise CloudflareToolError("O record_id informado é inválido.")
        return client.get_record(zone_id, record_id)
    if not name:
        raise CloudflareToolError("Informe record_id ou nome do registro.")
    full_name = canonical_record_name(name, str(zone["name"]).lower())
    matches = [
        record
        for record in client.list_records(
            zone_id,
            name=full_name,
            record_type=record_type,
        )
        if str(record.get("name", "")).lower() == full_name
        and (record_type is None or record.get("type") == record_type)
    ]
    if not matches:
        raise CloudflareToolError(
            f"Registro '{full_name}' não encontrado na zona '{zone['name']}'."
        )
    if len(matches) > 1:
        candidates = ", ".join(
            f"{record.get('id')}:{record.get('type')}" for record in matches
        )
        raise CloudflareToolError(
            f"O registro '{full_name}' é ambíguo ({candidates}); informe record_id."
        )
    return matches[0]


def record_selector_arguments(parser: argparse.ArgumentParser) -> None:
    """Adiciona os argumentos usados para selecionar um registro."""
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--record-id")
    selector.add_argument("--name")
    parser.add_argument("--type", dest="record_type", choices=RECORD_TYPES)


def add_proxy_choice(
    parser: argparse.ArgumentParser,
    *,
    required: bool,
) -> None:
    """Adiciona opções mutuamente exclusivas para proxy."""
    proxy = parser.add_mutually_exclusive_group(required=required)
    proxy.add_argument("--proxied", dest="proxied", action="store_true")
    proxy.add_argument("--dns-only", dest="proxied", action="store_false")
    parser.set_defaults(proxied=None)


def execute_doctor(
    client: CloudflareClient,
    _: argparse.Namespace,
) -> dict[str, Any]:
    """Valida o token e tenta listar as zonas disponíveis."""
    token = client.verify_token()
    result: dict[str, Any] = {
        "ok": True,
        "token": {
            "status": token.get("status"),
            "expires_on": token.get("expires_on"),
            "not_before": token.get("not_before"),
        },
    }
    try:
        zones = client.list_zones()
        result["zone_read"] = True
        result["zone_count"] = len(zones)
    except CloudflareApiError as exc:
        result["zone_read"] = False
        result["zone_error"] = str(exc)
    return result


def execute_zones_list(
    client: CloudflareClient,
    args: argparse.Namespace,
) -> dict[str, Any]:
    """Lista zonas."""
    zones = client.list_zones(name=args.name, account_id=args.account_id)
    return {
        "ok": True,
        "count": len(zones),
        "zones": [zone_summary(zone) for zone in zones],
    }


def execute_zones_create(
    client: CloudflareClient,
    args: argparse.Namespace,
) -> dict[str, Any]:
    """Cria uma zona ou retorna a zona existente."""
    zone_name = normalize_zone_name(args.name)
    if not ACCOUNT_ID_PATTERN.fullmatch(args.account_id):
        raise CloudflareToolError("O account_id informado é inválido.")
    existing = [
        zone
        for zone in client.list_zones(name=zone_name)
        if str(zone.get("name", "")).lower() == zone_name
    ]
    if existing:
        in_requested_account = [
            zone
            for zone in existing
            if isinstance(zone.get("account"), dict)
            and zone["account"].get("id") == args.account_id
        ]
        if len(in_requested_account) == 1:
            return {
                "ok": True,
                "changed": False,
                "reason": "zone_already_exists",
                "zone": zone_summary(in_requested_account[0]),
            }
        if len(in_requested_account) > 1:
            raise CloudflareToolError(
                f"A zona '{zone_name}' está duplicada na conta informada."
            )
        raise CloudflareToolError(
            f"A zona '{zone_name}' já existe, mas não na conta informada."
        )
    body = {
        "account": {"id": args.account_id},
        "name": zone_name,
        "type": args.zone_type,
    }
    if args.dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "operation": "zones.create",
            "request": body,
        }
    created = client.create_zone(body)
    return {"ok": True, "changed": True, "zone": zone_summary(created)}


def execute_dns_list(
    client: CloudflareClient,
    args: argparse.Namespace,
) -> dict[str, Any]:
    """Lista registros DNS."""
    zone = resolve_zone(client, args.zone)
    name = (
        canonical_record_name(args.name, str(zone["name"]).lower())
        if args.name
        else None
    )
    records = client.list_records(
        str(zone["id"]),
        name=name,
        record_type=args.record_type,
        proxied=args.proxied,
    )
    return {
        "ok": True,
        "zone": zone_summary(zone),
        "count": len(records),
        "records": [record_summary(record) for record in records],
    }


def execute_dns_show(
    client: CloudflareClient,
    args: argparse.Namespace,
) -> dict[str, Any]:
    """Mostra um registro DNS único."""
    zone = resolve_zone(client, args.zone)
    record = resolve_record(
        client,
        zone,
        record_id=args.record_id,
        name=args.name,
        record_type=args.record_type,
    )
    return {
        "ok": True,
        "zone": zone_summary(zone),
        "record": record_summary(record),
    }


def requested_record_body(
    args: argparse.Namespace,
    *,
    zone_name: str,
) -> dict[str, Any]:
    """Monta o corpo de criação de registro sem campos ausentes."""
    body: dict[str, Any] = {
        "type": args.record_type,
        "name": canonical_record_name(args.name, zone_name),
        "content": args.content,
        "ttl": args.ttl,
    }
    if args.proxied is not None:
        body["proxied"] = args.proxied
    if args.priority is not None:
        body["priority"] = args.priority
    if args.comment is not None:
        body["comment"] = args.comment
    return body


def execute_dns_create(
    client: CloudflareClient,
    args: argparse.Namespace,
) -> dict[str, Any]:
    """Cria um registro DNS com proteção contra duplicação idêntica."""
    zone = resolve_zone(client, args.zone)
    zone_id = str(zone["id"])
    body = requested_record_body(
        args,
        zone_name=str(zone["name"]).lower(),
    )
    existing = [
        record
        for record in client.list_records(
            zone_id,
            name=body["name"],
            record_type=body["type"],
        )
        if str(record.get("name", "")).lower() == body["name"]
        and record.get("type") == body["type"]
        and str(record.get("content")) == str(body["content"])
    ]
    if len(existing) == 1:
        comparable_fields = ("ttl", "proxied", "priority", "comment")
        differs = any(
            field in body and existing[0].get(field) != body.get(field)
            for field in comparable_fields
        )
        if differs:
            raise CloudflareToolError(
                "Já existe registro com mesmo tipo, nome e conteúdo, mas com "
                "outras propriedades. Use 'dns update'."
            )
        return {
            "ok": True,
            "changed": False,
            "reason": "record_already_exists",
            "record": record_summary(existing[0]),
        }
    if len(existing) > 1:
        raise CloudflareToolError(
            "Existem registros duplicados; informe um record_id após revisá-los."
        )
    if args.dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "operation": "dns.create",
            "zone": zone_summary(zone),
            "request": body,
        }
    created = client.create_record(zone_id, body)
    return {
        "ok": True,
        "changed": True,
        "zone": zone_summary(zone),
        "record": record_summary(created),
    }


def update_body(args: argparse.Namespace) -> dict[str, Any]:
    """Seleciona somente campos explicitamente informados para PATCH."""
    values = {
        "content": args.content,
        "ttl": args.ttl,
        "comment": args.comment,
        "proxied": args.proxied,
    }
    return {key: value for key, value in values.items() if value is not None}


def execute_dns_update(
    client: CloudflareClient,
    args: argparse.Namespace,
) -> dict[str, Any]:
    """Atualiza parcialmente um registro DNS."""
    zone = resolve_zone(client, args.zone)
    record = resolve_record(
        client,
        zone,
        record_id=args.record_id,
        name=args.name,
        record_type=args.record_type,
    )
    body = update_body(args)
    if not body:
        raise CloudflareToolError("Informe pelo menos um campo para atualização.")
    changes = {
        key: {"before": record.get(key), "after": value}
        for key, value in body.items()
        if record.get(key) != value
    }
    if not changes:
        return {
            "ok": True,
            "changed": False,
            "reason": "already_in_desired_state",
            "record": record_summary(record),
        }
    if args.dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "operation": "dns.update",
            "zone": zone_summary(zone),
            "record_id": record["id"],
            "changes": changes,
        }
    updated = client.update_record(str(zone["id"]), str(record["id"]), body)
    return {
        "ok": True,
        "changed": True,
        "zone": zone_summary(zone),
        "record": record_summary(updated),
    }


def execute_dns_proxy(
    client: CloudflareClient,
    args: argparse.Namespace,
) -> dict[str, Any]:
    """Liga ou desliga o proxy de um registro proxiable."""
    zone = resolve_zone(client, args.zone)
    record = resolve_record(
        client,
        zone,
        record_id=args.record_id,
        name=args.name,
        record_type=args.record_type,
    )
    if record.get("proxiable") is not True:
        raise CloudflareToolError(
            f"O registro '{record.get('name')}' não aceita proxy da Cloudflare."
        )
    if record.get("proxied") is args.enabled:
        return {
            "ok": True,
            "changed": False,
            "reason": "already_in_desired_state",
            "record": record_summary(record),
        }
    if args.dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "operation": "dns.proxy",
            "zone": zone_summary(zone),
            "record_id": record["id"],
            "before": record.get("proxied"),
            "after": args.enabled,
        }
    updated = client.update_record(
        str(zone["id"]),
        str(record["id"]),
        {"proxied": args.enabled},
    )
    return {
        "ok": True,
        "changed": True,
        "zone": zone_summary(zone),
        "record": record_summary(updated),
    }


def execute_dns_delete(
    client: CloudflareClient,
    args: argparse.Namespace,
) -> dict[str, Any]:
    """Exclui um registro DNS resolvido de forma única."""
    zone = resolve_zone(client, args.zone)
    record = resolve_record(
        client,
        zone,
        record_id=args.record_id,
        name=args.name,
        record_type=args.record_type,
    )
    if args.dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "operation": "dns.delete",
            "zone": zone_summary(zone),
            "record": record_summary(record),
        }
    result = client.delete_record(str(zone["id"]), str(record["id"]))
    return {
        "ok": True,
        "changed": True,
        "deleted_record_id": result.get("id", record["id"]),
        "zone": zone_summary(zone),
    }


def add_dry_run(parser: argparse.ArgumentParser) -> None:
    """Adiciona simulação opcional a uma operação de escrita."""
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Valida e mostra a alteração sem enviá-la à Cloudflare.",
    )


def build_parser() -> argparse.ArgumentParser:
    """Constrói a CLI da skill."""
    parser = argparse.ArgumentParser(
        description="Gerencia zonas e registros DNS da Cloudflare."
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    commands = parser.add_subparsers(dest="resource", required=True)

    doctor = commands.add_parser("doctor", help="Valida token e acesso de leitura.")
    doctor.set_defaults(handler=execute_doctor)

    zones = commands.add_parser("zones", help="Gerencia zonas.")
    zone_commands = zones.add_subparsers(dest="operation", required=True)
    zones_list = zone_commands.add_parser("list", help="Lista zonas.")
    zones_list.add_argument("--name")
    zones_list.add_argument("--account-id")
    zones_list.set_defaults(handler=execute_zones_list)

    zones_create = zone_commands.add_parser("create", help="Cria uma zona.")
    zones_create.add_argument("--name", required=True)
    zones_create.add_argument("--account-id", required=True)
    zones_create.add_argument(
        "--type",
        dest="zone_type",
        choices=("full", "partial", "secondary", "internal"),
        default="full",
    )
    add_dry_run(zones_create)
    zones_create.set_defaults(handler=execute_zones_create)

    dns = commands.add_parser("dns", help="Gerencia registros DNS.")
    dns_commands = dns.add_subparsers(dest="operation", required=True)

    dns_list = dns_commands.add_parser("list", help="Lista registros.")
    dns_list.add_argument("--zone", required=True)
    dns_list.add_argument("--name")
    dns_list.add_argument("--type", dest="record_type", choices=RECORD_TYPES)
    add_proxy_choice(dns_list, required=False)
    dns_list.set_defaults(handler=execute_dns_list)

    dns_show = dns_commands.add_parser("show", help="Mostra um registro.")
    dns_show.add_argument("--zone", required=True)
    record_selector_arguments(dns_show)
    dns_show.set_defaults(handler=execute_dns_show)

    dns_create = dns_commands.add_parser("create", help="Cria um registro.")
    dns_create.add_argument("--zone", required=True)
    dns_create.add_argument("--type", dest="record_type", choices=RECORD_TYPES, required=True)
    dns_create.add_argument("--name", required=True)
    dns_create.add_argument("--content", required=True)
    dns_create.add_argument("--ttl", type=int, default=1)
    dns_create.add_argument("--priority", type=int)
    dns_create.add_argument("--comment")
    add_proxy_choice(dns_create, required=False)
    add_dry_run(dns_create)
    dns_create.set_defaults(handler=execute_dns_create)

    dns_update = dns_commands.add_parser("update", help="Atualiza um registro.")
    dns_update.add_argument("--zone", required=True)
    record_selector_arguments(dns_update)
    dns_update.add_argument("--content")
    dns_update.add_argument("--ttl", type=int)
    dns_update.add_argument("--comment")
    add_proxy_choice(dns_update, required=False)
    add_dry_run(dns_update)
    dns_update.set_defaults(handler=execute_dns_update)

    dns_proxy = dns_commands.add_parser("proxy", help="Altera o proxy do registro.")
    dns_proxy.add_argument("--zone", required=True)
    record_selector_arguments(dns_proxy)
    proxy_state = dns_proxy.add_mutually_exclusive_group(required=True)
    proxy_state.add_argument("--enable", dest="enabled", action="store_true")
    proxy_state.add_argument("--disable", dest="enabled", action="store_false")
    add_dry_run(dns_proxy)
    dns_proxy.set_defaults(handler=execute_dns_proxy)

    dns_delete = dns_commands.add_parser("delete", help="Exclui um registro.")
    dns_delete.add_argument("--zone", required=True)
    record_selector_arguments(dns_delete)
    add_dry_run(dns_delete)
    dns_delete.set_defaults(handler=execute_dns_delete)

    return parser


def print_json(payload: Any, *, stream: Any = sys.stdout) -> None:
    """Imprime JSON UTF-8 para consumo por agentes."""
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), file=stream)


def main() -> int:
    """Carrega a credencial, executa o comando e sanitiza falhas."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    args = build_parser().parse_args()
    client: CloudflareClient | None = None
    try:
        config = load_config(Path(args.config).expanduser().resolve())
        token = read_entry_secret(config.credential_ref)
        client = CloudflareClient(config, token)
        token = ""
        result = args.handler(client, args)
    except (CloudflareToolError, CloudflareApiError, VaultToolError, OSError) as exc:
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
