"""Estado persistente de tarefas, execuções e vínculos Telegram/Codex.

O armazenamento não executa scripts e não concede permissões. Ele apenas registra
contratos validados, estados operacionais e o mapeamento 1:1 das conversas.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from interfaces.telegram.automation import (
    AutomationContractError,
    AutomationTask,
    validate_task_definition,
)


class AutomationStateError(RuntimeError):
    """Indica estado inexistente, duplicado ou inconsistente."""


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class AutomationState:
    """Persistência SQLite local para scheduler e roteamento de conversas."""

    def __init__(self, path: Path):
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self._initialize()

    def _initialize(self) -> None:
        with self.connection:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS automation_groups (
                    alias TEXT PRIMARY KEY,
                    chat_id INTEGER NOT NULL UNIQUE,
                    valid INTEGER NOT NULL DEFAULT 0,
                    diagnostics_json TEXT NOT NULL DEFAULT '{}',
                    checked_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS automation_tasks (
                    task_uid TEXT PRIMARY KEY,
                    topic_title TEXT NOT NULL,
                    topic_policy TEXT NOT NULL,
                    thread_policy TEXT NOT NULL,
                    resumable INTEGER NOT NULL,
                    trigger TEXT NOT NULL,
                    script_id TEXT,
                    prompt TEXT,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    group_alias TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(group_alias) REFERENCES automation_groups(alias)
                );
                CREATE TABLE IF NOT EXISTS automation_runs (
                    run_uid TEXT PRIMARY KEY,
                    task_uid TEXT NOT NULL,
                    event_uid TEXT,
                    status TEXT NOT NULL,
                    codex_thread_id TEXT UNIQUE,
                    telegram_chat_id INTEGER,
                    telegram_message_thread_id INTEGER,
                    telegram_root_message_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(task_uid) REFERENCES automation_tasks(task_uid)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS automation_run_telegram_topic
                    ON automation_runs(telegram_chat_id, telegram_message_thread_id)
                    WHERE telegram_chat_id IS NOT NULL
                      AND telegram_message_thread_id IS NOT NULL;
                CREATE UNIQUE INDEX IF NOT EXISTS automation_event_once
                    ON automation_runs(task_uid, event_uid)
                    WHERE event_uid IS NOT NULL;
                """
            )

    def close(self) -> None:
        self.connection.close()

    def upsert_group(
        self,
        alias: str,
        chat_id: int,
        *,
        valid: bool,
        diagnostics: Mapping[str, Any] | None = None,
    ) -> None:
        """Registra o diagnóstico do grupo sem armazenar credenciais."""
        if not alias or chat_id >= 0:
            raise AutomationStateError("Grupo exige alias e chat_id negativo.")
        with self.connection:
            self.connection.execute(
                """INSERT INTO automation_groups
                   (alias, chat_id, valid, diagnostics_json, checked_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(alias) DO UPDATE SET
                     chat_id=excluded.chat_id,
                     valid=excluded.valid,
                     diagnostics_json=excluded.diagnostics_json,
                     checked_at=excluded.checked_at""",
                (alias, chat_id, int(valid), json.dumps(dict(diagnostics or {}), ensure_ascii=False), _now()),
            )

    def group_valid(self, alias: str) -> bool:
        row = self.connection.execute(
            "SELECT valid FROM automation_groups WHERE alias = ?", (alias,)
        ).fetchone()
        return bool(row and row["valid"])

    def save_task(self, definition: Mapping[str, Any], *, group_alias: str | None = None) -> AutomationTask:
        """Valida e salva uma tarefa; habilitação exige grupo válido."""
        group_valid = group_alias is None or self.group_valid(group_alias)
        try:
            task = validate_task_definition(definition, group_valid=group_valid)
        except AutomationContractError as exc:
            raise AutomationStateError(str(exc)) from exc
        now = _now()
        with self.connection:
            self.connection.execute(
                """INSERT INTO automation_tasks
                   (task_uid, topic_title, topic_policy, thread_policy, resumable,
                    trigger, script_id, prompt, enabled, group_alias, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(task_uid) DO UPDATE SET
                    topic_title=excluded.topic_title,
                    topic_policy=excluded.topic_policy,
                    thread_policy=excluded.thread_policy,
                    resumable=excluded.resumable,
                    trigger=excluded.trigger,
                    script_id=excluded.script_id,
                    prompt=excluded.prompt,
                    enabled=excluded.enabled,
                    group_alias=excluded.group_alias,
                    updated_at=excluded.updated_at""",
                (
                    task.task_uid,
                    task.topic_title,
                    task.topic_policy,
                    task.thread_policy,
                    int(task.resumable),
                    task.trigger,
                    task.script_id,
                    task.prompt,
                    int(task.enabled),
                    group_alias,
                    now,
                    now,
                ),
            )
        return task

    def task(self, task_uid: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM automation_tasks WHERE task_uid=?", (task_uid,)
        ).fetchone()
        return dict(row) if row else None

    def set_task_enabled(self, task_uid: str, enabled: bool) -> None:
        row = self.connection.execute(
            "SELECT group_alias FROM automation_tasks WHERE task_uid=?", (task_uid,)
        ).fetchone()
        if row is None:
            raise AutomationStateError("Tarefa não encontrada.")
        if enabled and row["group_alias"] and not self.group_valid(row["group_alias"]):
            raise AutomationStateError("Grupo Telegram inválido; tarefa não pode ser habilitada.")
        with self.connection:
            self.connection.execute(
                "UPDATE automation_tasks SET enabled=?,updated_at=? WHERE task_uid=?",
                (int(enabled), _now(), task_uid),
            )

    def create_run(self, run_uid: str, task_uid: str, *, event_uid: str | None = None) -> None:
        """Cria uma execução idempotente e rejeita evento duplicado."""
        try:
            with self.connection:
                self.connection.execute(
                    """INSERT INTO automation_runs
                       (run_uid, task_uid, event_uid, status, created_at, updated_at)
                       VALUES (?, ?, ?, 'queued', ?, ?)""",
                    (run_uid, task_uid, event_uid, _now(), _now()),
                )
        except sqlite3.IntegrityError as exc:
            raise AutomationStateError("A execução ou evento já foi registrado.") from exc

    def bind_conversation(
        self,
        run_uid: str,
        *,
        codex_thread_id: str,
        telegram_chat_id: int,
        telegram_message_thread_id: int,
        telegram_root_message_id: int,
    ) -> None:
        """Vincula exatamente uma thread Codex a um tópico Telegram."""
        if telegram_chat_id >= 0:
            raise AutomationStateError("A conversa Telegram deve ser um grupo/supergrupo.")
        if not codex_thread_id.strip():
            raise AutomationStateError("codex_thread_id é obrigatório.")
        existing = self.connection.execute(
            """SELECT codex_thread_id, telegram_chat_id, telegram_message_thread_id
               FROM automation_runs WHERE run_uid=?""",
            (run_uid,),
        ).fetchone()
        if existing is None:
            raise AutomationStateError("Execução não encontrada.")
        if any(
            existing[key] is not None
            and existing[key] != value
            for key, value in (
                ("codex_thread_id", codex_thread_id.strip()),
                ("telegram_chat_id", telegram_chat_id),
                ("telegram_message_thread_id", telegram_message_thread_id),
            )
        ):
            raise AutomationStateError("A execução já possui outro vínculo de conversa.")
        try:
            with self.connection:
                cursor = self.connection.execute(
                    """UPDATE automation_runs SET
                       codex_thread_id=?, telegram_chat_id=?,
                       telegram_message_thread_id=?, telegram_root_message_id=?,
                       updated_at=? WHERE run_uid=?""",
                    (
                        codex_thread_id.strip(),
                        telegram_chat_id,
                        telegram_message_thread_id,
                        telegram_root_message_id,
                        _now(),
                        run_uid,
                    ),
                )
                if cursor.rowcount != 1:
                    raise AutomationStateError("Execução não encontrada.")
        except sqlite3.IntegrityError as exc:
            raise AutomationStateError(
                "A thread Codex ou o tópico Telegram já está vinculado a outra execução."
            ) from exc

    def run_for_topic(self, chat_id: int, message_thread_id: int) -> dict[str, Any] | None:
        """Busca a execução pelo par imutável do tópico Telegram."""
        row = self.connection.execute(
            """SELECT * FROM automation_runs
               WHERE telegram_chat_id=? AND telegram_message_thread_id=?""",
            (chat_id, message_thread_id),
        ).fetchone()
        return dict(row) if row else None

    def run_for_codex_thread(self, codex_thread_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM automation_runs WHERE codex_thread_id=?",
            (codex_thread_id,),
        ).fetchone()
        return dict(row) if row else None

    def update_run_status(self, run_uid: str, status: str) -> None:
        if status not in {"queued", "running", "waiting-agent", "succeeded", "failed", "unknown", "cancelled", "notification_pending"}:
            raise AutomationStateError("Estado de execução inválido.")
        with self.connection:
            self.connection.execute(
                "UPDATE automation_runs SET status=?,updated_at=? WHERE run_uid=?",
                (status, _now(), run_uid),
            )

    def set_codex_thread(self, run_uid: str, codex_thread_id: str) -> None:
        if not codex_thread_id.strip():
            raise AutomationStateError("codex_thread_id é obrigatório.")
        try:
            with self.connection:
                self.connection.execute(
                    "UPDATE automation_runs SET codex_thread_id=?,updated_at=? WHERE run_uid=?",
                    (codex_thread_id.strip(), _now(), run_uid),
                )
        except sqlite3.IntegrityError as exc:
            raise AutomationStateError("A thread Codex já está vinculada a outra execução.") from exc
