"""Cliente mínimo e sanitizado para a API HTTP de bots do Telegram."""

from __future__ import annotations

import hashlib
import html
import json
import mimetypes
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from interfaces.telegram.contracts import Attachment, TelegramReceipt


class TelegramApiError(RuntimeError):
    """Representa uma falha da API sem incluir o token na mensagem."""


@dataclass(frozen=True)
class DownloadedFile:
    file_id: str
    original_name: str | None
    path: Path
    mime_type: str | None
    size_bytes: int
    sha256: str
    file_unique_id: str | None = None


class TelegramApi:
    def __init__(
        self, token: str, timeout_seconds: int, assistant_name: str = "A assistente"
    ):
        self._token = token
        self._base = f"https://api.telegram.org/bot{token}/"
        self._file_base = f"https://api.telegram.org/file/bot{token}/"
        self.timeout_seconds = timeout_seconds
        self.assistant_name = assistant_name.strip() or "A assistente"

    def close(self) -> None:
        self._token = ""
        self._base = ""
        self._file_base = ""

    def call(self, method: str, payload: dict[str, Any] | None = None, *, timeout: int | None = None) -> Any:
        body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self._base + method,
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": "Coworker-Telegram/1.0"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout_seconds) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise TelegramApiError(f"A chamada '{method}' ao Telegram falhou.") from exc
        if not isinstance(result, dict) or result.get("ok") is not True:
            description = str(result.get("description", "erro não informado")) if isinstance(result, dict) else "resposta inválida"
            raise TelegramApiError(f"Telegram recusou '{method}': {description}")
        return result.get("result")

    def get_me(self) -> dict[str, Any]:
        return dict(self.call("getMe"))

    def get_commands(self) -> list[dict[str, str]]:
        result = self.call("getMyCommands", {"scope": {"type": "all_private_chats"}})
        return [dict(item) for item in result or []]

    def set_commands(self, commands: tuple[tuple[str, str], ...]) -> bool:
        payload = {
            "commands": [
                {"command": command, "description": description}
                for command, description in commands
            ],
            "scope": {"type": "all_private_chats"},
        }
        return bool(self.call("setMyCommands", payload))

    def get_profile(self) -> dict[str, str]:
        """Consulta os campos públicos editáveis do bot."""
        name = self.call("getMyName") or {}
        short = self.call("getMyShortDescription") or {}
        description = self.call("getMyDescription") or {}
        return {
            "name": str(name.get("name", "")),
            "short_description": str(short.get("short_description", "")),
            "description": str(description.get("description", "")),
        }

    def set_profile(self, *, name: str, short_description: str, description: str) -> bool:
        """Sincroniza nome e bios; o username continua sob controle do BotFather."""
        results = (
            self.call("setMyName", {"name": name}),
            self.call("setMyShortDescription", {"short_description": short_description}),
            self.call("setMyDescription", {"description": description}),
        )
        return all(bool(item) for item in results)

    def delete_webhook(self) -> None:
        self.call("deleteWebhook", {"drop_pending_updates": False})

    def delete_message(self, chat_id: int, message_id: int) -> bool:
        """Remove uma mensagem da conversa privada quando a Bot API permitir."""
        return bool(
            self.call(
                "deleteMessage",
                {"chat_id": chat_id, "message_id": message_id},
            )
        )

    def get_updates(self, offset: int | None, poll_timeout: int) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": poll_timeout,
            "allowed_updates": ["message"],
            "limit": 50,
        }
        if offset is not None:
            payload["offset"] = offset
        result = self.call("getUpdates", payload, timeout=poll_timeout + 10)
        return [dict(item) for item in result or []]

    def call_multipart(
        self,
        method: str,
        fields: dict[str, Any],
        files: dict[str, Path],
    ) -> Any:
        boundary = f"coworker-{uuid.uuid4().hex}"
        body = bytearray()
        for name, value in fields.items():
            body.extend(f"--{boundary}\r\n".encode())
            body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
            serialized = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
            body.extend(serialized.encode("utf-8"))
            body.extend(b"\r\n")
        for name, path in files.items():
            safe_name = sanitize_filename(path.name)
            mime = mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
            body.extend(f"--{boundary}\r\n".encode())
            body.extend(
                f'Content-Disposition: form-data; name="{name}"; filename="{safe_name}"\r\n'.encode()
            )
            body.extend(f"Content-Type: {mime}\r\n\r\n".encode())
            body.extend(path.read_bytes())
            body.extend(b"\r\n")
        body.extend(f"--{boundary}--\r\n".encode())
        request = urllib.request.Request(
            self._base + method,
            data=bytes(body),
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "User-Agent": "Coworker-Telegram/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            raise TelegramApiError(f"O upload '{method}' ao Telegram falhou.") from exc
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise TelegramApiError(f"Telegram recusou o upload '{method}'.")
        return result.get("result")

    def send_text(
        self, chat_id: int, text: str, *, reply_to_message_id: int | None = None
    ) -> list[TelegramReceipt]:
        if not text.strip():
            text = f"{self.assistant_name} concluiu sem produzir uma mensagem de texto."
        chunks = telegram_html_chunks(text)
        receipts: list[TelegramReceipt] = []
        for index, chunk in enumerate(chunks):
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            if index == 0 and reply_to_message_id is not None:
                payload["reply_parameters"] = {"message_id": reply_to_message_id}
            result = self.call(
                "sendMessage",
                payload,
            )
            receipts.append(_receipt(result))
        return receipts

    def send_photo(self, chat_id: int, path: Path, caption: str = "", *, reply_to_message_id: int | None = None) -> TelegramReceipt:
        return self._send_file("sendPhoto", "photo", chat_id, path, caption, reply_to_message_id)

    def send_document(self, chat_id: int, path: Path, caption: str = "", *, reply_to_message_id: int | None = None) -> TelegramReceipt:
        return self._send_file("sendDocument", "document", chat_id, path, caption, reply_to_message_id)

    def send_audio(self, chat_id: int, path: Path, caption: str = "", *, reply_to_message_id: int | None = None) -> TelegramReceipt:
        return self._send_file("sendAudio", "audio", chat_id, path, caption, reply_to_message_id)

    def send_voice(self, chat_id: int, path: Path, caption: str = "", *, reply_to_message_id: int | None = None) -> TelegramReceipt:
        return self._send_file("sendVoice", "voice", chat_id, path, caption, reply_to_message_id)

    def send_video(self, chat_id: int, path: Path, caption: str = "", *, reply_to_message_id: int | None = None) -> TelegramReceipt:
        return self._send_file("sendVideo", "video", chat_id, path, caption, reply_to_message_id)

    def send_animation(self, chat_id: int, path: Path, caption: str = "", *, reply_to_message_id: int | None = None) -> TelegramReceipt:
        return self._send_file("sendAnimation", "animation", chat_id, path, caption, reply_to_message_id)

    def _send_file(
        self,
        method: str,
        field: str,
        chat_id: int,
        path: Path,
        caption: str,
        reply_to_message_id: int | None,
    ) -> TelegramReceipt:
        fields: dict[str, Any] = {"chat_id": chat_id}
        if caption:
            fields.update({"caption": markdown_to_telegram_html(caption[:1024]), "parse_mode": "HTML"})
        if reply_to_message_id is not None:
            fields["reply_parameters"] = {"message_id": reply_to_message_id}
        return _receipt(self.call_multipart(method, fields, {field: path}))

    def send_media_group(
        self,
        chat_id: int,
        items: list[tuple[str, Path, str]],
        *,
        reply_to_message_id: int | None = None,
    ) -> list[TelegramReceipt]:
        if not 2 <= len(items) <= 10 or any(kind not in {"photo", "video", "document", "audio"} for kind, _, _ in items):
            raise TelegramApiError("O grupo de mídia deve conter de 2 a 10 itens compatíveis.")
        media: list[dict[str, str]] = []
        files: dict[str, Path] = {}
        for index, (kind, path, caption) in enumerate(items):
            field = f"file{index}"
            files[field] = path
            item = {"type": kind, "media": f"attach://{field}"}
            if caption:
                item["caption"] = markdown_to_telegram_html(caption[:1024])
                item["parse_mode"] = "HTML"
            media.append(item)
        fields: dict[str, Any] = {"chat_id": chat_id, "media": media}
        if reply_to_message_id is not None:
            fields["reply_parameters"] = {"message_id": reply_to_message_id}
        result = self.call_multipart("sendMediaGroup", fields, files)
        return [_receipt(item) for item in result or []]

    def send_typing(self, chat_id: int) -> None:
        self.call("sendChatAction", {"chat_id": chat_id, "action": "typing"})

    def download(
        self,
        file_id: str,
        original_name: str | None,
        mime_type: str | None,
        destination_root: Path,
        update_id: int,
        max_bytes: int,
        file_unique_id: str | None = None,
    ) -> DownloadedFile:
        info = dict(self.call("getFile", {"file_id": file_id}))
        remote_size = int(info.get("file_size") or 0)
        if remote_size and remote_size > max_bytes:
            raise TelegramApiError("A mídia excede o limite local configurado.")
        remote_path = str(info.get("file_path", ""))
        if not remote_path or ".." in Path(remote_path).parts:
            raise TelegramApiError("O Telegram devolveu um caminho de mídia inválido.")
        folder = destination_root / datetime.now().strftime("%Y") / datetime.now().strftime("%m") / str(update_id)
        folder.mkdir(parents=True, exist_ok=True)
        safe_name = sanitize_filename(original_name or Path(remote_path).name or "arquivo")
        destination = unique_path(folder / safe_name)
        request = urllib.request.Request(self._file_base + urllib.parse.quote(remote_path, safe="/"), headers={"User-Agent": "Coworker-Telegram/1.0"})
        digest = hashlib.sha256()
        size = 0
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response, destination.open("xb") as stream:
                while chunk := response.read(64 * 1024):
                    size += len(chunk)
                    if size > max_bytes:
                        raise TelegramApiError("A mídia excede o limite local configurado.")
                    digest.update(chunk)
                    stream.write(chunk)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        detected_mime = mime_type or mimetypes.guess_type(destination.name)[0]
        metadata = {
            "schema_version": 1,
            "update_id": update_id,
            "telegram_file_id": file_id,
            "original_name": original_name,
            "local_name": destination.name,
            "mime_type": detected_mime,
            "size_bytes": size,
            "sha256": digest.hexdigest(),
        }
        (folder / f"{destination.name}.metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return DownloadedFile(
            file_id, original_name, destination.resolve(), detected_mime, size,
            digest.hexdigest(), file_unique_id,
        )


def sanitize_filename(value: str) -> str:
    name = Path(value).name.strip().replace("\0", "")
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    if not name:
        name = "arquivo"
    stem = Path(name).stem[:100] or "arquivo"
    suffix = Path(name).suffix[:15]
    return stem + suffix


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(1, 10_000):
        candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise TelegramApiError("Não foi possível definir um nome local único para a mídia.")


def split_text(text: str, limit: int = 3900) -> list[str]:
    normalized = text.strip() or "A assistente concluiu sem produzir uma mensagem de texto."
    chunks: list[str] = []
    while len(normalized) > limit:
        boundary = normalized.rfind("\n", 0, limit)
        if boundary < limit // 2:
            boundary = normalized.rfind(" ", 0, limit)
        if boundary < limit // 2:
            boundary = limit
        chunks.append(normalized[:boundary].rstrip())
        normalized = normalized[boundary:].lstrip()
    chunks.append(normalized)
    return chunks


def telegram_html_chunks(text: str, limit: int = 3800) -> list[str]:
    """Converte Markdown comum do Codex em HTML seguro aceito pelo Telegram."""
    raw_chunks = split_text(text, limit=max(1, limit - 32))
    formatted: list[str] = []
    inside_fence = False
    fence_language = ""
    for raw_chunk in raw_chunks:
        prefix = f"```{fence_language}\n" if inside_fence else ""
        for match in re.finditer(r"(?m)^\s*```([^\r\n`]*)\s*$", raw_chunk):
            if inside_fence:
                inside_fence = False
                fence_language = ""
            else:
                inside_fence = True
                fence_language = _safe_code_language(match.group(1))
        suffix = "\n```" if inside_fence else ""
        formatted.append(markdown_to_telegram_html(prefix + raw_chunk + suffix))
    return formatted


def markdown_to_telegram_html(value: str) -> str:
    """Renderiza somente o subconjunto de HTML documentado pela Bot API."""
    output: list[str] = []
    code_lines: list[str] = []
    code_language = ""
    inside_code = False

    for line in value.splitlines():
        fence = re.fullmatch(r"\s*```([^`]*)\s*", line)
        if fence:
            if inside_code:
                output.append(_code_block(code_lines, code_language))
                code_lines = []
                code_language = ""
                inside_code = False
            else:
                code_language = _safe_code_language(fence.group(1))
                inside_code = True
            continue
        if inside_code:
            code_lines.append(line)
            continue
        output.append(_format_markdown_line(line))

    if inside_code:
        output.append(_code_block(code_lines, code_language))
    return "\n".join(output).strip() or "A assistente concluiu sem produzir uma mensagem de texto."


def _format_markdown_line(line: str) -> str:
    heading = re.fullmatch(r"\s{0,3}#{1,6}\s+(.+?)\s*#*", line)
    if heading:
        return f"<b>{_format_inline_markdown(heading.group(1))}</b>"
    quote = re.fullmatch(r"\s*>\s?(.*)", line)
    if quote:
        return f"<blockquote>{_format_inline_markdown(quote.group(1))}</blockquote>"
    bullet = re.fullmatch(r"(\s*)[-+*]\s+(.+)", line)
    if bullet:
        return f"{bullet.group(1)}• {_format_inline_markdown(bullet.group(2))}"
    if re.fullmatch(r"\s*(?:---+|___+|\*\*\*+)\s*", line):
        return "────────"
    return _format_inline_markdown(line)


def _format_inline_markdown(value: str) -> str:
    protected: list[str] = []

    def preserve(rendered: str) -> str:
        token = f"\x02COWORKER{len(protected)}\x03"
        protected.append(rendered)
        return token

    value = re.sub(
        r"(?<!`)`([^`\r\n]+)`(?!`)",
        lambda match: preserve(f"<code>{html.escape(match.group(1), quote=False)}</code>"),
        value,
    )

    def replace_link(match: re.Match[str]) -> str:
        label = html.escape(match.group(1), quote=False)
        target = match.group(2).strip()
        if re.match(r"^(?:https?://|mailto:|tg://)", target, flags=re.IGNORECASE):
            return preserve(f'<a href="{html.escape(target, quote=True)}">{label}</a>')
        return preserve(f"{label} ({html.escape(target, quote=False)})")

    value = re.sub(r"\[([^\]\r\n]+)]\(([^)\r\n]+)\)", replace_link, value)
    value = html.escape(value, quote=False)
    value = re.sub(r"\*\*([^*\r\n]+)\*\*", r"<b>\1</b>", value)
    value = re.sub(r"__([^_\r\n]+)__", r"<b>\1</b>", value)
    value = re.sub(r"~~([^~\r\n]+)~~", r"<s>\1</s>", value)
    value = re.sub(r"(?<!\*)\*([^*\r\n]+)\*(?!\*)", r"<i>\1</i>", value)
    for index, rendered in enumerate(protected):
        value = value.replace(html.escape(f"\x02COWORKER{index}\x03", quote=False), rendered)
    return value


def _safe_code_language(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_+.-]", "", value.strip())[:40]


def _code_block(lines: list[str], language: str) -> str:
    content = html.escape("\n".join(lines), quote=False)
    if language:
        return f'<pre><code class="language-{language}">{content}</code></pre>'
    return f"<pre>{content}</pre>"


def media_descriptors(message: dict[str, Any]) -> list[tuple[str, str | None, str | None]]:
    """Extrai IDs e metadados sem baixar conteúdo de usuários não autorizados."""
    if isinstance(message.get("document"), dict):
        item = message["document"]
        return [(str(item.get("file_id", "")), item.get("file_name"), item.get("mime_type"))]
    if isinstance(message.get("photo"), list) and message["photo"]:
        item = message["photo"][-1]
        return [(str(item.get("file_id", "")), f"foto-{message.get('message_id', 'telegram')}.jpg", "image/jpeg")]
    for kind, extension, default_mime in (
        ("audio", ".mp3", "audio/mpeg"),
        ("voice", ".ogg", "audio/ogg"),
        ("video", ".mp4", "video/mp4"),
    ):
        if isinstance(message.get(kind), dict):
            item = message[kind]
            name = item.get("file_name") or f"{kind}-{message.get('message_id', 'telegram')}{extension}"
            return [(str(item.get("file_id", "")), name, item.get("mime_type") or default_mime)]
    return []


def message_attachments(message: dict[str, Any], origin: str = "current") -> list[Attachment]:
    result: list[Attachment] = []
    if isinstance(message.get("document"), dict):
        item = message["document"]
        result.append(_telegram_attachment(item, origin, item.get("file_name"), item.get("mime_type"), "document"))
    if isinstance(message.get("photo"), list) and message["photo"]:
        item = message["photo"][-1]
        result.append(_telegram_attachment(item, origin, f"foto-{message.get('message_id', 'telegram')}.jpg", "image/jpeg", "photo"))
    for kind, extension, default_mime in (
        ("audio", ".mp3", "audio/mpeg"),
        ("voice", ".ogg", "audio/ogg"),
        ("video", ".mp4", "video/mp4"),
        ("animation", ".gif", "image/gif"),
    ):
        if isinstance(message.get(kind), dict):
            item = message[kind]
            name = item.get("file_name") or f"{kind}-{message.get('message_id', 'telegram')}{extension}"
            result.append(_telegram_attachment(item, origin, name, item.get("mime_type") or default_mime, kind))
    return result


def _telegram_attachment(
    item: dict[str, Any], origin: str, name: str | None, mime: str | None, logical_type: str
) -> Attachment:
    return Attachment(
        origin=origin,
        file_id=str(item.get("file_id") or ""),
        file_unique_id=str(item.get("file_unique_id") or "") or None,
        original_name=str(name) if name else None,
        declared_mime=str(mime) if mime else None,
        logical_type=logical_type,
        size_bytes=int(item.get("file_size") or 0),
    )


def _receipt(message: Any) -> TelegramReceipt:
    if not isinstance(message, dict):
        return TelegramReceipt(0)
    file_id: str | None = None
    for key in ("document", "audio", "voice", "video", "animation"):
        item = message.get(key)
        if isinstance(item, dict) and item.get("file_id"):
            file_id = str(item["file_id"])
            break
    photos = message.get("photo")
    if file_id is None and isinstance(photos, list) and photos and isinstance(photos[-1], dict):
        file_id = str(photos[-1].get("file_id") or "") or None
    return TelegramReceipt(
        int(message.get("message_id") or 0),
        file_id,
        str(message.get("media_group_id") or "") or None,
    )
