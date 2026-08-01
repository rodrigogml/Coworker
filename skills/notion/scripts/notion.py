#!/usr/bin/env python3
"""Busca e edita páginas do Notion sem expor o token."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = PROJECT_ROOT / "data" / "config" / "notion.toml"
EXAMPLE_CONFIG = PROJECT_ROOT / "config" / "notion.example.toml"
PROJECT_SCRIPTS = PROJECT_ROOT / "scripts"
if str(PROJECT_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(PROJECT_SCRIPTS))

from credential_vault import VaultToolError, read_entry_secret  # noqa: E402
from integration_profiles import (  # noqa: E402
    IntegrationProfileError,
    resolve_credential_ref,
)


ALLOWED_API_HOST = "api.notion.com"
SUPPORTED_API_VERSION = "2026-03-11"
PAGE_ID_PATTERN = re.compile(
    r"^(?:[0-9a-fA-F]{32}|"
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$"
)
MAX_MARKDOWN_BYTES = 480_000
MAX_CHANGES = 100
SENSITIVE_KEYS = {
    "access_token",
    "authorization",
    "client_secret",
    "password",
    "secret",
    "token",
}


class NotionToolError(Exception):
    """Erro seguro para apresentação ao agente ou à pessoa usuária."""


class NotionApiError(NotionToolError):
    """Erro sanitizado devolvido pela API do Notion."""

    def __init__(self, status: int | None, code: str, message: str) -> None:
        self.status = status
        self.code = code
        prefix = f"Notion HTTP {status}" if status is not None else "Notion"
        super().__init__(f"{prefix} ({code}): {message}")


@dataclass(frozen=True)
class NotionConfig:
    """Configuração local não confidencial."""

    api_base: str
    api_version: str
    credential_ref: str
    timeout_seconds: int
    page_size: int
    max_pages: int
    scan_max_pages: int
    request_interval_seconds: float


def load_config(path: Path, profile: str | None = None) -> NotionConfig:
    """Carrega e valida a configuração TOML."""
    try:
        values = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise NotionToolError(
            f"Configuração local não encontrada em '{path}'. Copie "
            f"'{EXAMPLE_CONFIG}' para '{DEFAULT_CONFIG}'."
        ) from exc
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise NotionToolError(
            f"Não foi possível carregar a configuração '{path}'."
        ) from exc

    api_base = str(values.get("api_base", "")).rstrip("/")
    parsed = urllib.parse.urlparse(api_base)
    try:
        port = parsed.port
    except ValueError as exc:
        raise NotionToolError("'api_base' contém uma porta inválida.") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != ALLOWED_API_HOST
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path.rstrip("/") != "/v1"
        or parsed.query
        or parsed.fragment
    ):
        raise NotionToolError(
            f"'api_base' deve ser 'https://{ALLOWED_API_HOST}/v1'."
        )

    api_version = str(values.get("api_version", "")).strip()
    try:
        _, credential_ref = resolve_credential_ref(values, profile)
    except IntegrationProfileError as exc:
        raise NotionToolError(str(exc)) from exc
    timeout_seconds = values.get("timeout_seconds", 30)
    page_size = values.get("page_size", 50)
    max_pages = values.get("max_pages", 20)
    scan_max_pages = values.get("scan_max_pages", 10)
    request_interval_seconds = values.get("request_interval_seconds", 0.35)
    if api_version != SUPPORTED_API_VERSION:
        raise NotionToolError(
            f"'api_version' deve ser '{SUPPORTED_API_VERSION}' nesta versão da skill."
        )
    if not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= 120:
        raise NotionToolError("'timeout_seconds' deve estar entre 1 e 120.")
    if not isinstance(page_size, int) or not 1 <= page_size <= 100:
        raise NotionToolError("'page_size' deve estar entre 1 e 100.")
    if not isinstance(max_pages, int) or not 1 <= max_pages <= 100:
        raise NotionToolError("'max_pages' deve estar entre 1 e 100.")
    if not isinstance(scan_max_pages, int) or not 1 <= scan_max_pages <= 100:
        raise NotionToolError("'scan_max_pages' deve estar entre 1 e 100.")
    if (
        not isinstance(request_interval_seconds, (int, float))
        or isinstance(request_interval_seconds, bool)
        or not 0 <= request_interval_seconds <= 5
    ):
        raise NotionToolError(
            "'request_interval_seconds' deve estar entre 0 e 5."
        )
    return NotionConfig(
        api_base,
        api_version,
        credential_ref,
        timeout_seconds,
        page_size,
        max_pages,
        scan_max_pages,
        float(request_interval_seconds),
    )


def validate_page_id(value: str, field: str = "id") -> str:
    """Valida um ID de página antes de inseri-lo na URL."""
    normalized = str(value).strip()
    if not PAGE_ID_PATTERN.fullmatch(normalized):
        raise NotionToolError(f"'{field}' contém um ID de página inválido.")
    return normalized


def validate_required_text(value: str, field: str, maximum: int) -> str:
    """Valida um texto obrigatório."""
    normalized = str(value).strip()
    if not normalized:
        raise NotionToolError(f"'{field}' não pode ficar vazio.")
    if len(normalized) > maximum:
        raise NotionToolError(
            f"'{field}' deve ter no máximo {maximum} caracteres."
        )
    return normalized


def sanitize_payload(value: Any, secret: str | None = None) -> Any:
    """Remove campos confidenciais de estruturas devolvidas pela API."""
    if isinstance(value, dict):
        return {
            str(key): sanitize_payload(item, secret)
            for key, item in value.items()
            if str(key).lower() not in SENSITIVE_KEYS
        }
    if isinstance(value, list):
        return [sanitize_payload(item, secret) for item in value]
    if isinstance(value, str) and secret:
        return value.replace(secret, "[REDACTED]")
    return value


def safe_error(payload: Any) -> tuple[str, str]:
    """Extrai código e mensagem curtos de uma falha."""
    if not isinstance(payload, dict):
        return "api_error", "A API recusou a operação sem uma mensagem segura."
    code = payload.get("code")
    message = payload.get("message")
    safe_code = code.strip()[:100] if isinstance(code, str) else "api_error"
    safe_message = (
        message.strip()[:500]
        if isinstance(message, str) and message.strip()
        else "A API recusou a operação sem uma mensagem segura."
    )
    return safe_code, safe_message


class NotionClient:
    """Cliente mínimo para os endpoints permitidos da API do Notion."""

    def __init__(
        self,
        config: NotionConfig,
        token: str,
        *,
        opener: Callable[..., Any] = urllib.request.urlopen,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not token:
            raise NotionToolError("O token do Notion está vazio.")
        self.config = config
        self._token = token
        self._opener = opener
        self._sleeper = sleeper
        self._clock = clock
        self._last_request_at: float | None = None

    def close(self) -> None:
        """Descarta a referência ao token."""
        self._token = ""

    def _throttle(self) -> None:
        if self._last_request_at is not None:
            elapsed = self._clock() - self._last_request_at
            remaining = self.config.request_interval_seconds - elapsed
            if remaining > 0:
                self._sleeper(remaining)
        self._last_request_at = self._clock()

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        """Executa uma chamada fechada e devolve JSON sanitizado."""
        if method not in {"GET", "POST", "PATCH"}:
            raise NotionToolError("Método interno da API inválido.")
        if not path.startswith("/") or ".." in path or "?" in path or "#" in path:
            raise NotionToolError("Caminho interno da API inválido.")
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token}",
            "Notion-Version": self.config.api_version,
            "User-Agent": "Coworker-Notion/1.0",
        }
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.config.api_base}{path}",
            data=body,
            headers=headers,
            method=method,
        )
        self._throttle()
        try:
            with self._opener(
                request,
                timeout=self.config.timeout_seconds,
            ) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                error_payload = json.loads(raw.decode("utf-8")) if raw else {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                error_payload = {}
            code, message = safe_error(error_payload)
            code = code.replace(self._token, "[REDACTED]")
            message = message.replace(self._token, "[REDACTED]")
            raise NotionApiError(exc.code, code, message) from exc
        except urllib.error.URLError as exc:
            raise NotionApiError(
                None,
                "network_error",
                "Falha de comunicação com a API.",
            ) from exc
        if not raw:
            return None
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NotionApiError(
                None,
                "invalid_response",
                "A API devolveu uma resposta inválida.",
            ) from exc
        return sanitize_payload(decoded, self._token)


def extract_title(page: dict[str, Any]) -> str:
    """Obtém o título sem depender do nome da propriedade."""
    properties = page.get("properties")
    if isinstance(properties, dict):
        for prop in properties.values():
            if not isinstance(prop, dict) or prop.get("type") != "title":
                continue
            title = prop.get("title")
            if isinstance(title, list):
                parts = []
                for item in title:
                    if not isinstance(item, dict):
                        continue
                    plain = item.get("plain_text")
                    if isinstance(plain, str):
                        parts.append(plain)
                        continue
                    text = item.get("text")
                    if isinstance(text, dict) and isinstance(text.get("content"), str):
                        parts.append(text["content"])
                return "".join(parts).strip()
    return ""


def page_summary(page: dict[str, Any]) -> dict[str, Any]:
    """Reduz uma página aos metadados úteis para seleção."""
    return {
        "id": page.get("id"),
        "title": extract_title(page),
        "url": page.get("url"),
        "created_time": page.get("created_time"),
        "last_edited_time": page.get("last_edited_time"),
        "in_trash": page.get("in_trash", page.get("archived")),
    }


def search_pages(
    client: NotionClient,
    *,
    query: str | None,
    max_pages: int,
    start_cursor: str | None = None,
    result_limit: int | None = None,
) -> dict[str, Any]:
    """Pesquisa e pagina páginas por título."""
    results: list[dict[str, Any]] = []
    cursor = start_cursor
    pages_read = 0
    has_more = False
    limited = False
    while pages_read < max_pages:
        payload: dict[str, Any] = {
            "filter": {"property": "object", "value": "page"},
            "page_size": client.config.page_size,
        }
        if query:
            payload["query"] = query
        if cursor:
            payload["start_cursor"] = cursor
        response = client.request("POST", "/search", payload=payload)
        if not isinstance(response, dict):
            raise NotionApiError(
                None,
                "invalid_response",
                "A busca não devolveu um objeto JSON.",
            )
        raw_results = response.get("results")
        if not isinstance(raw_results, list):
            raise NotionApiError(
                None,
                "invalid_response",
                "A busca não devolveu uma lista de resultados.",
            )
        results.extend(
            page_summary(item) for item in raw_results if isinstance(item, dict)
        )
        pages_read += 1
        has_more = bool(response.get("has_more"))
        next_cursor = response.get("next_cursor")
        cursor = next_cursor if isinstance(next_cursor, str) else None
        if result_limit is not None and len(results) >= result_limit:
            limited = len(results) > result_limit or has_more
            break
        if not has_more or not cursor:
            break
    if result_limit is not None:
        results = results[:result_limit]
    return {
        "results": results,
        "pagination": {
            "pages_read": pages_read,
            "has_more": has_more,
            "next_cursor": cursor,
            "truncated": bool(limited or (has_more and cursor)),
        },
    }


def read_markdown_file(path_value: str) -> tuple[str, dict[str, Any]]:
    """Lê Markdown UTF-8 com limite e devolve metadados seguros."""
    path = Path(path_value).expanduser().resolve()
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise NotionToolError(
            f"Não foi possível ler o arquivo Markdown '{path}'."
        ) from exc
    if len(raw) > MAX_MARKDOWN_BYTES:
        raise NotionToolError(
            f"O arquivo Markdown deve ter no máximo {MAX_MARKDOWN_BYTES} bytes."
        )
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise NotionToolError("O arquivo Markdown deve usar UTF-8.") from exc
    return content, {
        "source": str(path),
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def read_changes_file(path_value: str) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Lê e valida substituições pontuais sem devolvê-las na prévia."""
    path = Path(path_value).expanduser().resolve()
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise NotionToolError(
            f"Não foi possível ler o arquivo de alterações '{path}'."
        ) from exc
    if len(raw) > MAX_MARKDOWN_BYTES:
        raise NotionToolError("O arquivo de alterações é muito grande.")
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NotionToolError(
            "O arquivo de alterações deve conter JSON UTF-8 válido."
        ) from exc
    if not isinstance(decoded, list) or not 1 <= len(decoded) <= MAX_CHANGES:
        raise NotionToolError(
            f"O arquivo deve conter de 1 a {MAX_CHANGES} alterações."
        )
    changes: list[dict[str, str]] = []
    for index, item in enumerate(decoded, start=1):
        if not isinstance(item, dict) or set(item) != {"old_str", "new_str"}:
            raise NotionToolError(
                f"A alteração {index} deve conter apenas 'old_str' e 'new_str'."
            )
        old_str = item.get("old_str")
        new_str = item.get("new_str")
        if not isinstance(old_str, str) or not old_str:
            raise NotionToolError(
                f"'old_str' da alteração {index} não pode ficar vazio."
            )
        if not isinstance(new_str, str):
            raise NotionToolError(
                f"'new_str' da alteração {index} deve ser texto."
            )
        changes.append({"old_str": old_str, "new_str": new_str})
    return changes, {
        "source": str(path),
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "changes": len(changes),
    }


def write_summary(response: Any) -> dict[str, Any]:
    """Resume uma escrita sem devolver o conteúdo completo da nota."""
    if not isinstance(response, dict):
        return {"ok": True}
    return {
        "ok": True,
        "object": response.get("object"),
        "id": response.get("id"),
        "url": response.get("url"),
        "truncated": response.get("truncated"),
        "unknown_block_ids": response.get("unknown_block_ids", []),
    }


def doctor(client: NotionClient, _args: argparse.Namespace) -> dict[str, Any]:
    response = client.request("GET", "/users/me")
    if not isinstance(response, dict):
        raise NotionApiError(
            None,
            "invalid_response",
            "O diagnóstico não devolveu um usuário.",
        )
    return {
        "ok": True,
        "integration": {
            "id": response.get("id"),
            "name": response.get("name"),
            "type": response.get("type"),
        },
        "api_version": client.config.api_version,
    }


def pages_search(client: NotionClient, args: argparse.Namespace) -> dict[str, Any]:
    query = (
        validate_required_text(args.query, "query", 500)
        if args.query is not None
        else None
    )
    cursor = validate_page_id(args.cursor, "cursor") if args.cursor else None
    max_pages = client.config.max_pages if args.all_pages else 1
    return search_pages(
        client,
        query=query,
        max_pages=max_pages,
        start_cursor=cursor,
    )


def pages_get(client: NotionClient, args: argparse.Namespace) -> dict[str, Any]:
    page_id = validate_page_id(args.id)
    response = client.request("GET", f"/pages/{page_id}/markdown")
    if not isinstance(response, dict):
        raise NotionApiError(
            None,
            "invalid_response",
            "A leitura não devolveu Markdown.",
        )
    return response


def make_snippet(content: str, start: int, end: int, radius: int = 100) -> str:
    """Cria trecho curto e legível ao redor de uma ocorrência."""
    left = max(0, start - radius)
    right = min(len(content), end + radius)
    snippet = content[left:right].replace("\r", " ").replace("\n", " ")
    snippet = re.sub(r"\s+", " ", snippet).strip()
    return f"{'…' if left else ''}{snippet}{'…' if right < len(content) else ''}"


def pages_find_content(
    client: NotionClient,
    args: argparse.Namespace,
) -> dict[str, Any]:
    needle = validate_required_text(args.query, "query", 500)
    max_pages = args.max_pages or client.config.scan_max_pages
    if not 1 <= max_pages <= client.config.scan_max_pages:
        raise NotionToolError(
            f"'max-pages' deve estar entre 1 e {client.config.scan_max_pages}."
        )
    candidates = search_pages(
        client,
        query=(
            validate_required_text(args.title, "title", 500)
            if args.title is not None
            else None
        ),
        max_pages=client.config.max_pages,
        result_limit=max_pages,
    )
    matches = []
    pages_scanned = 0
    incomplete_pages = 0
    comparison_needle = needle if args.case_sensitive else needle.casefold()
    for page in candidates["results"]:
        if pages_scanned >= max_pages:
            break
        page_id = page.get("id")
        if not isinstance(page_id, str):
            continue
        response = client.request("GET", f"/pages/{validate_page_id(page_id)}/markdown")
        pages_scanned += 1
        if not isinstance(response, dict):
            continue
        markdown = response.get("markdown")
        if not isinstance(markdown, str):
            continue
        incomplete = bool(response.get("truncated") or response.get("unknown_block_ids"))
        incomplete_pages += int(incomplete)
        haystack = markdown if args.case_sensitive else markdown.casefold()
        index = haystack.find(comparison_needle)
        if index >= 0:
            matches.append(
                {
                    **page,
                    "snippet": make_snippet(
                        markdown,
                        index,
                        index + len(needle),
                    ),
                    "content_incomplete": incomplete,
                }
            )
    total_candidates = len(candidates["results"])
    return {
        "matches": matches,
        "scan": {
            "pages_scanned": pages_scanned,
            "candidate_pages_loaded": total_candidates,
            "candidate_search_truncated": candidates["pagination"]["truncated"],
            "scan_truncated": total_candidates > pages_scanned,
            "incomplete_pages": incomplete_pages,
        },
    }


def pages_create(client: NotionClient, args: argparse.Namespace) -> dict[str, Any]:
    parent_id = validate_page_id(args.parent_page_id, "parent-page-id")
    title = validate_required_text(args.title, "title", 2000)
    markdown, metadata = read_markdown_file(args.markdown_file)
    payload = {
        "parent": {"page_id": parent_id},
        "properties": {
            "title": {
                "title": [{"type": "text", "text": {"content": title}}],
            }
        },
        "markdown": markdown,
    }
    if args.dry_run:
        return {
            "dry_run": True,
            "request": {
                "method": "POST",
                "path": "/pages",
                "parent_page_id": parent_id,
                "title": title,
                "content": metadata,
            },
        }
    return write_summary(client.request("POST", "/pages", payload=payload))


def pages_edit(client: NotionClient, args: argparse.Namespace) -> dict[str, Any]:
    page_id = validate_page_id(args.id)
    changes, metadata = read_changes_file(args.changes_file)
    payload = {
        "type": "update_content",
        "update_content": {"content_updates": changes},
    }
    if args.dry_run:
        return {
            "dry_run": True,
            "request": {
                "method": "PATCH",
                "path": f"/pages/{page_id}/markdown",
                "changes": metadata,
            },
        }
    return write_summary(
        client.request("PATCH", f"/pages/{page_id}/markdown", payload=payload)
    )


def pages_replace(client: NotionClient, args: argparse.Namespace) -> dict[str, Any]:
    page_id = validate_page_id(args.id)
    markdown, metadata = read_markdown_file(args.markdown_file)
    payload = {
        "type": "replace_content",
        "replace_content": {"new_str": markdown},
    }
    if args.dry_run:
        return {
            "dry_run": True,
            "request": {
                "method": "PATCH",
                "path": f"/pages/{page_id}/markdown",
                "content": metadata,
            },
        }
    return write_summary(
        client.request("PATCH", f"/pages/{page_id}/markdown", payload=payload)
    )


def pages_trash_state(
    client: NotionClient,
    args: argparse.Namespace,
    *,
    in_trash: bool,
) -> dict[str, Any]:
    page_id = validate_page_id(args.id)
    payload = {"in_trash": in_trash}
    if args.dry_run:
        return {
            "dry_run": True,
            "request": {
                "method": "PATCH",
                "path": f"/pages/{page_id}",
                "payload": payload,
            },
        }
    return write_summary(client.request("PATCH", f"/pages/{page_id}", payload=payload))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Busca e edita páginas do Notion sem expor credenciais."
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--profile")
    commands = parser.add_subparsers(dest="command", required=True)
    doctor_parser = commands.add_parser("doctor")
    doctor_parser.set_defaults(handler=doctor)

    pages_parser = commands.add_parser("pages")
    pages = pages_parser.add_subparsers(dest="pages_command", required=True)
    search = pages.add_parser("search")
    search.add_argument("--query")
    search.add_argument("--cursor")
    search.add_argument("--all-pages", action="store_true")
    search.set_defaults(handler=pages_search)

    get = pages.add_parser("get")
    get.add_argument("--id", required=True)
    get.set_defaults(handler=pages_get)

    find_content = pages.add_parser("find-content")
    find_content.add_argument("--query", required=True)
    find_content.add_argument("--title")
    find_content.add_argument("--max-pages", type=int)
    find_content.add_argument("--case-sensitive", action="store_true")
    find_content.set_defaults(handler=pages_find_content)

    create = pages.add_parser("create")
    create.add_argument("--parent-page-id", required=True)
    create.add_argument("--title", required=True)
    create.add_argument("--markdown-file", required=True)
    create.add_argument("--dry-run", action="store_true")
    create.set_defaults(handler=pages_create)

    edit = pages.add_parser("edit")
    edit.add_argument("--id", required=True)
    edit.add_argument("--changes-file", required=True)
    edit.add_argument("--dry-run", action="store_true")
    edit.set_defaults(handler=pages_edit)

    replace = pages.add_parser("replace")
    replace.add_argument("--id", required=True)
    replace.add_argument("--markdown-file", required=True)
    replace.add_argument("--dry-run", action="store_true")
    replace.set_defaults(handler=pages_replace)

    trash = pages.add_parser("trash")
    trash.add_argument("--id", required=True)
    trash.add_argument("--dry-run", action="store_true")
    trash.set_defaults(
        handler=lambda client, args: pages_trash_state(
            client,
            args,
            in_trash=True,
        )
    )
    restore = pages.add_parser("restore")
    restore.add_argument("--id", required=True)
    restore.add_argument("--dry-run", action="store_true")
    restore.set_defaults(
        handler=lambda client, args: pages_trash_state(
            client,
            args,
            in_trash=False,
        )
    )
    return parser


def print_json(value: Any, *, stream: Any = sys.stdout) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2), file=stream)


def main() -> int:
    """Carrega o token, executa a operação e sanitiza falhas."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    args = build_parser().parse_args()
    client: NotionClient | None = None
    try:
        config = load_config(Path(args.config).expanduser().resolve(), args.profile)
        token = read_entry_secret(config.credential_ref)
        client = NotionClient(config, token)
        token = ""
        result = args.handler(client, args)
    except (NotionToolError, VaultToolError, OSError) as exc:
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
