#!/usr/bin/env python3
"""Gerencia recursos permitidos do Todoist sem expor o token."""

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
DEFAULT_CONFIG = PROJECT_ROOT / "data" / "config" / "todoist.toml"
EXAMPLE_CONFIG = PROJECT_ROOT / "config" / "todoist.example.toml"
PROJECT_SCRIPTS = PROJECT_ROOT / "scripts"
if str(PROJECT_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(PROJECT_SCRIPTS))

from credential_vault import VaultToolError, read_entry_secret  # noqa: E402
from integration_profiles import (  # noqa: E402
    IntegrationProfileError,
    resolve_credential_ref,
)
from integration_config import missing_config_message  # noqa: E402


ALLOWED_API_HOST = "api.todoist.com"
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
SENSITIVE_KEYS = {
    "access_token",
    "authorization",
    "client_secret",
    "password",
    "secret",
    "token",
}


class TodoistToolError(Exception):
    """Erro seguro para apresentação ao agente ou à pessoa usuária."""


class TodoistApiError(TodoistToolError):
    """Erro sanitizado devolvido pela API do Todoist."""

    def __init__(self, status: int | None, message: str) -> None:
        self.status = status
        self.message = message
        prefix = f"Todoist HTTP {status}" if status is not None else "Todoist"
        super().__init__(f"{prefix}: {message}")


@dataclass(frozen=True)
class TodoistConfig:
    """Configuração local não confidencial."""

    api_base: str
    credential_ref: str
    timeout_seconds: int
    page_size: int
    max_pages: int


def load_config(path: Path, profile: str | None = None) -> TodoistConfig:
    """Carrega e valida a configuração TOML."""
    try:
        values = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise TodoistToolError(
            missing_config_message("todoist", path)
        ) from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise TodoistToolError(
            f"Não foi possível carregar a configuração '{path}'."
        ) from exc

    api_base = str(values.get("api_base", "")).rstrip("/")
    parsed = urllib.parse.urlparse(api_base)
    try:
        port = parsed.port
    except ValueError as exc:
        raise TodoistToolError("'api_base' contém uma porta inválida.") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != ALLOWED_API_HOST
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path.rstrip("/") != "/api/v1"
        or parsed.query
        or parsed.fragment
    ):
        raise TodoistToolError(
            f"'api_base' deve ser 'https://{ALLOWED_API_HOST}/api/v1'."
        )

    try:
        _, credential_ref = resolve_credential_ref(values, profile)
    except IntegrationProfileError as exc:
        raise TodoistToolError(str(exc)) from exc
    timeout_seconds = values.get("timeout_seconds", 30)
    page_size = values.get("page_size", 100)
    max_pages = values.get("max_pages", 20)
    if not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= 120:
        raise TodoistToolError("'timeout_seconds' deve estar entre 1 e 120.")
    if not isinstance(page_size, int) or not 1 <= page_size <= 200:
        raise TodoistToolError("'page_size' deve estar entre 1 e 200.")
    if not isinstance(max_pages, int) or not 1 <= max_pages <= 100:
        raise TodoistToolError("'max_pages' deve estar entre 1 e 100.")
    return TodoistConfig(
        api_base,
        credential_ref,
        timeout_seconds,
        page_size,
        max_pages,
    )


def validate_identifier(value: str, field: str) -> str:
    """Valida um identificador antes de inseri-lo no caminho da URL."""
    normalized = str(value).strip()
    if not IDENTIFIER_PATTERN.fullmatch(normalized):
        raise TodoistToolError(f"'{field}' contém um identificador inválido.")
    return normalized


def validate_required_text(value: str, field: str, *, maximum: int = 1024) -> str:
    """Valida texto obrigatório antes de uma escrita."""
    normalized = str(value).strip()
    if not normalized:
        raise TodoistToolError(f"'{field}' não pode ficar vazio.")
    if len(normalized) > maximum:
        raise TodoistToolError(
            f"'{field}' deve ter no máximo {maximum} caracteres."
        )
    return normalized


def sanitize_payload(value: Any) -> Any:
    """Remove campos confidenciais de estruturas devolvidas pela API."""
    if isinstance(value, dict):
        return {
            str(key): sanitize_payload(item)
            for key, item in value.items()
            if str(key).lower() not in SENSITIVE_KEYS
        }
    if isinstance(value, list):
        return [sanitize_payload(item) for item in value]
    return value


def safe_error_message(payload: Any) -> str:
    """Extrai somente uma mensagem curta de erro."""
    if isinstance(payload, dict):
        for key in ("error", "message", "error_tag"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:500]
    return "A API recusou a operação sem uma mensagem segura."


class TodoistClient:
    """Cliente mínimo para operações permitidas da API v1."""

    def __init__(
        self,
        config: TodoistConfig,
        token: str,
        *,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        if not token:
            raise TodoistToolError("O token do Todoist está vazio.")
        self.config = config
        self._token = token
        self._opener = opener

    def close(self) -> None:
        """Descarta a referência ao token."""
        self._token = ""

    def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        """Executa uma chamada e devolve JSON sanitizado."""
        if not path.startswith("/") or ".." in path:
            raise TodoistToolError("Caminho interno da API inválido.")
        url = f"{self.config.api_base}{path}"
        clean_query = {
            key: value
            for key, value in (query or {}).items()
            if value is not None and value != ""
        }
        if clean_query:
            url = f"{url}?{urllib.parse.urlencode(clean_query)}"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token}",
            "User-Agent": "Coworker-Todoist/1.0",
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
            message = safe_error_message(error_payload).replace(
                self._token,
                "[REDACTED]",
            )
            raise TodoistApiError(exc.code, message) from exc
        except urllib.error.URLError as exc:
            raise TodoistApiError(None, "Falha de comunicação com a API.") from exc

        if not raw:
            return None
        try:
            return sanitize_payload(json.loads(raw.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TodoistApiError(None, "A API devolveu JSON inválido.") from exc

    def list(
        self,
        path: str,
        query: dict[str, Any],
        *,
        all_pages: bool,
    ) -> dict[str, Any]:
        """Percorre uma listagem paginada com limite configurado."""
        cursor = query.get("cursor")
        results: list[Any] = []
        seen_ids: set[str] = set()
        pages = 0
        next_cursor: str | None = None
        while True:
            current_query = dict(query)
            current_query["cursor"] = cursor
            payload = self.request("GET", path, query=current_query)
            if not isinstance(payload, dict) or not isinstance(
                payload.get("results"),
                list,
            ):
                raise TodoistApiError(None, "Formato de paginação inesperado.")
            for item in payload["results"]:
                identifier = str(item.get("id", "")) if isinstance(item, dict) else ""
                if identifier and identifier in seen_ids:
                    continue
                if identifier:
                    seen_ids.add(identifier)
                results.append(item)
            pages += 1
            next_cursor = payload.get("next_cursor")
            if (
                not all_pages
                or not next_cursor
                or pages >= self.config.max_pages
            ):
                break
            cursor = next_cursor
        return {
            "ok": True,
            "results": results,
            "count": len(results),
            "pagination": {
                "pages": pages,
                "next_cursor": next_cursor,
                "truncated": bool(
                    all_pages
                    and next_cursor
                    and pages >= self.config.max_pages
                ),
            },
        }


def dry_run(method: str, path: str, payload: dict[str, Any] | None) -> dict[str, Any]:
    """Descreve uma escrita sem executá-la."""
    return {
        "ok": True,
        "dry_run": True,
        "request": {
            "method": method,
            "path": path,
            "payload": payload,
        },
    }


def execute_list(
    client: TodoistClient,
    args: argparse.Namespace,
    path: str,
    query: dict[str, Any],
) -> dict[str, Any]:
    """Executa uma listagem comum."""
    query["limit"] = args.limit or client.config.page_size
    query["cursor"] = args.cursor
    return client.list(path, query, all_pages=args.all_pages)


def execute_show(
    client: TodoistClient,
    args: argparse.Namespace,
    resource: str,
) -> dict[str, Any]:
    """Consulta um objeto por ID."""
    identifier = validate_identifier(args.id, "id")
    result = client.request("GET", f"/{resource}/{identifier}")
    return {"ok": True, "result": result}


def execute_write(
    client: TodoistClient,
    args: argparse.Namespace,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Executa ou simula uma escrita."""
    if args.dry_run:
        return dry_run(method, path, payload)
    result = client.request(method, path, payload=payload)
    return {"ok": True, "result": result}


def compact_payload(**values: Any) -> dict[str, Any]:
    """Remove somente valores ausentes, preservando falsos e listas vazias."""
    return {key: value for key, value in values.items() if value is not None}


def require_changes(payload: dict[str, Any]) -> dict[str, Any]:
    """Exige ao menos um campo mutável."""
    if not payload:
        raise TodoistToolError("Informe ao menos um campo para alterar.")
    return payload


def tasks_list(client: TodoistClient, args: argparse.Namespace) -> dict[str, Any]:
    return execute_list(
        client,
        args,
        "/tasks",
        compact_payload(
            project_id=args.project_id,
            section_id=args.section_id,
            parent_id=args.parent_id,
            label=args.label,
            ids=args.ids,
        ),
    )


def tasks_create(client: TodoistClient, args: argparse.Namespace) -> dict[str, Any]:
    payload = compact_payload(
        content=validate_required_text(args.content, "content"),
        description=args.description,
        project_id=args.project_id,
        section_id=args.section_id,
        parent_id=args.parent_id,
        labels=args.labels,
        priority=args.priority,
        due_string=args.due_string,
        due_date=args.due_date,
        due_datetime=args.due_datetime,
    )
    return execute_write(client, args, "POST", "/tasks", payload)


def tasks_update(client: TodoistClient, args: argparse.Namespace) -> dict[str, Any]:
    identifier = validate_identifier(args.id, "id")
    content = (
        validate_required_text(args.content, "content")
        if args.content is not None
        else None
    )
    payload = require_changes(
        compact_payload(
            content=content,
            description=args.description,
            labels=args.labels,
            priority=args.priority,
            due_string=args.due_string,
            due_date=args.due_date,
            due_datetime=args.due_datetime,
        )
    )
    return execute_write(client, args, "POST", f"/tasks/{identifier}", payload)


def tasks_move(client: TodoistClient, args: argparse.Namespace) -> dict[str, Any]:
    identifier = validate_identifier(args.id, "id")
    targets = compact_payload(
        project_id=args.project_id,
        section_id=args.section_id,
        parent_id=args.parent_id,
    )
    if len(targets) != 1:
        raise TodoistToolError("Informe exatamente um destino para mover a tarefa.")
    return execute_write(
        client,
        args,
        "POST",
        f"/tasks/{identifier}/move",
        targets,
    )


def resource_action(
    client: TodoistClient,
    args: argparse.Namespace,
    resource: str,
    action: str,
    method: str = "POST",
) -> dict[str, Any]:
    identifier = validate_identifier(args.id, "id")
    suffix = f"/{action}" if action else ""
    return execute_write(
        client,
        args,
        method,
        f"/{resource}/{identifier}{suffix}",
    )


def projects_list(client: TodoistClient, args: argparse.Namespace) -> dict[str, Any]:
    return execute_list(
        client,
        args,
        "/projects",
        compact_payload(
            folder_id=args.folder_id,
            workspace_id=args.workspace_id,
        ),
    )


def projects_create(client: TodoistClient, args: argparse.Namespace) -> dict[str, Any]:
    payload = compact_payload(
        name=validate_required_text(args.name, "name", maximum=255),
        description=args.description,
        parent_id=args.parent_id,
        color=args.color,
        is_favorite=args.favorite,
        view_style=args.view_style,
        workspace_id=args.workspace_id,
    )
    return execute_write(client, args, "POST", "/projects", payload)


def projects_update(client: TodoistClient, args: argparse.Namespace) -> dict[str, Any]:
    identifier = validate_identifier(args.id, "id")
    name = (
        validate_required_text(args.name, "name", maximum=255)
        if args.name is not None
        else None
    )
    payload = require_changes(
        compact_payload(
            name=name,
            description=args.description,
            color=args.color,
            is_favorite=args.favorite,
            view_style=args.view_style,
        )
    )
    return execute_write(client, args, "POST", f"/projects/{identifier}", payload)


def sections_list(client: TodoistClient, args: argparse.Namespace) -> dict[str, Any]:
    return execute_list(
        client,
        args,
        "/sections",
        compact_payload(project_id=args.project_id),
    )


def sections_create(client: TodoistClient, args: argparse.Namespace) -> dict[str, Any]:
    payload = compact_payload(
        name=validate_required_text(args.name, "name", maximum=255),
        project_id=args.project_id,
        order=args.order,
        description=args.description,
    )
    return execute_write(client, args, "POST", "/sections", payload)


def sections_update(client: TodoistClient, args: argparse.Namespace) -> dict[str, Any]:
    identifier = validate_identifier(args.id, "id")
    name = (
        validate_required_text(args.name, "name", maximum=255)
        if args.name is not None
        else None
    )
    payload = require_changes(
        compact_payload(
            name=name,
            section_order=args.order,
            description=args.description,
        )
    )
    return execute_write(client, args, "POST", f"/sections/{identifier}", payload)


def labels_list(client: TodoistClient, args: argparse.Namespace) -> dict[str, Any]:
    return execute_list(client, args, "/labels", {})


def labels_create(client: TodoistClient, args: argparse.Namespace) -> dict[str, Any]:
    payload = compact_payload(
        name=validate_required_text(args.name, "name", maximum=128),
        order=args.order,
        color=args.color,
        is_favorite=args.favorite,
    )
    return execute_write(client, args, "POST", "/labels", payload)


def labels_update(client: TodoistClient, args: argparse.Namespace) -> dict[str, Any]:
    identifier = validate_identifier(args.id, "id")
    name = (
        validate_required_text(args.name, "name", maximum=128)
        if args.name is not None
        else None
    )
    payload = require_changes(
        compact_payload(
            name=name,
            order=args.order,
            color=args.color,
            is_favorite=args.favorite,
        )
    )
    return execute_write(client, args, "POST", f"/labels/{identifier}", payload)


def doctor(client: TodoistClient, args: argparse.Namespace) -> dict[str, Any]:
    del args
    payload = client.request("GET", "/projects", query={"limit": 1})
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise TodoistApiError(None, "Resposta de diagnóstico inesperada.")
    return {
        "ok": True,
        "authenticated": True,
        "api_base": client.config.api_base,
    }


def add_pagination(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--cursor")
    parser.add_argument("--limit", type=int, choices=range(1, 201))
    parser.add_argument("--all-pages", action="store_true")


def add_write(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dry-run", action="store_true")


def add_id(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--id", required=True)


def add_favorite(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--favorite", dest="favorite", action="store_true")
    group.add_argument("--not-favorite", dest="favorite", action="store_false")
    parser.set_defaults(favorite=None)


def add_task_fields(
    parser: argparse.ArgumentParser,
    *,
    require_content: bool,
) -> None:
    parser.add_argument("--content", required=require_content)
    parser.add_argument("--description")
    parser.add_argument("--labels", nargs="*")
    parser.add_argument("--priority", type=int, choices=range(1, 5))
    due_group = parser.add_mutually_exclusive_group()
    due_group.add_argument("--due-string")
    due_group.add_argument("--due-date")
    due_group.add_argument("--due-datetime")


def build_parser() -> argparse.ArgumentParser:
    """Cria a CLI sem aceitar token ou chamadas arbitrárias."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--profile")
    resources = parser.add_subparsers(dest="resource", required=True)

    doctor_parser = resources.add_parser("doctor")
    doctor_parser.set_defaults(handler=doctor)

    tasks = resources.add_parser("tasks").add_subparsers(
        dest="action",
        required=True,
    )
    task_list = tasks.add_parser("list")
    task_list.add_argument("--project-id")
    task_list.add_argument("--section-id")
    task_list.add_argument("--parent-id")
    task_list.add_argument("--label")
    task_list.add_argument("--ids")
    add_pagination(task_list)
    task_list.set_defaults(handler=tasks_list)
    task_show = tasks.add_parser("show")
    add_id(task_show)
    task_show.set_defaults(
        handler=lambda client, args: execute_show(client, args, "tasks")
    )
    task_create = tasks.add_parser("create")
    add_task_fields(task_create, require_content=True)
    task_create.add_argument("--project-id")
    task_create.add_argument("--section-id")
    task_create.add_argument("--parent-id")
    add_write(task_create)
    task_create.set_defaults(handler=tasks_create)
    task_update = tasks.add_parser("update")
    add_id(task_update)
    add_task_fields(task_update, require_content=False)
    add_write(task_update)
    task_update.set_defaults(handler=tasks_update)
    task_move = tasks.add_parser("move")
    add_id(task_move)
    task_move.add_argument("--project-id")
    task_move.add_argument("--section-id")
    task_move.add_argument("--parent-id")
    add_write(task_move)
    task_move.set_defaults(handler=tasks_move)
    for action in ("close", "reopen", "delete"):
        action_parser = tasks.add_parser(action)
        add_id(action_parser)
        add_write(action_parser)
        method = "DELETE" if action == "delete" else "POST"
        suffix = "" if action == "delete" else action
        action_parser.set_defaults(
            handler=lambda client, args, suffix=suffix, method=method: resource_action(
                client,
                args,
                "tasks",
                suffix,
                method,
            )
        )

    projects = resources.add_parser("projects").add_subparsers(
        dest="action",
        required=True,
    )
    project_list = projects.add_parser("list")
    project_list.add_argument("--folder-id", type=int)
    project_list.add_argument("--workspace-id", type=int)
    add_pagination(project_list)
    project_list.set_defaults(handler=projects_list)
    project_show = projects.add_parser("show")
    add_id(project_show)
    project_show.set_defaults(
        handler=lambda client, args: execute_show(client, args, "projects")
    )
    project_create = projects.add_parser("create")
    project_create.add_argument("--name", required=True)
    project_create.add_argument("--description")
    project_create.add_argument("--parent-id")
    project_create.add_argument("--color")
    project_create.add_argument("--view-style", choices=("list", "board", "calendar"))
    project_create.add_argument("--workspace-id", type=int)
    add_favorite(project_create)
    add_write(project_create)
    project_create.set_defaults(handler=projects_create)
    project_update = projects.add_parser("update")
    add_id(project_update)
    project_update.add_argument("--name")
    project_update.add_argument("--description")
    project_update.add_argument("--color")
    project_update.add_argument("--view-style", choices=("list", "board", "calendar"))
    add_favorite(project_update)
    add_write(project_update)
    project_update.set_defaults(handler=projects_update)
    for action in ("archive", "unarchive", "delete"):
        action_parser = projects.add_parser(action)
        add_id(action_parser)
        add_write(action_parser)
        method = "DELETE" if action == "delete" else "POST"
        suffix = "" if action == "delete" else action
        action_parser.set_defaults(
            handler=lambda client, args, suffix=suffix, method=method: resource_action(
                client,
                args,
                "projects",
                suffix,
                method,
            )
        )

    sections = resources.add_parser("sections").add_subparsers(
        dest="action",
        required=True,
    )
    section_list = sections.add_parser("list")
    section_list.add_argument("--project-id")
    add_pagination(section_list)
    section_list.set_defaults(handler=sections_list)
    section_show = sections.add_parser("show")
    add_id(section_show)
    section_show.set_defaults(
        handler=lambda client, args: execute_show(client, args, "sections")
    )
    section_create = sections.add_parser("create")
    section_create.add_argument("--name", required=True)
    section_create.add_argument("--project-id", required=True)
    section_create.add_argument("--order", type=int)
    section_create.add_argument("--description")
    add_write(section_create)
    section_create.set_defaults(handler=sections_create)
    section_update = sections.add_parser("update")
    add_id(section_update)
    section_update.add_argument("--name")
    section_update.add_argument("--order", type=int)
    section_update.add_argument("--description")
    add_write(section_update)
    section_update.set_defaults(handler=sections_update)
    for action in ("archive", "unarchive", "delete"):
        action_parser = sections.add_parser(action)
        add_id(action_parser)
        add_write(action_parser)
        method = "DELETE" if action == "delete" else "POST"
        suffix = "" if action == "delete" else action
        action_parser.set_defaults(
            handler=lambda client, args, suffix=suffix, method=method: resource_action(
                client,
                args,
                "sections",
                suffix,
                method,
            )
        )

    labels = resources.add_parser("labels").add_subparsers(
        dest="action",
        required=True,
    )
    label_list = labels.add_parser("list")
    add_pagination(label_list)
    label_list.set_defaults(handler=labels_list)
    label_show = labels.add_parser("show")
    add_id(label_show)
    label_show.set_defaults(
        handler=lambda client, args: execute_show(client, args, "labels")
    )
    label_create = labels.add_parser("create")
    label_create.add_argument("--name", required=True)
    label_create.add_argument("--order", type=int)
    label_create.add_argument("--color")
    add_favorite(label_create)
    add_write(label_create)
    label_create.set_defaults(handler=labels_create)
    label_update = labels.add_parser("update")
    add_id(label_update)
    label_update.add_argument("--name")
    label_update.add_argument("--order", type=int)
    label_update.add_argument("--color")
    add_favorite(label_update)
    add_write(label_update)
    label_update.set_defaults(handler=labels_update)
    label_delete = labels.add_parser("delete")
    add_id(label_delete)
    add_write(label_delete)
    label_delete.set_defaults(
        handler=lambda client, args: resource_action(
            client,
            args,
            "labels",
            "",
            "DELETE",
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
    client: TodoistClient | None = None
    try:
        config = load_config(Path(args.config).expanduser().resolve(), args.profile)
        token = read_entry_secret(config.credential_ref)
        client = TodoistClient(config, token)
        token = ""
        result = args.handler(client, args)
    except (TodoistToolError, VaultToolError, OSError) as exc:
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
