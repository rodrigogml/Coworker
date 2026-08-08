#!/usr/bin/env python3
"""Busca e organiza Gmail usando perfis OAuth sem expor tokens."""

from __future__ import annotations

import argparse
import base64
import hashlib
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
DEFAULT_CONFIG = PROJECT_ROOT / "data" / "config" / "gmail.toml"
EXAMPLE_CONFIG = PROJECT_ROOT / "config" / "gmail.example.toml"
PROJECT_SCRIPTS = PROJECT_ROOT / "scripts"
if str(PROJECT_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(PROJECT_SCRIPTS))

from credential_vault import VaultToolError  # noqa: E402
from google_accounts import (  # noqa: E402
    GoogleAccess,
    GoogleAccountError,
    load_google_config,
    require_google_scopes,
    refresh_google_access,
)
from integration_config import missing_config_message  # noqa: E402


ALLOWED_API_HOST = "gmail.googleapis.com"
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
MAX_MESSAGE_BYTES = 24 * 1024 * 1024


class GmailToolError(Exception):
    """Erro seguro da skill Gmail."""


class GmailApiError(GmailToolError):
    """Erro sanitizado devolvido pela API do Gmail."""

    def __init__(self, status: int | None, code: str, message: str) -> None:
        prefix = f"Gmail HTTP {status}" if status is not None else "Gmail"
        super().__init__(f"{prefix} ({code}): {message}")


@dataclass(frozen=True)
class GmailConfig:
    """Configuração não confidencial da API."""

    api_base: str
    google_config: Path
    timeout_seconds: int
    page_size: int
    max_pages: int
    max_response_bytes: int


def load_config(path: Path) -> GmailConfig:
    try:
        values = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise GmailToolError(
            missing_config_message("gmail", path)
        ) from exc
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise GmailToolError(
            f"Não foi possível carregar a configuração Gmail '{path}'."
        ) from exc
    api_base = str(values.get("api_base", "")).rstrip("/")
    parsed = urllib.parse.urlparse(api_base)
    try:
        port = parsed.port
    except ValueError as exc:
        raise GmailToolError("'api_base' contém uma porta inválida.") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != ALLOWED_API_HOST
        or parsed.path.rstrip("/") != "/gmail/v1"
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise GmailToolError(
            "'api_base' deve ser 'https://gmail.googleapis.com/gmail/v1'."
        )
    raw_google_path = str(values.get("google_config", "")).strip()
    if not raw_google_path:
        raise GmailToolError("'google_config' não pode ficar vazio.")
    google_path = Path(raw_google_path).expanduser()
    if not google_path.is_absolute():
        google_path = (PROJECT_ROOT / google_path).resolve()
    timeout = values.get("timeout_seconds", 30)
    page_size = values.get("page_size", 50)
    max_pages = values.get("max_pages", 20)
    max_response = values.get("max_response_bytes", 5_242_880)
    if not isinstance(timeout, int) or not 1 <= timeout <= 120:
        raise GmailToolError("'timeout_seconds' deve estar entre 1 e 120.")
    if not isinstance(page_size, int) or not 1 <= page_size <= 500:
        raise GmailToolError("'page_size' deve estar entre 1 e 500.")
    if not isinstance(max_pages, int) or not 1 <= max_pages <= 100:
        raise GmailToolError("'max_pages' deve estar entre 1 e 100.")
    if not isinstance(max_response, int) or not 1024 <= max_response <= 50_000_000:
        raise GmailToolError("'max_response_bytes' está fora do limite permitido.")
    return GmailConfig(
        api_base,
        google_path,
        timeout,
        page_size,
        max_pages,
        max_response,
    )


def validate_identifier(value: str, field: str) -> str:
    normalized = str(value).strip()
    if not IDENTIFIER_PATTERN.fullmatch(normalized):
        raise GmailToolError(f"'{field}' contém um identificador inválido.")
    return normalized


def safe_error(payload: Any) -> tuple[str, str]:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            code = error.get("status") or error.get("code")
            message = error.get("message")
            return (
                str(code)[:100] if code is not None else "api_error",
                str(message)[:500] if message else "A API recusou a operação.",
            )
    return "api_error", "A API recusou a operação."


class GmailClient:
    """Cliente fechado para os endpoints permitidos."""

    def __init__(
        self,
        config: GmailConfig,
        access_token: str,
        *,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        if not access_token:
            raise GmailToolError("O access token do Gmail está vazio.")
        self.config = config
        self._access_token = access_token
        self._opener = opener

    def close(self) -> None:
        self._access_token = ""

    def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        if method not in {"GET", "POST"}:
            raise GmailToolError("Método interno da API inválido.")
        if not path.startswith("/") or ".." in path or "?" in path or "#" in path:
            raise GmailToolError("Caminho interno da API inválido.")
        clean_query = {
            key: value
            for key, value in (query or {}).items()
            if value is not None and value != "" and value != []
        }
        url = f"{self.config.api_base}{path}"
        if clean_query:
            url += "?" + urllib.parse.urlencode(clean_query, doseq=True)
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._access_token}",
            "User-Agent": "Coworker-Gmail/1.0",
        }
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with self._opener(request, timeout=self.config.timeout_seconds) as response:
                raw = response.read(self.config.max_response_bytes + 1)
        except urllib.error.HTTPError as exc:
            try:
                error_payload = json.loads(exc.read().decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                error_payload = {}
            code, message = safe_error(error_payload)
            code = code.replace(self._access_token, "[REDACTED]")
            message = message.replace(self._access_token, "[REDACTED]")
            raise GmailApiError(exc.code, code, message) from exc
        except urllib.error.URLError as exc:
            raise GmailApiError(
                None,
                "network_error",
                "Falha de comunicação com a API.",
            ) from exc
        if len(raw) > self.config.max_response_bytes:
            raise GmailApiError(
                None,
                "response_too_large",
                "A resposta excede o limite local configurado.",
            )
        if not raw:
            return None
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GmailApiError(
                None,
                "invalid_response",
                "A API devolveu JSON inválido.",
            ) from exc
        return self._redact(decoded)

    def _redact(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): self._redact(item)
                for key, item in value.items()
                if str(key).lower() not in {"access_token", "refresh_token"}
            }
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        if isinstance(value, str):
            return value.replace(self._access_token, "[REDACTED]")
        return value


def paginate(
    client: GmailClient,
    path: str,
    result_key: str,
    query: dict[str, Any],
    *,
    all_pages: bool,
) -> dict[str, Any]:
    results: list[Any] = []
    page_token = query.pop("pageToken", None)
    pages_read = 0
    next_token: str | None = None
    while True:
        current = dict(query)
        current["pageToken"] = page_token
        response = client.request("GET", path, query=current)
        if not isinstance(response, dict):
            raise GmailApiError(None, "invalid_response", "A lista é inválida.")
        page_results = response.get(result_key, [])
        if not isinstance(page_results, list):
            raise GmailApiError(None, "invalid_response", "A lista é inválida.")
        results.extend(page_results)
        pages_read += 1
        raw_next = response.get("nextPageToken")
        next_token = raw_next if isinstance(raw_next, str) else None
        if not all_pages or not next_token or pages_read >= client.config.max_pages:
            break
        page_token = next_token
    return {
        result_key: results,
        "result_size_estimate": response.get("resultSizeEstimate"),
        "pagination": {
            "pages_read": pages_read,
            "next_page_token": next_token,
            "truncated": bool(next_token),
        },
    }


def doctor(client: GmailClient, _args: argparse.Namespace) -> Any:
    return client.request("GET", "/users/me/profile")


def list_resource(
    client: GmailClient,
    args: argparse.Namespace,
    resource: str,
    result_key: str,
) -> dict[str, Any]:
    if args.limit is not None and not 1 <= args.limit <= 500:
        raise GmailToolError("'limit' deve estar entre 1 e 500.")
    query = {
        "maxResults": args.limit or client.config.page_size,
        "pageToken": args.page_token,
    }
    if hasattr(args, "query"):
        query["q"] = args.query
    if hasattr(args, "label_ids"):
        query["labelIds"] = args.label_ids
    if hasattr(args, "include_spam_trash"):
        query["includeSpamTrash"] = args.include_spam_trash or None
    return paginate(
        client,
        f"/users/me/{resource}",
        result_key,
        query,
        all_pages=args.all_pages,
    )


def show_resource(
    client: GmailClient,
    args: argparse.Namespace,
    resource: str,
) -> Any:
    identifier = validate_identifier(args.id, "id")
    query: dict[str, Any] = {}
    if hasattr(args, "format"):
        query["format"] = args.format
    if hasattr(args, "metadata_headers") and args.metadata_headers:
        query["metadataHeaders"] = args.metadata_headers
    return client.request(
        "GET",
        f"/users/me/{resource}/{identifier}",
        query=query,
    )


def labels_list(client: GmailClient, _args: argparse.Namespace) -> Any:
    return client.request("GET", "/users/me/labels")


def message_modify(client: GmailClient, args: argparse.Namespace) -> Any:
    identifier = validate_identifier(args.id, "id")
    add = [validate_identifier(item, "add-label") for item in args.add_label]
    remove = [
        validate_identifier(item, "remove-label") for item in args.remove_label
    ]
    if not add and not remove:
        raise GmailToolError("Informe ao menos um marcador para adicionar ou remover.")
    if set(add).intersection(remove):
        raise GmailToolError(
            "Um mesmo marcador não pode ser adicionado e removido na mesma operação."
        )
    payload = {"addLabelIds": add, "removeLabelIds": remove}
    if args.dry_run:
        return {
            "dry_run": True,
            "request": {
                "method": "POST",
                "path": f"/users/me/messages/{identifier}/modify",
                "payload": payload,
            },
        }
    return client.request(
        "POST",
        f"/users/me/messages/{identifier}/modify",
        payload=payload,
    )


def message_trash_state(
    client: GmailClient,
    args: argparse.Namespace,
    operation: str,
) -> Any:
    identifier = validate_identifier(args.id, "id")
    path = f"/users/me/messages/{identifier}/{operation}"
    if args.dry_run:
        return {"dry_run": True, "request": {"method": "POST", "path": path}}
    return client.request("POST", path, payload={})


def read_message_file(path_value: str) -> tuple[bytes, dict[str, Any]]:
    path = Path(path_value).expanduser().resolve()
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise GmailToolError(f"Não foi possível ler a mensagem '{path}'.") from exc
    if not raw or len(raw) > MAX_MESSAGE_BYTES:
        raise GmailToolError(
            f"A mensagem deve ter entre 1 e {MAX_MESSAGE_BYTES} bytes."
        )
    normalized = raw.replace(b"\r\n", b"\n")
    if b"\n\n" not in normalized:
        raise GmailToolError("A mensagem deve separar cabeçalhos e corpo.")
    header_block = normalized.split(b"\n\n", 1)[0]
    header_names = {
        line.split(b":", 1)[0].strip().lower()
        for line in header_block.splitlines()
        if b":" in line and not line.startswith((b" ", b"\t"))
    }
    for required in (b"from", b"to", b"subject"):
        if required not in header_names:
            raise GmailToolError(
                "A mensagem deve conter cabeçalhos From, To e Subject."
            )
    return raw, {
        "source": str(path),
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def draft_create(client: GmailClient, args: argparse.Namespace) -> Any:
    raw, metadata = read_message_file(args.message_file)
    encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    payload = {"message": {"raw": encoded}}
    if args.thread_id:
        payload["message"]["threadId"] = validate_identifier(
            args.thread_id,
            "thread-id",
        )
    if args.dry_run:
        return {
            "dry_run": True,
            "request": {
                "method": "POST",
                "path": "/users/me/drafts",
                "message": metadata,
                "thread_id": payload["message"].get("threadId"),
            },
        }
    return client.request("POST", "/users/me/drafts", payload=payload)


def draft_send(client: GmailClient, args: argparse.Namespace) -> Any:
    identifier = validate_identifier(args.id, "id")
    payload = {"id": identifier}
    if args.dry_run:
        return {
            "dry_run": True,
            "request": {
                "method": "POST",
                "path": "/users/me/drafts/send",
                "draft_id": identifier,
            },
        }
    return client.request("POST", "/users/me/drafts/send", payload=payload)


def add_list_arguments(parser: argparse.ArgumentParser, *, search: bool) -> None:
    if search:
        parser.add_argument("--query")
        parser.add_argument("--label-id", dest="label_ids", action="append", default=[])
        parser.add_argument("--include-spam-trash", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--page-token")
    parser.add_argument("--all-pages", action="store_true")


def add_dry_run(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dry-run", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Busca e organiza mensagens do Gmail.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--profile")
    commands = parser.add_subparsers(dest="command", required=True)
    doctor_parser = commands.add_parser("doctor")
    doctor_parser.set_defaults(handler=doctor)

    messages_parser = commands.add_parser("messages")
    messages = messages_parser.add_subparsers(dest="operation", required=True)
    messages_list = messages.add_parser("list")
    add_list_arguments(messages_list, search=True)
    messages_list.set_defaults(
        handler=lambda client, args: list_resource(
            client, args, "messages", "messages"
        )
    )
    messages_show = messages.add_parser("show")
    messages_show.add_argument("--id", required=True)
    messages_show.add_argument(
        "--format",
        choices=("minimal", "metadata", "full", "raw"),
        default="metadata",
    )
    messages_show.add_argument(
        "--metadata-header",
        dest="metadata_headers",
        action="append",
        default=[],
    )
    messages_show.set_defaults(
        handler=lambda client, args: show_resource(client, args, "messages")
    )
    modify = messages.add_parser("modify")
    modify.add_argument("--id", required=True)
    modify.add_argument("--add-label", action="append", default=[])
    modify.add_argument("--remove-label", action="append", default=[])
    add_dry_run(modify)
    modify.set_defaults(handler=message_modify)
    for operation in ("trash", "untrash"):
        action = messages.add_parser(operation)
        action.add_argument("--id", required=True)
        add_dry_run(action)
        action.set_defaults(
            handler=lambda client, args, selected=operation: message_trash_state(
                client, args, selected
            )
        )

    threads_parser = commands.add_parser("threads")
    threads = threads_parser.add_subparsers(dest="operation", required=True)
    threads_list = threads.add_parser("list")
    add_list_arguments(threads_list, search=True)
    threads_list.set_defaults(
        handler=lambda client, args: list_resource(
            client, args, "threads", "threads"
        )
    )
    threads_show = threads.add_parser("show")
    threads_show.add_argument("--id", required=True)
    threads_show.add_argument(
        "--format",
        choices=("minimal", "metadata", "full"),
        default="metadata",
    )
    threads_show.add_argument(
        "--metadata-header",
        dest="metadata_headers",
        action="append",
        default=[],
    )
    threads_show.set_defaults(
        handler=lambda client, args: show_resource(client, args, "threads")
    )

    labels_parser = commands.add_parser("labels")
    labels = labels_parser.add_subparsers(dest="operation", required=True)
    labels_list_parser = labels.add_parser("list")
    labels_list_parser.set_defaults(handler=labels_list)

    drafts_parser = commands.add_parser("drafts")
    drafts = drafts_parser.add_subparsers(dest="operation", required=True)
    drafts_list = drafts.add_parser("list")
    add_list_arguments(drafts_list, search=False)
    drafts_list.set_defaults(
        handler=lambda client, args: list_resource(client, args, "drafts", "drafts")
    )
    drafts_show = drafts.add_parser("show")
    drafts_show.add_argument("--id", required=True)
    drafts_show.add_argument(
        "--format",
        choices=("minimal", "metadata", "full", "raw"),
        default="metadata",
    )
    drafts_show.set_defaults(
        handler=lambda client, args: show_resource(client, args, "drafts")
    )
    drafts_create = drafts.add_parser("create")
    drafts_create.add_argument("--message-file", required=True)
    drafts_create.add_argument("--thread-id")
    add_dry_run(drafts_create)
    drafts_create.set_defaults(handler=draft_create)
    drafts_send = drafts.add_parser("send")
    drafts_send.add_argument("--id", required=True)
    add_dry_run(drafts_send)
    drafts_send.set_defaults(handler=draft_send)
    return parser


def print_json(value: Any, *, stream: Any = sys.stdout) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2), file=stream)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    args = build_parser().parse_args()
    access: GoogleAccess | None = None
    client: GmailClient | None = None
    try:
        config = load_config(Path(args.config).expanduser().resolve())
        google_config = load_google_config(config.google_config)
        access = refresh_google_access(google_config, args.profile)
        require_google_scopes(
            access,
            {"https://www.googleapis.com/auth/gmail.modify"},
            "Gmail",
        )
        client = GmailClient(config, access.access_token)
        result = args.handler(client, args)
    except (
        GmailToolError,
        GoogleAccountError,
        VaultToolError,
        OSError,
    ) as exc:
        print_json(
            {"ok": False, "error": str(exc), "error_type": type(exc).__name__},
            stream=sys.stderr,
        )
        return 1
    finally:
        if client is not None:
            client.close()
        if access is not None:
            access.close()
    print_json(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
