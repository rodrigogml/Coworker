#!/usr/bin/env python3
"""Pesquisa e organiza Google Drive sem expor credenciais."""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
import sys
import tempfile
import urllib.parse
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = PROJECT_ROOT / "data" / "config" / "drive.toml"
EXAMPLE_CONFIG = PROJECT_ROOT / "config" / "drive.example.toml"
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
from google_api import (  # noqa: E402
    GoogleApiClient,
    GoogleApiError,
    paginate,
    validate_api_base,
)
from google_service_config import (  # noqa: E402
    GoogleServiceConfig,
    load_service_config,
)


FILE_ID = re.compile(r"^[A-Za-z0-9_-]+$")
FOLDER_MIME = "application/vnd.google-apps.folder"
DRIVE_SCOPE = {"https://www.googleapis.com/auth/drive"}


class DriveToolError(Exception):
    """Erro seguro da skill Drive."""


@dataclass(frozen=True)
class DriveConfig:
    common: GoogleServiceConfig
    upload_base: str
    max_download_bytes: int
    max_upload_bytes: int


@dataclass
class DriveClients:
    api: GoogleApiClient
    upload: GoogleApiClient

    def close(self) -> None:
        self.api.close()
        self.upload.close()


def load_config(path: Path) -> DriveConfig:
    try:
        common = load_service_config(
            path,
            project_root=PROJECT_ROOT,
            default_path=DEFAULT_CONFIG,
            example_path=EXAMPLE_CONFIG,
            service="Google Drive",
            api_host="www.googleapis.com",
            api_path="/drive/v3",
            max_page_size=1000,
        )
        upload_base = validate_api_base(
            common.extras.get("upload_base"),
            host="www.googleapis.com",
            path="/upload/drive/v3",
            field="upload_base",
        )
    except ValueError as exc:
        raise DriveToolError(str(exc)) from exc
    maximum = common.extras.get("max_download_bytes", 104_857_600)
    if not isinstance(maximum, int) or not 1024 <= maximum <= 1_073_741_824:
        raise DriveToolError("'max_download_bytes' está fora do limite permitido.")
    maximum_upload = common.extras.get("max_upload_bytes", 104_857_600)
    if (
        not isinstance(maximum_upload, int)
        or not 1024 <= maximum_upload <= 1_073_741_824
    ):
        raise DriveToolError("'max_upload_bytes' está fora do limite permitido.")
    return DriveConfig(common, upload_base, maximum, maximum_upload)


def _id(value: str, field: str = "id") -> str:
    selected = str(value).strip()
    if not FILE_ID.fullmatch(selected):
        raise DriveToolError(f"'{field}' contém um identificador inválido.")
    return selected


def _fields() -> str:
    return (
        "id,name,mimeType,size,createdTime,modifiedTime,trashed,parents,"
        "webViewLink,webContentLink,md5Checksum,capabilities,owners,shared"
    )


def doctor(
    clients: DriveClients,
    _config: DriveConfig,
    _args: argparse.Namespace,
) -> Any:
    return clients.api.request(
        "GET",
        "/about",
        query={"fields": "user(displayName,emailAddress),storageQuota"},
    )


def files_list(
    clients: DriveClients,
    config: DriveConfig,
    args: argparse.Namespace,
) -> Any:
    query_text = args.query
    if args.parent_id:
        parent_query = f"'{_id(args.parent_id, 'parent-id')}' in parents"
        query_text = f"({query_text}) and {parent_query}" if query_text else parent_query
    query = {
        "q": query_text,
        "pageSize": config.common.page_size,
        "pageToken": args.page_token,
        "orderBy": args.order_by,
        "spaces": "drive",
        "corpora": args.corpora,
        "driveId": args.drive_id,
        "includeItemsFromAllDrives": "true",
        "supportsAllDrives": "true",
        "fields": f"nextPageToken,files({_fields()})",
    }
    return paginate(
        clients.api,
        "/files",
        "files",
        query,
        all_pages=args.all_pages,
        max_pages=config.common.max_pages,
    )


def drives_list(
    clients: DriveClients,
    config: DriveConfig,
    args: argparse.Namespace,
) -> Any:
    return paginate(
        clients.api,
        "/drives",
        "drives",
        {
            "pageSize": min(config.common.page_size, 100),
            "pageToken": args.page_token,
            "q": args.query,
            "useDomainAdminAccess": "false",
        },
        all_pages=args.all_pages,
        max_pages=config.common.max_pages,
    )


def file_show(
    clients: DriveClients,
    _config: DriveConfig,
    args: argparse.Namespace,
) -> Any:
    return clients.api.request(
        "GET",
        f"/files/{_id(args.id)}",
        query={"fields": _fields(), "supportsAllDrives": "true"},
    )


def _output_path(value: str, *, overwrite: bool) -> Path:
    output = Path(value).expanduser().resolve()
    if output.exists() and not overwrite:
        raise DriveToolError(
            f"O destino '{output}' já existe. Use --overwrite para substituí-lo."
        )
    if not output.parent.is_dir():
        raise DriveToolError(f"O diretório de destino '{output.parent}' não existe.")
    return output


def file_download(
    clients: DriveClients,
    config: DriveConfig,
    args: argparse.Namespace,
) -> Any:
    identifier = _id(args.id)
    output = _output_path(args.output, overwrite=args.overwrite)
    data = clients.api.request(
        "GET",
        f"/files/{identifier}",
        query={"alt": "media", "supportsAllDrives": "true"},
        expect_json=False,
        response_limit=config.max_download_bytes,
    )
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=output.parent,
            prefix=f".{output.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(data)
            temporary_name = temporary.name
        os.replace(temporary_name, output)
        temporary_name = None
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)
    return {
        "ok": True,
        "id": identifier,
        "output": str(output),
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def file_export(
    clients: DriveClients,
    config: DriveConfig,
    args: argparse.Namespace,
) -> Any:
    identifier = _id(args.id)
    output = _output_path(args.output, overwrite=args.overwrite)
    data = clients.api.request(
        "GET",
        f"/files/{identifier}/export",
        query={"mimeType": args.mime_type},
        expect_json=False,
        response_limit=config.max_download_bytes,
    )
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=output.parent,
            prefix=f".{output.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(data)
            temporary_name = temporary.name
        os.replace(temporary_name, output)
        temporary_name = None
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)
    return {
        "ok": True,
        "id": identifier,
        "output": str(output),
        "mime_type": args.mime_type,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _multipart(metadata: dict[str, Any], content: bytes, mime_type: str) -> tuple[bytes, str]:
    boundary = f"botina-{uuid.uuid4().hex}"
    body = (
        f"--{boundary}\r\n"
        "Content-Type: application/json; charset=UTF-8\r\n\r\n"
        f"{json.dumps(metadata, ensure_ascii=False)}\r\n"
        f"--{boundary}\r\n"
        f"Content-Type: {mime_type}\r\n\r\n"
    ).encode("utf-8") + content + f"\r\n--{boundary}--\r\n".encode("ascii")
    return body, f"multipart/related; boundary={boundary}"


def file_upload(
    clients: DriveClients,
    config: DriveConfig,
    args: argparse.Namespace,
) -> Any:
    source = Path(args.source).expanduser().resolve()
    try:
        content = source.read_bytes()
    except OSError as exc:
        raise DriveToolError(f"Não foi possível ler '{source}'.") from exc
    if not content:
        raise DriveToolError("O arquivo de origem está vazio.")
    if len(content) > config.max_upload_bytes:
        raise DriveToolError("O arquivo excede o limite local configurado.")
    name = args.name or source.name
    mime_type = args.mime_type or mimetypes.guess_type(source.name)[0] or "application/octet-stream"
    metadata: dict[str, Any] = {"name": name}
    if args.parent_id:
        metadata["parents"] = [_id(args.parent_id, "parent-id")]
    summary = {
        "source": str(source),
        "name": name,
        "mime_type": mime_type,
        "bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "parent_id": metadata.get("parents", [None])[0],
    }
    if args.dry_run:
        return {"dry_run": True, "upload": summary}
    body, content_type = _multipart(metadata, content, mime_type)
    return clients.upload.request(
        "POST",
        "/files",
        query={"uploadType": "multipart", "fields": _fields(), "supportsAllDrives": "true"},
        body=body,
        content_type=content_type,
    )


def file_replace(
    clients: DriveClients,
    config: DriveConfig,
    args: argparse.Namespace,
) -> Any:
    identifier = _id(args.id)
    source = Path(args.source).expanduser().resolve()
    try:
        content = source.read_bytes()
    except OSError as exc:
        raise DriveToolError(f"Não foi possível ler '{source}'.") from exc
    if not content or len(content) > config.max_upload_bytes:
        raise DriveToolError(
            "O arquivo deve ser não vazio e respeitar o limite local de upload."
        )
    mime_type = (
        args.mime_type
        or mimetypes.guess_type(source.name)[0]
        or "application/octet-stream"
    )
    metadata = {"name": args.name} if args.name else {}
    summary = {
        "id": identifier,
        "source": str(source),
        "name": args.name,
        "mime_type": mime_type,
        "bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }
    if args.dry_run:
        return {"dry_run": True, "replace": summary}
    body, content_type = _multipart(metadata, content, mime_type)
    return clients.upload.request(
        "PATCH",
        f"/files/{identifier}",
        query={
            "uploadType": "multipart",
            "fields": _fields(),
            "supportsAllDrives": "true",
        },
        body=body,
        content_type=content_type,
    )


def file_copy(
    clients: DriveClients,
    _config: DriveConfig,
    args: argparse.Namespace,
) -> Any:
    identifier = _id(args.id)
    payload: dict[str, Any] = {}
    if args.name:
        payload["name"] = args.name
    if args.parent_id:
        payload["parents"] = [_id(args.parent_id, "parent-id")]
    path = f"/files/{identifier}/copy"
    query = {"fields": _fields(), "supportsAllDrives": "true"}
    if args.dry_run:
        return {
            "dry_run": True,
            "request": {"method": "POST", "path": path, "query": query, "payload": payload},
        }
    return clients.api.request("POST", path, query=query, payload=payload)


def folder_create(
    clients: DriveClients,
    _config: DriveConfig,
    args: argparse.Namespace,
) -> Any:
    payload: dict[str, Any] = {"name": args.name, "mimeType": FOLDER_MIME}
    if args.parent_id:
        payload["parents"] = [_id(args.parent_id, "parent-id")]
    if args.dry_run:
        return {"dry_run": True, "request": {"method": "POST", "path": "/files", "payload": payload}}
    return clients.api.request(
        "POST",
        "/files",
        query={"fields": _fields(), "supportsAllDrives": "true"},
        payload=payload,
    )


def file_update(
    clients: DriveClients,
    _config: DriveConfig,
    args: argparse.Namespace,
) -> Any:
    identifier = _id(args.id)
    payload: dict[str, Any] = {}
    if args.name is not None:
        payload["name"] = args.name
    if args.trashed is not None:
        payload["trashed"] = args.trashed
    query: dict[str, Any] = {
        "fields": _fields(),
        "supportsAllDrives": "true",
    }
    if args.add_parent:
        query["addParents"] = _id(args.add_parent, "add-parent")
    if args.remove_parent:
        query["removeParents"] = _id(args.remove_parent, "remove-parent")
    if not payload and not args.add_parent and not args.remove_parent:
        raise DriveToolError("Informe ao menos uma alteração.")
    path = f"/files/{identifier}"
    if args.dry_run:
        return {
            "dry_run": True,
            "request": {"method": "PATCH", "path": path, "query": query, "payload": payload},
        }
    return clients.api.request("PATCH", path, query=query, payload=payload)


def permissions_list(
    clients: DriveClients,
    _config: DriveConfig,
    args: argparse.Namespace,
) -> Any:
    return clients.api.request(
        "GET",
        f"/files/{_id(args.file_id, 'file-id')}/permissions",
        query={
            "fields": "permissions(id,type,role,emailAddress,domain,displayName,expirationTime,deleted)",
            "supportsAllDrives": "true",
        },
    )


def permission_create(
    clients: DriveClients,
    _config: DriveConfig,
    args: argparse.Namespace,
) -> Any:
    payload: dict[str, Any] = {"type": args.type, "role": args.role}
    if args.type in {"user", "group"}:
        if not args.email:
            raise DriveToolError("--email é obrigatório para user ou group.")
        payload["emailAddress"] = args.email
    elif args.type == "domain":
        if not args.domain:
            raise DriveToolError("--domain é obrigatório para domain.")
        payload["domain"] = args.domain
    path = f"/files/{_id(args.file_id, 'file-id')}/permissions"
    query = {
        "sendNotificationEmail": str(args.notify).lower(),
        "supportsAllDrives": "true",
        "fields": "id,type,role,emailAddress,domain",
    }
    if args.dry_run:
        return {
            "dry_run": True,
            "request": {"method": "POST", "path": path, "query": query, "payload": payload},
        }
    return clients.api.request("POST", path, query=query, payload=payload)


def permission_delete(
    clients: DriveClients,
    _config: DriveConfig,
    args: argparse.Namespace,
) -> Any:
    file_id = _id(args.file_id, "file-id")
    permission_id = _id(args.permission_id, "permission-id")
    path = f"/files/{file_id}/permissions/{permission_id}"
    if args.dry_run:
        return {"dry_run": True, "request": {"method": "DELETE", "path": path}}
    clients.api.request("DELETE", path, query={"supportsAllDrives": "true"})
    return {"ok": True, "permission_deleted": permission_id, "file_id": file_id}


def permission_update(
    clients: DriveClients,
    _config: DriveConfig,
    args: argparse.Namespace,
) -> Any:
    file_id = _id(args.file_id, "file-id")
    permission_id = _id(args.permission_id, "permission-id")
    path = f"/files/{file_id}/permissions/{permission_id}"
    payload = {"role": args.role}
    query = {
        "supportsAllDrives": "true",
        "fields": "id,type,role,emailAddress,domain",
    }
    if args.dry_run:
        return {
            "dry_run": True,
            "request": {"method": "PATCH", "path": path, "query": query, "payload": payload},
        }
    return clients.api.request("PATCH", path, query=query, payload=payload)


def _dry_run(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dry-run", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Pesquisa e organiza Google Drive.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--profile")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("doctor")
    check.set_defaults(handler=doctor)

    drives = commands.add_parser("drives")
    drive_commands = drives.add_subparsers(dest="operation", required=True)
    drive_list = drive_commands.add_parser("list")
    drive_list.add_argument("--query")
    drive_list.add_argument("--page-token")
    drive_list.add_argument("--all-pages", action="store_true")
    drive_list.set_defaults(handler=drives_list)

    files = commands.add_parser("files")
    file_commands = files.add_subparsers(dest="operation", required=True)
    listing = file_commands.add_parser("list")
    listing.add_argument("--query")
    listing.add_argument("--parent-id")
    listing.add_argument("--order-by", default="modifiedTime desc")
    listing.add_argument("--corpora", choices=("user", "drive", "allDrives"), default="user")
    listing.add_argument("--drive-id")
    listing.add_argument("--page-token")
    listing.add_argument("--all-pages", action="store_true")
    listing.set_defaults(handler=files_list)

    show = file_commands.add_parser("show")
    show.add_argument("--id", required=True)
    show.set_defaults(handler=file_show)

    download = file_commands.add_parser("download")
    download.add_argument("--id", required=True)
    download.add_argument("--output", required=True)
    download.add_argument("--overwrite", action="store_true")
    download.set_defaults(handler=file_download)

    export = file_commands.add_parser("export")
    export.add_argument("--id", required=True)
    export.add_argument("--mime-type", required=True)
    export.add_argument("--output", required=True)
    export.add_argument("--overwrite", action="store_true")
    export.set_defaults(handler=file_export)

    upload = file_commands.add_parser("upload")
    upload.add_argument("--source", required=True)
    upload.add_argument("--name")
    upload.add_argument("--mime-type")
    upload.add_argument("--parent-id")
    _dry_run(upload)
    upload.set_defaults(handler=file_upload)

    replace = file_commands.add_parser("replace")
    replace.add_argument("--id", required=True)
    replace.add_argument("--source", required=True)
    replace.add_argument("--name")
    replace.add_argument("--mime-type")
    _dry_run(replace)
    replace.set_defaults(handler=file_replace)

    copy = file_commands.add_parser("copy")
    copy.add_argument("--id", required=True)
    copy.add_argument("--name")
    copy.add_argument("--parent-id")
    _dry_run(copy)
    copy.set_defaults(handler=file_copy)

    mkdir = file_commands.add_parser("mkdir")
    mkdir.add_argument("--name", required=True)
    mkdir.add_argument("--parent-id")
    _dry_run(mkdir)
    mkdir.set_defaults(handler=folder_create)

    update = file_commands.add_parser("update")
    update.add_argument("--id", required=True)
    update.add_argument("--name")
    update.add_argument("--add-parent")
    update.add_argument("--remove-parent")
    update.add_argument("--trashed", action=argparse.BooleanOptionalAction)
    _dry_run(update)
    update.set_defaults(handler=file_update)

    permissions = commands.add_parser("permissions")
    permission_commands = permissions.add_subparsers(dest="operation", required=True)
    permission_list = permission_commands.add_parser("list")
    permission_list.add_argument("--file-id", required=True)
    permission_list.set_defaults(handler=permissions_list)

    permission_add = permission_commands.add_parser("create")
    permission_add.add_argument("--file-id", required=True)
    permission_add.add_argument("--type", choices=("user", "group", "domain", "anyone"), required=True)
    permission_add.add_argument("--role", choices=("reader", "commenter", "writer"), required=True)
    permission_add.add_argument("--email")
    permission_add.add_argument("--domain")
    permission_add.add_argument("--notify", action="store_true")
    _dry_run(permission_add)
    permission_add.set_defaults(handler=permission_create)

    permission_remove = permission_commands.add_parser("delete")
    permission_remove.add_argument("--file-id", required=True)
    permission_remove.add_argument("--permission-id", required=True)
    _dry_run(permission_remove)
    permission_remove.set_defaults(handler=permission_delete)

    permission_change = permission_commands.add_parser("update")
    permission_change.add_argument("--file-id", required=True)
    permission_change.add_argument("--permission-id", required=True)
    permission_change.add_argument(
        "--role",
        choices=("reader", "commenter", "writer"),
        required=True,
    )
    _dry_run(permission_change)
    permission_change.set_defaults(handler=permission_update)
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
    clients: DriveClients | None = None
    try:
        config = load_config(Path(args.config).expanduser().resolve())
        google_config = load_google_config(config.common.google_config)
        access = refresh_google_access(google_config, args.profile)
        require_google_scopes(access, DRIVE_SCOPE, "Google Drive")
        common = {
            "timeout_seconds": config.common.timeout_seconds,
            "max_response_bytes": config.common.max_response_bytes,
        }
        clients = DriveClients(
            GoogleApiClient(config.common.api_base, access.access_token, "Drive", **common),
            GoogleApiClient(config.upload_base, access.access_token, "Drive", **common),
        )
        result = args.handler(clients, config, args)
    except (
        DriveToolError,
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
        if clients is not None:
            clients.close()
        if access is not None:
            access.close()
    print_json(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
