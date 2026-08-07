"""Contratos internos independentes dos payloads brutos do Telegram e Codex."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


ARTIFACT_KINDS = frozenset(
    {"auto", "photo", "document", "audio", "voice", "video", "animation"}
)

DELIVERY_SCHEMA = {
    "type": "object",
    "required": ["text", "artifacts"],
    "additionalProperties": False,
    "properties": {
        "text": {"type": "string"},
        "artifacts": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["path", "kind", "caption"],
                "additionalProperties": False,
                "properties": {
                    "path": {"type": "string", "minLength": 1},
                    "kind": {"type": "string", "enum": sorted(ARTIFACT_KINDS)},
                    "caption": {"type": "string"},
                },
            },
        },
    },
}


@dataclass(frozen=True)
class Attachment:
    origin: str
    file_id: str
    file_unique_id: str | None = None
    original_name: str | None = None
    declared_mime: str | None = None
    detected_mime: str | None = None
    logical_type: str = "document"
    size_bytes: int = 0
    sha256: str | None = None
    local_path: Path | None = None


@dataclass(frozen=True)
class ReplyContext:
    message_id: int
    author: str
    text: str | None = None
    quote: str | None = None
    attachments: tuple[Attachment, ...] = ()
    thread_id: str | None = None
    turn_id: str | None = None
    source: str = "update"


@dataclass(frozen=True)
class InboundMessage:
    update_ids: tuple[int, ...]
    chat_id: int
    user_id: int
    message_ids: tuple[int, ...]
    text: str
    media_group_id: str | None = None
    attachments: tuple[Attachment, ...] = ()
    reply_context: ReplyContext | None = None
    telegram_message_thread_id: int | None = None
    chat_type: str = "private"


@dataclass(frozen=True)
class OutboundArtifact:
    path: Path
    relative_path: str
    requested_kind: str
    effective_kind: str
    caption: str
    mime_type: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class CodexDelivery:
    text: str
    artifacts: tuple[OutboundArtifact, ...] = ()
    thread_id: str | None = None
    turn_id: str | None = None
    status: str = "completed"


@dataclass(frozen=True)
class TelegramReceipt:
    message_id: int
    file_id: str | None = None
    media_group_id: str | None = None
    metadata: dict[str, str | int | None] = field(default_factory=dict)
