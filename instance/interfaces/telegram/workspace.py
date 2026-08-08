"""Caixas isoladas de trabalho e validação de artefatos de saída."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from interfaces.telegram.contracts import (
    ARTIFACT_KINDS,
    DELIVERY_SCHEMA,
    CodexDelivery,
    OutboundArtifact,
)


class WorkspaceError(RuntimeError):
    """Indica saída inválida sem revelar conteúdo do arquivo."""


@dataclass(frozen=True)
class JobWorkspace:
    root: Path
    input_dir: Path
    derived_dir: Path
    output_dir: Path
    schema_path: Path
    result_path: Path

    @classmethod
    def create(cls, jobs_dir: Path, job_id: int) -> "JobWorkspace":
        root = (jobs_dir / str(job_id)).resolve()
        input_dir = root / "input"
        derived_dir = root / "derived"
        output_dir = root / "output"
        for directory in (input_dir, derived_dir, output_dir):
            directory.mkdir(parents=True, exist_ok=True)
        schema_path = root / "delivery-schema.json"
        result_path = root / "result.json"
        schema_path.write_text(
            json.dumps(DELIVERY_SCHEMA, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return cls(root, input_dir, derived_dir, output_dir, schema_path, result_path)

    def save_result(self, raw: str) -> None:
        self.result_path.write_text(raw, encoding="utf-8")


def detect_mime(path: Path) -> str:
    with path.open("rb") as stream:
        header = stream.read(32)
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if header.startswith(b"%PDF-"):
        return "application/pdf"
    if header.startswith(b"PK\x03\x04"):
        guessed = mimetypes.guess_type(path.name)[0]
        return guessed or "application/zip"
    if header.startswith(b"OggS"):
        return "audio/ogg"
    if len(header) >= 12 and header[4:8] == b"ftyp":
        return "video/mp4"
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def png_has_alpha(path: Path) -> bool:
    with path.open("rb") as stream:
        header = stream.read(26)
    return len(header) >= 26 and header.startswith(b"\x89PNG\r\n\x1a\n") and header[25] in {4, 6}


def effective_kind(requested: str, mime: str, path: Path) -> str:
    compatible = {
        "photo": mime in {"image/jpeg", "image/webp"} or (mime == "image/png" and not png_has_alpha(path)),
        "animation": mime == "image/gif",
        "voice": mime == "audio/ogg",
        "audio": mime.startswith("audio/"),
        "video": mime.startswith("video/"),
        "document": True,
    }
    if requested != "auto" and compatible.get(requested, False):
        return requested
    if mime == "image/png":
        return "document" if png_has_alpha(path) else "photo"
    if mime in {"image/jpeg", "image/webp"}:
        return "photo"
    if mime == "image/gif":
        return "animation"
    if mime == "audio/ogg":
        return "voice"
    if mime.startswith("audio/"):
        return "audio"
    if mime.startswith("video/"):
        return "video"
    return "document"


def validate_artifact(
    workspace: JobWorkspace,
    relative_path: str,
    requested_kind: str,
    caption: str,
    max_bytes: int,
) -> OutboundArtifact:
    if requested_kind not in ARTIFACT_KINDS:
        raise WorkspaceError("O resultado solicitou um tipo de artefato inválido.")
    raw = Path(relative_path)
    if raw.is_absolute() or ".." in raw.parts:
        raise WorkspaceError("O resultado declarou um caminho fora da caixa de saída.")
    candidate = workspace.output_dir / raw
    _reject_reparse_chain(workspace.output_dir, candidate)
    try:
        resolved_output = workspace.output_dir.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(resolved_output)
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise WorkspaceError("O artefato não existe dentro da caixa de saída.") from exc
    if not resolved.is_file() or resolved.is_symlink():
        raise WorkspaceError("O artefato não é um arquivo regular seguro.")
    size = resolved.stat().st_size
    if size <= 0:
        raise WorkspaceError("O artefato está vazio.")
    if size > max_bytes:
        raise WorkspaceError("O artefato excede o limite de envio configurado.")
    mime = detect_mime(resolved)
    kind = effective_kind(requested_kind, mime, resolved)
    digest = hashlib.sha256()
    with resolved.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return OutboundArtifact(
        resolved,
        raw.as_posix(),
        requested_kind,
        kind,
        caption[:1024],
        mime,
        size,
        digest.hexdigest(),
    )


def parse_delivery(raw: str, workspace: JobWorkspace, max_bytes: int) -> CodexDelivery:
    workspace.save_result(raw)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return CodexDelivery(text=raw.strip())
    if not isinstance(value, dict) or not isinstance(value.get("text"), str):
        return CodexDelivery(text=raw.strip())
    requested = value.get("artifacts", [])
    if not isinstance(requested, list):
        raise WorkspaceError("A lista de artefatos do resultado é inválida.")
    artifacts: list[OutboundArtifact] = []
    for item in requested:
        if not isinstance(item, dict):
            raise WorkspaceError("O resultado contém um artefato inválido.")
        artifacts.append(
            validate_artifact(
                workspace,
                str(item.get("path", "")),
                str(item.get("kind", "auto")),
                str(item.get("caption", "")),
                max_bytes,
            )
        )
    return CodexDelivery(text=value["text"].strip(), artifacts=tuple(artifacts))


def _reject_reparse_chain(root: Path, candidate: Path) -> None:
    current = root
    relative = candidate.relative_to(root)
    for part in relative.parts:
        current = current / part
        if not current.exists():
            break
        info = current.lstat()
        attributes = int(getattr(info, "st_file_attributes", 0))
        reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
        if current.is_symlink() or attributes & reparse_flag:
            raise WorkspaceError("O caminho do artefato contém link ou reparse point.")
