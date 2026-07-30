#!/usr/bin/env python3
"""Consulta e administra Google Contacts pela People API."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = PROJECT_ROOT / "data" / "config" / "contacts.toml"
EXAMPLE_CONFIG = PROJECT_ROOT / "config" / "contacts.example.toml"
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
from google_api import GoogleApiClient, GoogleApiError, paginate  # noqa: E402
from google_service_config import (  # noqa: E402
    GoogleServiceConfig,
    load_service_config,
)


RESOURCE = re.compile(r"^people/[A-Za-z0-9_-]+$")
GROUP_RESOURCE = re.compile(r"^contactGroups/[A-Za-z0-9_-]+$")
CONTACTS_SCOPE = {"https://www.googleapis.com/auth/contacts"}
ALLOWED_FIELDS = {
    "metadata",
    "names",
    "emailAddresses",
    "phoneNumbers",
    "organizations",
    "biographies",
    "memberships",
}


class ContactsToolError(Exception):
    """Erro seguro da skill Contacts."""


def load_config(path: Path) -> GoogleServiceConfig:
    try:
        config = load_service_config(
            path,
            project_root=PROJECT_ROOT,
            default_path=DEFAULT_CONFIG,
            example_path=EXAMPLE_CONFIG,
            service="Google Contacts",
            api_host="people.googleapis.com",
            api_path="/v1",
            max_page_size=1000,
        )
    except ValueError as exc:
        raise ContactsToolError(str(exc)) from exc
    fields = str(config.extras.get("person_fields", "")).strip()
    selected = set(fields.split(","))
    if not fields or not selected.issubset(ALLOWED_FIELDS):
        raise ContactsToolError("'person_fields' contém campos não permitidos.")
    if "metadata" not in selected:
        raise ContactsToolError("'person_fields' deve incluir metadata.")
    return config


def _resource(value: str) -> str:
    selected = str(value).strip()
    if not RESOURCE.fullmatch(selected):
        raise ContactsToolError(
            "'resource-name' deve usar o formato people/IDENTIFICADOR."
        )
    return selected


def _group_resource(value: str) -> str:
    selected = str(value).strip()
    if not GROUP_RESOURCE.fullmatch(selected):
        raise ContactsToolError(
            "'group-resource' deve usar o formato contactGroups/IDENTIFICADOR."
        )
    return selected


def _fields(config: GoogleServiceConfig) -> str:
    return str(config.extras["person_fields"])


def doctor(
    client: GoogleApiClient,
    config: GoogleServiceConfig,
    _args: argparse.Namespace,
) -> Any:
    return client.request(
        "GET",
        "/people/me",
        query={"personFields": _fields(config)},
    )


def contacts_list(
    client: GoogleApiClient,
    config: GoogleServiceConfig,
    args: argparse.Namespace,
) -> Any:
    query = {
        "personFields": _fields(config),
        "pageSize": config.page_size,
        "sortOrder": args.sort_order,
        "pageToken": args.page_token,
        "sources": "READ_SOURCE_TYPE_CONTACT",
    }
    return paginate(
        client,
        "/people/me/connections",
        "connections",
        query,
        all_pages=args.all_pages,
        max_pages=config.max_pages,
    )


def contacts_search(
    client: GoogleApiClient,
    config: GoogleServiceConfig,
    args: argparse.Namespace,
) -> Any:
    query = args.query.strip()
    if not query:
        raise ContactsToolError("'query' não pode ficar vazia.")
    read_mask = ",".join(
        field for field in _fields(config).split(",") if field != "metadata"
    )
    client.request(
        "GET",
        "/people:searchContacts",
        query={
            "query": "",
            "pageSize": 1,
            "readMask": read_mask,
            "sources": "READ_SOURCE_TYPE_CONTACT",
        },
    )
    return client.request(
        "GET",
        "/people:searchContacts",
        query={
            "query": query,
            "pageSize": args.limit,
            "readMask": read_mask,
            "sources": "READ_SOURCE_TYPE_CONTACT",
        },
    )


def contact_show(
    client: GoogleApiClient,
    config: GoogleServiceConfig,
    args: argparse.Namespace,
) -> Any:
    return client.request(
        "GET",
        f"/{_resource(args.resource_name)}",
        query={"personFields": _fields(config), "sources": "READ_SOURCE_TYPE_CONTACT"},
    )


def _person_fields(args: argparse.Namespace, *, partial: bool) -> tuple[dict[str, Any], list[str]]:
    person: dict[str, Any] = {}
    changed: list[str] = []
    name = getattr(args, "name", None)
    if name is not None:
        person["names"] = [{"unstructuredName": name}]
        changed.append("names")
    emails = getattr(args, "email", None)
    if emails:
        person["emailAddresses"] = [{"value": item} for item in emails]
        changed.append("emailAddresses")
    phones = getattr(args, "phone", None)
    if phones:
        person["phoneNumbers"] = [{"value": item} for item in phones]
        changed.append("phoneNumbers")
    organization = getattr(args, "organization", None)
    title = getattr(args, "title", None)
    if organization is not None or title is not None:
        person["organizations"] = [
            {
                key: value
                for key, value in {"name": organization, "title": title}.items()
                if value is not None
            }
        ]
        changed.append("organizations")
    note = getattr(args, "note", None)
    if note is not None:
        person["biographies"] = [{"value": note, "contentType": "TEXT_PLAIN"}]
        changed.append("biographies")
    for argument, field in (
        ("clear_name", "names"),
        ("clear_emails", "emailAddresses"),
        ("clear_phones", "phoneNumbers"),
        ("clear_organization", "organizations"),
        ("clear_note", "biographies"),
    ):
        if getattr(args, argument, False):
            if field in changed:
                raise ContactsToolError(
                    f"Não é possível definir e limpar '{field}' simultaneamente."
                )
            person[field] = []
            changed.append(field)
    if not changed:
        raise ContactsToolError("Informe ao menos um campo do contato.")
    if not partial and not any(person.get(field) for field in ("names", "emailAddresses", "phoneNumbers")):
        raise ContactsToolError("O novo contato precisa de nome, e-mail ou telefone.")
    return person, changed


def contact_create(
    client: GoogleApiClient,
    config: GoogleServiceConfig,
    args: argparse.Namespace,
) -> Any:
    payload, _changed = _person_fields(args, partial=False)
    query = {"personFields": _fields(config), "sources": "READ_SOURCE_TYPE_CONTACT"}
    if args.dry_run:
        return {
            "dry_run": True,
            "request": {"method": "POST", "path": "/people:createContact", "payload": payload},
        }
    return client.request("POST", "/people:createContact", query=query, payload=payload)


def contact_update(
    client: GoogleApiClient,
    config: GoogleServiceConfig,
    args: argparse.Namespace,
) -> Any:
    resource = _resource(args.resource_name)
    payload, changed = _person_fields(args, partial=True)
    current = client.request(
        "GET",
        f"/{resource}",
        query={"personFields": _fields(config), "sources": "READ_SOURCE_TYPE_CONTACT"},
    )
    if not isinstance(current, dict) or not isinstance(current.get("metadata"), dict):
        raise ContactsToolError("A API não devolveu os metadados do contato.")
    payload["metadata"] = current["metadata"]
    path = f"/{resource}:updateContact"
    query = {
        "updatePersonFields": ",".join(changed),
        "personFields": _fields(config),
        "sources": "READ_SOURCE_TYPE_CONTACT",
    }
    if args.dry_run:
        safe_payload = dict(payload)
        safe_payload["metadata"] = {"present": True}
        return {
            "dry_run": True,
            "request": {"method": "PATCH", "path": path, "query": query, "payload": safe_payload},
        }
    return client.request("PATCH", path, query=query, payload=payload)


def contact_delete(
    client: GoogleApiClient,
    _config: GoogleServiceConfig,
    args: argparse.Namespace,
) -> Any:
    resource = _resource(args.resource_name)
    path = f"/{resource}:deleteContact"
    if args.dry_run:
        return {"dry_run": True, "request": {"method": "DELETE", "path": path}}
    client.request("DELETE", path)
    return {"ok": True, "deleted": resource}


def groups_list(
    client: GoogleApiClient,
    config: GoogleServiceConfig,
    args: argparse.Namespace,
) -> Any:
    return paginate(
        client,
        "/contactGroups",
        "contactGroups",
        {
            "pageSize": min(config.page_size, 1000),
            "pageToken": args.page_token,
            "groupFields": "metadata,groupType,memberCount,name",
        },
        all_pages=args.all_pages,
        max_pages=config.max_pages,
    )


def group_show(
    client: GoogleApiClient,
    _config: GoogleServiceConfig,
    args: argparse.Namespace,
) -> Any:
    return client.request(
        "GET",
        f"/{_group_resource(args.group_resource)}",
        query={"maxMembers": args.max_members},
    )


def group_create(
    client: GoogleApiClient,
    _config: GoogleServiceConfig,
    args: argparse.Namespace,
) -> Any:
    name = args.name.strip()
    if not name:
        raise ContactsToolError("'name' não pode ficar vazio.")
    payload = {"contactGroup": {"name": name}}
    if args.dry_run:
        return {
            "dry_run": True,
            "request": {"method": "POST", "path": "/contactGroups", "payload": payload},
        }
    return client.request("POST", "/contactGroups", payload=payload)


def group_update(
    client: GoogleApiClient,
    _config: GoogleServiceConfig,
    args: argparse.Namespace,
) -> Any:
    resource = _group_resource(args.group_resource)
    name = args.name.strip()
    if not name:
        raise ContactsToolError("'name' não pode ficar vazio.")
    current = client.request("GET", f"/{resource}")
    if not isinstance(current, dict) or not isinstance(current.get("etag"), str):
        raise ContactsToolError("A API não devolveu o etag do grupo.")
    payload = {
        "contactGroup": {
            "resourceName": resource,
            "etag": current["etag"],
            "name": name,
        },
        "updateGroupFields": "name",
        "readGroupFields": "metadata,groupType,memberCount,name",
    }
    path = f"/{resource}"
    if args.dry_run:
        safe_payload = {
            **payload,
            "contactGroup": {
                "resourceName": resource,
                "etag": "[PRESENT]",
                "name": name,
            },
        }
        return {
            "dry_run": True,
            "request": {"method": "PUT", "path": path, "payload": safe_payload},
        }
    return client.request("PUT", path, payload=payload)


def group_delete(
    client: GoogleApiClient,
    _config: GoogleServiceConfig,
    args: argparse.Namespace,
) -> Any:
    resource = _group_resource(args.group_resource)
    path = f"/{resource}"
    query = {"deleteContacts": "false"}
    if args.dry_run:
        return {
            "dry_run": True,
            "request": {"method": "DELETE", "path": path, "query": query},
        }
    client.request("DELETE", path, query=query)
    return {"ok": True, "group_deleted": resource, "contacts_deleted": False}


def group_members_modify(
    client: GoogleApiClient,
    _config: GoogleServiceConfig,
    args: argparse.Namespace,
) -> Any:
    resource = _group_resource(args.group_resource)
    additions = [_resource(item) for item in args.add]
    removals = [_resource(item) for item in args.remove]
    if not additions and not removals:
        raise ContactsToolError("Informe ao menos um contato para adicionar ou remover.")
    if len(additions) + len(removals) > 1000:
        raise ContactsToolError("Uma alteração de grupo aceita no máximo 1000 contatos.")
    overlap = set(additions).intersection(removals)
    if overlap:
        raise ContactsToolError(
            "Um contato não pode ser adicionado e removido na mesma operação."
        )
    payload = {
        "resourceNamesToAdd": additions,
        "resourceNamesToRemove": removals,
    }
    path = f"/{resource}/members:modify"
    if args.dry_run:
        return {
            "dry_run": True,
            "request": {"method": "POST", "path": path, "payload": payload},
        }
    return client.request("POST", path, payload=payload)


def _person_arguments(parser: argparse.ArgumentParser, *, update: bool) -> None:
    parser.add_argument("--name")
    parser.add_argument("--email", action="append", default=[])
    parser.add_argument("--phone", action="append", default=[])
    parser.add_argument("--organization")
    parser.add_argument("--title")
    parser.add_argument("--note")
    if update:
        parser.add_argument("--clear-name", action="store_true")
        parser.add_argument("--clear-emails", action="store_true")
        parser.add_argument("--clear-phones", action="store_true")
        parser.add_argument("--clear-organization", action="store_true")
        parser.add_argument("--clear-note", action="store_true")
    parser.add_argument("--dry-run", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Consulta e administra Google Contacts.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--profile")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("doctor")
    check.set_defaults(handler=doctor)

    contacts = commands.add_parser("contacts")
    operations = contacts.add_subparsers(dest="operation", required=True)
    listing = operations.add_parser("list")
    listing.add_argument(
        "--sort-order",
        choices=("LAST_MODIFIED_ASCENDING", "LAST_MODIFIED_DESCENDING", "FIRST_NAME_ASCENDING", "LAST_NAME_ASCENDING"),
        default="LAST_MODIFIED_DESCENDING",
    )
    listing.add_argument("--page-token")
    listing.add_argument("--all-pages", action="store_true")
    listing.set_defaults(handler=contacts_list)

    search = operations.add_parser("search")
    search.add_argument("--query", required=True)
    search.add_argument("--limit", type=int, choices=range(1, 31), default=10)
    search.set_defaults(handler=contacts_search)

    show = operations.add_parser("show")
    show.add_argument("--resource-name", required=True)
    show.set_defaults(handler=contact_show)

    create = operations.add_parser("create")
    _person_arguments(create, update=False)
    create.set_defaults(handler=contact_create)

    update = operations.add_parser("update")
    update.add_argument("--resource-name", required=True)
    _person_arguments(update, update=True)
    update.set_defaults(handler=contact_update)

    delete = operations.add_parser("delete")
    delete.add_argument("--resource-name", required=True)
    delete.add_argument("--dry-run", action="store_true")
    delete.set_defaults(handler=contact_delete)

    groups = commands.add_parser("groups")
    group_operations = groups.add_subparsers(dest="operation", required=True)
    group_list = group_operations.add_parser("list")
    group_list.add_argument("--page-token")
    group_list.add_argument("--all-pages", action="store_true")
    group_list.set_defaults(handler=groups_list)

    group_get = group_operations.add_parser("show")
    group_get.add_argument("--group-resource", required=True)
    group_get.add_argument("--max-members", type=int, choices=range(0, 1001), default=100)
    group_get.set_defaults(handler=group_show)

    group_add = group_operations.add_parser("create")
    group_add.add_argument("--name", required=True)
    group_add.add_argument("--dry-run", action="store_true")
    group_add.set_defaults(handler=group_create)

    group_change = group_operations.add_parser("update")
    group_change.add_argument("--group-resource", required=True)
    group_change.add_argument("--name", required=True)
    group_change.add_argument("--dry-run", action="store_true")
    group_change.set_defaults(handler=group_update)

    group_remove = group_operations.add_parser("delete")
    group_remove.add_argument("--group-resource", required=True)
    group_remove.add_argument("--dry-run", action="store_true")
    group_remove.set_defaults(handler=group_delete)

    group_members = group_operations.add_parser("members")
    group_members.add_argument("--group-resource", required=True)
    group_members.add_argument("--add", action="append", default=[])
    group_members.add_argument("--remove", action="append", default=[])
    group_members.add_argument("--dry-run", action="store_true")
    group_members.set_defaults(handler=group_members_modify)
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
    client: GoogleApiClient | None = None
    try:
        config = load_config(Path(args.config).expanduser().resolve())
        google_config = load_google_config(config.google_config)
        access = refresh_google_access(google_config, args.profile)
        require_google_scopes(access, CONTACTS_SCOPE, "Google Contacts")
        client = GoogleApiClient(
            config.api_base,
            access.access_token,
            "Contacts",
            timeout_seconds=config.timeout_seconds,
            max_response_bytes=config.max_response_bytes,
        )
        result = args.handler(client, config, args)
    except (
        ContactsToolError,
        GoogleApiError,
        GoogleAccountError,
        VaultToolError,
        ValueError,
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
