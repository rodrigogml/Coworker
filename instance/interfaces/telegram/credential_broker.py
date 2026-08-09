"""Contrato local para solicitações protegidas entre Codex e gateway."""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.credential_vault import VaultToolError, validate_entry_path


REQUEST_FILENAME = "credential-request.json"
RESPONSE_FILENAME = "credential-response.json"
ALLOWED_FIELDS = frozenset({"username", "password", "attachment"})


class CredentialBrokerError(RuntimeError):
    """Indica solicitação inválida sem incluir valores confidenciais."""


@dataclass(frozen=True)
class CredentialField:
    name: str
    label: str


@dataclass(frozen=True)
class CredentialRequest:
    request_id: str
    job_id: int
    chat_id: int
    entry: str
    prompt: str
    fields: tuple[CredentialField, ...]
    attachment_name: str | None
    request_path: Path
    response_path: Path
    expires_at: float


def parse_field_spec(value: str) -> CredentialField:
    name, separator, raw_label = value.partition(":")
    normalized = name.strip().lower()
    if normalized not in ALLOWED_FIELDS:
        raise CredentialBrokerError("Campo deve ser username, password ou attachment.")
    label = raw_label.strip() if separator else normalized.title()
    if not label or len(label) > 80 or any(char in label for char in "\r\n\0"):
        raise CredentialBrokerError("Rótulo de campo inválido.")
    return CredentialField(normalized, label)


def validate_fields(fields: list[CredentialField]) -> tuple[CredentialField, ...]:
    if not 1 <= len(fields) <= 3:
        raise CredentialBrokerError("Informe de um a três campos protegidos.")
    names = [field.name for field in fields]
    if any(
        not field.label
        or len(field.label) > 80
        or any(char in field.label for char in "\r\n\0")
        for field in fields
    ):
        raise CredentialBrokerError("Rótulo de campo inválido.")
    if len(set(names)) != len(names):
        raise CredentialBrokerError("Campos protegidos não podem ser repetidos.")
    if "attachment" in names and names.count("attachment") != 1:
        raise CredentialBrokerError("A captura aceita somente um anexo.")
    if set(names) - {"username", "password", "attachment"}:
        raise CredentialBrokerError("Há um campo protegido desconhecido.")
    if "username" in names and "password" not in names and "attachment" not in names:
        raise CredentialBrokerError("username exige password ou attachment.")
    return tuple(fields)


def job_context_from_environment() -> tuple[Path, int, int]:
    raw_output = os.environ.get("COWORKER_JOB_OUTPUT", "").strip()
    raw_chat = os.environ.get("COWORKER_CHAT_ID", "").strip()
    if not raw_output or not raw_chat:
        raise CredentialBrokerError(
            "A captura protegida só pode ser solicitada dentro de um trabalho do gateway."
        )
    output = Path(raw_output).resolve()
    if output.name != "output" or not output.parent.name.isdigit():
        raise CredentialBrokerError("A caixa do trabalho é inválida.")
    try:
        chat_id = int(raw_chat)
        job_id = int(output.parent.name)
    except ValueError as exc:
        raise CredentialBrokerError("O contexto do trabalho é inválido.") from exc
    return output.parent, job_id, chat_id


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def create_request(
    entry: str,
    prompt: str,
    fields: list[CredentialField],
    timeout_seconds: int,
    attachment_name: str | None = None,
) -> CredentialRequest:
    job_root, job_id, chat_id = job_context_from_environment()
    try:
        normalized_entry = validate_entry_path(entry)
    except VaultToolError as exc:
        raise CredentialBrokerError("Destino da credencial inválido.") from exc
    normalized_prompt = prompt.strip()
    if not normalized_prompt or len(normalized_prompt) > 500:
        raise CredentialBrokerError("A explicação da credencial é inválida.")
    normalized_fields = validate_fields(fields)
    normalized_attachment_name = str(attachment_name or "").strip() or None
    if normalized_attachment_name and (
        len(normalized_attachment_name) > 255
        or any(char in normalized_attachment_name for char in "\\/\r\n\0")
    ):
        raise CredentialBrokerError("Nome de anexo invalido.")
    if normalized_attachment_name and "attachment" not in {field.name for field in normalized_fields}:
        raise CredentialBrokerError("Nome de anexo exige o campo attachment.")
    request_path = job_root / REQUEST_FILENAME
    response_path = job_root / RESPONSE_FILENAME
    if request_path.exists() or response_path.exists():
        raise CredentialBrokerError("Já existe uma solicitação protegida neste trabalho.")
    request_id = uuid.uuid4().hex
    expires_at = time.time() + timeout_seconds
    request = CredentialRequest(
        request_id,
        job_id,
        chat_id,
        normalized_entry,
        normalized_prompt,
        normalized_fields,
        normalized_attachment_name,
        request_path,
        response_path,
        expires_at,
    )
    write_json_atomic(
        request_path,
        {
            "version": 1,
            "request_id": request_id,
            "job_id": job_id,
            "chat_id": chat_id,
            "entry": normalized_entry,
            "prompt": normalized_prompt,
            "fields": [field.__dict__ for field in normalized_fields],
            "attachment_name": normalized_attachment_name,
            "expires_at": expires_at,
        },
    )
    return request


def load_request(path: Path, jobs_dir: Path) -> CredentialRequest:
    try:
        resolved = path.resolve(strict=True)
        job_root = resolved.parent
        job_root.relative_to(jobs_dir.resolve(strict=True))
        if resolved.name != REQUEST_FILENAME or not job_root.name.isdigit():
            raise ValueError
        value = json.loads(resolved.read_text(encoding="utf-8"))
        fields = validate_fields(
            [
                CredentialField(str(item["name"]), str(item["label"]))
                for item in value["fields"]
            ]
        )
        entry = validate_entry_path(str(value["entry"]))
        request_id = str(value["request_id"])
        if len(request_id) != 32 or any(char not in "0123456789abcdef" for char in request_id):
            raise ValueError
        request = CredentialRequest(
            request_id,
            int(value["job_id"]),
            int(value["chat_id"]),
            entry,
            str(value["prompt"]).strip(),
            fields,
            str(value.get("attachment_name") or "").strip() or None,
            resolved,
            job_root / RESPONSE_FILENAME,
            float(value["expires_at"]),
        )
        if request.job_id != int(job_root.name) or not request.prompt:
            raise ValueError
        if request.attachment_name and "attachment" not in {field.name for field in request.fields}:
            raise ValueError
        return request
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError, VaultToolError) as exc:
        raise CredentialBrokerError("Solicitação protegida inválida.") from exc


def write_response(request: CredentialRequest, **result: Any) -> None:
    write_json_atomic(
        request.response_path,
        {"request_id": request.request_id, **result},
    )
