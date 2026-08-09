"""Agendador local seguro para tarefas da instância.

O MVP aceita somente pontos de entrada Python do projeto, sem shell, código inline
ou caminhos arbitrários. A persistência fica no diretório de estado da instância.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence


class SchedulerError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ScheduledTask:
    task_uid: str
    topic_title: str
    trigger: str
    thread_policy: str
    script_path: str | None = None
    prompt: str | None = None
    interval_seconds: int | None = None
    run_at: str | None = None
    enabled: bool = True
    resumable: bool = False
    topic_policy: str = "run"
    telegram_chat_id: int | None = None
    group_alias: str | None = None


def validate_task(task: ScheduledTask, project_root: Path) -> Path | None:
    if not task.task_uid or len(task.task_uid) > 80:
        raise SchedulerError("task_uid inválido.")
    if not task.topic_title or len(task.topic_title.encode("utf-8")) > 128:
        raise SchedulerError("topic_title deve ter entre 1 e 128 bytes.")
    if task.trigger not in {"interval", "once", "event"}:
        raise SchedulerError("trigger inválido.")
    if task.thread_policy not in {"new", "resume"}:
        raise SchedulerError("thread_policy inválido.")
    if task.thread_policy == "resume" and not task.resumable:
        raise SchedulerError("resume exige tarefa marcada como retornável.")
    if task.topic_policy not in {"task", "run", "case"}:
        raise SchedulerError("topic_policy inválida.")
    if task.telegram_chat_id is not None and task.telegram_chat_id >= 0:
        raise SchedulerError("telegram_chat_id deve identificar um grupo.")
    if bool(task.script_path) == bool(task.prompt):
        raise SchedulerError("Informe exatamente script_path ou prompt.")
    if task.trigger == "interval" and (task.interval_seconds or 0) < 60:
        raise SchedulerError("interval_seconds deve ser no mínimo 60.")
    if task.trigger == "once" and not task.run_at:
        raise SchedulerError("run_at é obrigatório para tarefas once.")
    if not task.script_path:
        return None
    candidate = (project_root / task.script_path).resolve()
    root = project_root.resolve()
    if root not in candidate.parents or candidate.suffix.lower() != ".py" or not candidate.is_file():
        raise SchedulerError("script_path deve apontar para um .py existente dentro do projeto.")
    if "data" not in candidate.parts and "interfaces" not in candidate.parts and "skills" not in candidate.parts:
        raise SchedulerError("script_path deve estar em data/, interfaces/ ou skills/.")
    return candidate


class SchedulerStore:
    """Estado persistente de tarefas e execuções, isolado por instância."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.connection:
            self.connection.executescript(
                """CREATE TABLE IF NOT EXISTS tasks(
                    task_uid TEXT PRIMARY KEY, definition_json TEXT NOT NULL,
                    enabled INTEGER NOT NULL, next_run_at TEXT, last_run_at TEXT,
                    updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs(
                    run_uid TEXT PRIMARY KEY, task_uid TEXT NOT NULL,
                    started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
                    result_json TEXT, error TEXT);
                CREATE INDEX IF NOT EXISTS runs_task ON runs(task_uid, started_at);"""
            )

    def close(self) -> None:
        with self.lock:
            self.connection.close()

    def save(self, task: ScheduledTask, project_root: Path) -> None:
        validate_task(task, project_root)
        payload = json.dumps(task.__dict__, ensure_ascii=False, sort_keys=True)
        with self.lock, self.connection:
            self.connection.execute(
                """INSERT INTO tasks(task_uid,definition_json,enabled,next_run_at,last_run_at,updated_at)
                   VALUES(?,?,?,?,NULL,?) ON CONFLICT(task_uid) DO UPDATE SET
                   definition_json=excluded.definition_json, enabled=excluded.enabled,
                   next_run_at=excluded.next_run_at, updated_at=excluded.updated_at""",
                (task.task_uid, payload, int(task.enabled), task.run_at, _now()),
            )

    def due(self, now: str | None = None) -> list[ScheduledTask]:
        now = now or _now()
        rows = self.connection.execute(
            "SELECT definition_json FROM tasks WHERE enabled=1 AND (next_run_at IS NULL OR next_run_at<=?)",
            (now,),
        ).fetchall()
        tasks = [ScheduledTask(**json.loads(row["definition_json"])) for row in rows]
        return [task for task in tasks if task.trigger != "event"]

    def list_tasks(self) -> list[ScheduledTask]:
        rows = self.connection.execute(
            "SELECT definition_json FROM tasks ORDER BY task_uid"
        ).fetchall()
        return [ScheduledTask(**json.loads(row["definition_json"])) for row in rows]

    def set_enabled(self, task_uid: str, enabled: bool) -> bool:
        with self.lock, self.connection:
            cursor = self.connection.execute(
                "UPDATE tasks SET enabled=?, updated_at=? WHERE task_uid=?",
                (int(enabled), _now(), task_uid),
            )
        return cursor.rowcount == 1

    def begin(self, task: ScheduledTask) -> str:
        run_uid = f"run-{uuid.uuid4().hex[:20]}"
        with self.lock, self.connection:
            self.connection.execute(
                "INSERT INTO runs(run_uid,task_uid,started_at,status) VALUES(?,?,?,'running')",
                (run_uid, task.task_uid, _now()),
            )
            if task.trigger == "interval":
                self.connection.execute(
                    "UPDATE tasks SET next_run_at=?,last_run_at=?,updated_at=? WHERE task_uid=?",
                    (datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() + int(task.interval_seconds or 60), timezone.utc).isoformat(), _now(), _now(), task.task_uid),
                )
            elif task.trigger == "once":
                self.connection.execute("UPDATE tasks SET enabled=0,last_run_at=?,updated_at=? WHERE task_uid=?", (_now(), _now(), task.task_uid))
        return run_uid

    def finish(self, run_uid: str, status: str, result: object = None, error: str | None = None) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                "UPDATE runs SET finished_at=?,status=?,result_json=?,error=? WHERE run_uid=?",
                (_now(), status, json.dumps(result, ensure_ascii=False) if result is not None else None, error, run_uid),
            )


def run_python_script(script: Path, project_root: Path, args: Sequence[str] = ()) -> dict[str, object]:
    validate_task(ScheduledTask("runner", "runner", "event", "new", script_path=str(script.relative_to(project_root))), project_root)
    completed = subprocess.run(
        [sys.executable, str(script), *args], cwd=project_root, shell=False,
        capture_output=True, text=True, timeout=300,
    )
    return {"ok": completed.returncode == 0, "returncode": completed.returncode,
            "stdout": completed.stdout[-12000:], "stderr": completed.stderr[-4000:]}


class TaskScheduler:
    def __init__(self, store: SchedulerStore, project_root: Path, callback: Callable[[ScheduledTask, str], object], interval: float = 5.0):
        self.store, self.project_root, self.callback = store, project_root, callback
        self.interval = max(1.0, interval)
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._loop, name="coworker-scheduler", daemon=True)

    def start(self) -> None:
        if not self.thread.is_alive():
            self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=5)

    def _loop(self) -> None:
        while not self.stop_event.wait(self.interval):
            for task in self.store.due():
                try:
                    run_uid = self.store.begin(task)
                    result = self.callback(task, run_uid)
                    self.store.finish(run_uid, "completed", result)
                except Exception as exc:  # callback failures must not kill the gateway
                    try:
                        self.store.finish(run_uid, "failed", error=str(exc))
                    except UnboundLocalError:
                        pass
