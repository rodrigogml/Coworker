"""Apresenta progresso seguro do Codex sem retransmitir raciocínio interno."""

from __future__ import annotations

import re
import time
from collections.abc import Callable

from interfaces.telegram.codex import CodexProgress
from interfaces.telegram.telegram_api import TelegramApi, TelegramApiError


PROGRESS_MODES = ("off", "compact", "detailed")
_MAX_MILESTONES = 6
_MAX_COMMENTARIES = 3
_MIN_DRAFT_INTERVAL_SECONDS = 0.8


class TelegramProgressReporter:
    """Mantém um draft animado e usa uma mensagem editável como fallback."""

    def __init__(
        self,
        api: TelegramApi,
        chat_id: int,
        job_id: int,
        mode: str,
        *,
        send_fallback: Callable[[str], int | None],
        edit_fallback: Callable[[int, str], None],
        warn: Callable[[str], None] | None = None,
    ) -> None:
        if mode not in PROGRESS_MODES or mode == "off":
            raise ValueError("O relator exige progresso compact ou detailed.")
        self.api = api
        self.chat_id = chat_id
        self.draft_id = job_id or 1
        self.mode = mode
        self.send_fallback = send_fallback
        self.edit_fallback = edit_fallback
        self.warn = warn
        self.milestones: list[str] = []
        self.commentaries: list[str] = []
        self.current_commentary = ""
        self.last_render = ""
        self.last_sent_at = 0.0
        self.draft_supported: bool | None = None
        self.fallback_message_id: int | None = None

    def start(self) -> None:
        self.publish(CodexProgress("milestone", "Iniciando o processamento no Codex."))

    def publish(self, progress: CodexProgress) -> None:
        text = self._clean(progress.text)
        if not text:
            return
        if progress.kind == "milestone":
            if text not in self.milestones:
                self.milestones.append(text)
                self.milestones = self.milestones[-_MAX_MILESTONES:]
            self._deliver(force=True)
            return
        if progress.kind != "commentary" or self.mode != "detailed":
            return
        self.current_commentary = text
        if progress.completed and text not in self.commentaries:
            self.commentaries.append(text)
            self.commentaries = self.commentaries[-_MAX_COMMENTARIES:]
        self._deliver(force=progress.completed)

    def finish(self, status: str) -> str | None:
        summary = self._summary(status)
        if self.draft_supported:
            try:
                self.api.send_draft(self.chat_id, self.draft_id, "")
            except TelegramApiError as exc:
                self._warning(f"draft de progresso não removido: {exc}")
        if self.fallback_message_id is not None:
            try:
                self.edit_fallback(self.fallback_message_id, summary)
            except TelegramApiError as exc:
                self._warning(f"fallback de progresso não finalizado: {exc}")
            return None
        if status == "completed" and self.mode == "detailed":
            return summary
        return None

    def _deliver(self, *, force: bool) -> None:
        now = time.monotonic()
        if not force and now - self.last_sent_at < _MIN_DRAFT_INTERVAL_SECONDS:
            return
        rendered = self._render_live()
        if rendered == self.last_render:
            return
        if self.draft_supported is not False:
            try:
                self.api.send_draft(self.chat_id, self.draft_id, rendered)
                self.draft_supported = True
                self.last_render = rendered
                self.last_sent_at = now
                return
            except TelegramApiError as exc:
                self.draft_supported = False
                self._warning(f"sendMessageDraft indisponível; usando fallback: {exc}")
        try:
            if self.fallback_message_id is None:
                self.fallback_message_id = self.send_fallback(rendered)
            else:
                self.edit_fallback(self.fallback_message_id, rendered)
            self.last_render = rendered
            self.last_sent_at = now
        except TelegramApiError as exc:
            self._warning(f"progresso não enviado: {exc}")

    def _render_live(self) -> str:
        lines = [
            "**⏳ Em andamento — ainda não é a resposta final**",
            "",
        ]
        lines.extend(f"- {item}" for item in self.milestones)
        if self.mode == "detailed" and self.current_commentary:
            lines.extend(("", "**Atualização do Codex:**", ""))
            lines.extend(f"> {line}" for line in self.current_commentary.splitlines())
        return "\n".join(lines)[:3500].rstrip()

    def _summary(self, status: str) -> str:
        headings = {
            "completed": "✅ Etapas concluídas",
            "cancelled": "⚠️ Processamento cancelado",
            "failed": "❌ Processamento interrompido",
        }
        lines = [f"**{headings.get(status, 'Processamento encerrado')}**", ""]
        lines.extend(f"- {item}" for item in self.milestones)
        if status == "completed" and self.mode == "detailed" and self.commentaries:
            lines.extend(("", "**Comentários intermediários:**", ""))
            for index, commentary in enumerate(self.commentaries):
                if index:
                    lines.append("> —")
                lines.extend(f"> {line}" for line in commentary.splitlines())
        return "\n".join(lines)[:3500].rstrip()

    @staticmethod
    def _clean(value: str) -> str:
        value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value)
        value = value.strip()
        return value[:2200].rstrip()

    def _warning(self, message: str) -> None:
        if self.warn is not None:
            self.warn(message)
