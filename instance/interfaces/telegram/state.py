"""Persistência operacional e idempotente da interface Telegram."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import string
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = PROJECT_ROOT / "migrations" / "telegram"

class StateError(RuntimeError):
    """Representa uma operação inválida no estado do gateway."""


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def pin_digest(pin: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", pin.encode("ascii"), salt, 200_000)


@dataclass(frozen=True)
class Owner:
    user_id: int
    chat_id: int
    display_name: str
    username: str | None
    paired_at: str


@dataclass(frozen=True)
class PairingCandidate:
    approval_code: str
    user_id: int
    chat_id: int
    display_name: str
    username: str | None
    expires_at: str


@dataclass(frozen=True)
class CodexPreferences:
    model: str | None = None
    reasoning_effort: str | None = None
    speed: str | None = None
    verbosity: str | None = None
    progress_mode: str = "off"


class StateStore:
    """Controla autorização, sessões e mensagens em um SQLite exclusivo da máquina."""

    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "telegram.sqlite3"
        self.connection = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self._initialize()

    def close(self) -> None:
        self.connection.close()

    def _initialize(self) -> None:
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
               version TEXT PRIMARY KEY, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)"""
        )
        migrations = sorted(MIGRATIONS_DIR.glob("*.sql"))
        if not migrations:
            raise StateError(f"Nenhuma migration foi encontrada em '{MIGRATIONS_DIR}'.")
        for path in migrations:
            try:
                migration = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise StateError(f"Não foi possível ler a migration '{path}'.") from exc
            checksum = hashlib.sha256(migration.encode("utf-8")).hexdigest()
            row = self.connection.execute(
                "SELECT checksum FROM schema_migrations WHERE version=?", (path.name,)
            ).fetchone()
            if row:
                if not hmac.compare_digest(str(row["checksum"]), checksum):
                    raise StateError(f"A migration já aplicada '{path.name}' foi alterada.")
                continue
            version = path.name.replace("'", "''")
            applied_at = iso().replace("'", "''")
            script = (
                "BEGIN IMMEDIATE;\n"
                + migration
                + "\nINSERT INTO schema_migrations(version,checksum,applied_at) VALUES "
                + f"('{version}','{checksum}','{applied_at}');\nCOMMIT;"
            )
            try:
                self.connection.executescript(script)
            except sqlite3.Error as exc:
                self.connection.rollback()
                raise StateError(f"Falha ao aplicar a migration '{path.name}'.") from exc
        self.connection.commit()

    def owner(self) -> Owner | None:
        row = self.connection.execute(
            "SELECT * FROM authorized_users WHERE role='owner' AND revoked_at IS NULL"
        ).fetchone()
        return (
            Owner(row["user_id"], row["chat_id"], row["display_name"], row["username"], row["paired_at"])
            if row
            else None
        )

    def is_authorized(self, user_id: int, chat_id: int) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM authorized_users WHERE user_id=? AND chat_id=? AND revoked_at IS NULL",
            (user_id, chat_id),
        ).fetchone() is not None

    def group_member_allowed(self, user_id: int, chat_id: int) -> bool:
        owner = self.owner()
        if owner and owner.user_id == user_id:
            return True
        return self.connection.execute(
            "SELECT 1 FROM telegram_group_members WHERE user_id=? AND chat_id=? AND revoked_at IS NULL",
            (user_id, chat_id),
        ).fetchone() is not None

    def grant_group_member(self, chat_id: int, user_id: int, role: str = "member") -> None:
        if role not in {"owner", "admin", "member"}:
            raise StateError("Papel de grupo inválido.")
        self.connection.execute(
            """INSERT INTO telegram_group_members(chat_id,user_id,role,granted_at,revoked_at)
               VALUES (?,?,?,?,NULL)
               ON CONFLICT(chat_id,user_id) DO UPDATE SET role=excluded.role,
               granted_at=excluded.granted_at, revoked_at=NULL""",
            (chat_id, user_id, role, iso()),
        )
        self.connection.commit()

    def begin_pairing(self, ttl_seconds: int, max_attempts: int) -> tuple[str, str]:
        if self.owner():
            raise StateError("Já existe uma proprietária vinculada.")
        now = utc_now()
        self.connection.execute(
            "UPDATE pairing_requests SET status='cancelled' WHERE status IN ('open','pending')"
        )
        pin = f"{secrets.randbelow(1_000_000):06d}"
        salt = secrets.token_bytes(16)
        expires_at = now + timedelta(seconds=ttl_seconds)
        self.connection.execute(
            """INSERT INTO pairing_requests
               (pin_salt,pin_digest,created_at,expires_at,max_attempts,status)
               VALUES (?,?,?,?,?,'open')""",
            (salt, pin_digest(pin, salt), iso(now), iso(expires_at), max_attempts),
        )
        self.connection.commit()
        return pin, iso(expires_at)

    def request_pairing(
        self,
        pin: str,
        user_id: int,
        chat_id: int,
        display_name: str,
        username: str | None,
    ) -> PairingCandidate:
        if self.owner():
            raise StateError("O pareamento inicial já foi concluído.")
        row = self.connection.execute(
            "SELECT * FROM pairing_requests WHERE status='open' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            raise StateError("Não existe uma janela de pareamento aberta.")
        if parse_iso(row["expires_at"]) <= utc_now():
            self.connection.execute(
                "UPDATE pairing_requests SET status='expired' WHERE id=?", (row["id"],)
            )
            self.connection.commit()
            raise StateError("O PIN expirou. Gere outro localmente.")
        attempts = int(row["attempts"]) + 1
        valid = hmac.compare_digest(pin_digest(pin, row["pin_salt"]), row["pin_digest"])
        if not valid:
            status = "blocked" if attempts >= int(row["max_attempts"]) else "open"
            self.connection.execute(
                "UPDATE pairing_requests SET attempts=?, status=? WHERE id=?",
                (attempts, status, row["id"]),
            )
            self.connection.commit()
            raise StateError(
                "PIN inválido. O pareamento foi bloqueado."
                if status == "blocked"
                else "PIN inválido."
            )
        alphabet = string.ascii_uppercase + string.digits
        approval_code = "".join(secrets.choice(alphabet) for _ in range(6))
        self.connection.execute(
            """UPDATE pairing_requests SET status='pending', attempts=?,
               candidate_user_id=?,candidate_chat_id=?,candidate_name=?,
               candidate_username=?,approval_code=? WHERE id=?""",
            (attempts, user_id, chat_id, display_name, username, approval_code, row["id"]),
        )
        self.connection.commit()
        return PairingCandidate(approval_code, user_id, chat_id, display_name, username, row["expires_at"])

    def pending_pairing(self) -> PairingCandidate | None:
        row = self.connection.execute(
            "SELECT * FROM pairing_requests WHERE status='pending' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        return PairingCandidate(
            row["approval_code"], row["candidate_user_id"], row["candidate_chat_id"],
            row["candidate_name"], row["candidate_username"], row["expires_at"]
        )

    def approve_pairing(self, approval_code: str) -> Owner:
        if self.owner():
            raise StateError("Já existe uma proprietária vinculada.")
        row = self.connection.execute(
            "SELECT * FROM pairing_requests WHERE status='pending' AND approval_code=?",
            (approval_code.strip().upper(),),
        ).fetchone()
        if not row:
            raise StateError("Código de aprovação inválido ou inexistente.")
        if parse_iso(row["expires_at"]) <= utc_now():
            self.connection.execute(
                "UPDATE pairing_requests SET status='expired' WHERE id=?", (row["id"],)
            )
            self.connection.commit()
            raise StateError("A solicitação de pareamento expirou.")
        paired_at = iso()
        with self.connection:
            self.connection.execute(
                """INSERT INTO authorized_users
                   (user_id,chat_id,role,display_name,username,paired_at)
                   VALUES (?,?, 'owner', ?,?,?)""",
                (row["candidate_user_id"], row["candidate_chat_id"], row["candidate_name"], row["candidate_username"], paired_at),
            )
            self.connection.execute(
                "UPDATE pairing_requests SET status='approved' WHERE id=?", (row["id"],)
            )
        owner = self.owner()
        assert owner is not None
        return owner

    def cancel_pairing(self) -> bool:
        cursor = self.connection.execute(
            "UPDATE pairing_requests SET status='cancelled' WHERE status IN ('open','pending')"
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def update_seen(self, update_id: int) -> bool:
        try:
            self.connection.execute(
                "INSERT INTO telegram_updates(update_id,received_at,status) VALUES (?,?,'received')",
                (update_id, iso()),
            )
            self.connection.commit()
            return False
        except sqlite3.IntegrityError:
            return True

    def finish_update(self, update_id: int, status: str, error: str | None = None) -> None:
        self.connection.execute(
            "UPDATE telegram_updates SET processed_at=?,status=?,error=? WHERE update_id=?",
            (iso(), status, error, update_id),
        )
        self.connection.commit()

    def session(self, chat_id: int) -> str | None:
        row = self.connection.execute(
            "SELECT thread_id FROM codex_sessions WHERE chat_id=? AND active=1", (chat_id,)
        ).fetchone()
        return str(row["thread_id"]) if row and row["thread_id"] else None

    def set_session(self, chat_id: int, thread_id: str) -> None:
        now = iso()
        self.connection.execute(
            """INSERT INTO codex_sessions(chat_id,thread_id,created_at,last_used_at,active)
               VALUES (?,?,?,?,1) ON CONFLICT(chat_id) DO UPDATE SET
               thread_id=excluded.thread_id,last_used_at=excluded.last_used_at,active=1""",
            (chat_id, thread_id, now, now),
        )
        self.connection.commit()

    def clear_session(self, chat_id: int) -> bool:
        cursor = self.connection.execute(
            "UPDATE codex_sessions SET active=0,last_used_at=? WHERE chat_id=? AND active=1",
            (iso(), chat_id),
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def codex_preferences(self, chat_id: int) -> CodexPreferences:
        row = self.connection.execute(
            """SELECT model,reasoning_effort,speed,verbosity,progress_mode
               FROM codex_preferences WHERE chat_id=?""",
            (chat_id,),
        ).fetchone()
        if not row:
            return CodexPreferences()
        return CodexPreferences(
            model=str(row["model"]) if row["model"] else None,
            reasoning_effort=(
                str(row["reasoning_effort"]) if row["reasoning_effort"] else None
            ),
            speed=str(row["speed"]) if row["speed"] else None,
            verbosity=str(row["verbosity"]) if row["verbosity"] else None,
            progress_mode=str(row["progress_mode"] or "off"),
        )

    def set_codex_preference(self, chat_id: int, field: str, value: str | None) -> None:
        allowed = {
            "model",
            "reasoning_effort",
            "speed",
            "verbosity",
            "progress_mode",
        }
        if field not in allowed:
            raise StateError("Campo de configuração do Codex inválido.")
        try:
            with self.connection:
                self.connection.execute(
                    """INSERT INTO codex_preferences(chat_id,updated_at)
                       VALUES (?,?) ON CONFLICT(chat_id) DO NOTHING""",
                    (chat_id, iso()),
                )
                self.connection.execute(
                    f"UPDATE codex_preferences SET {field}=?,updated_at=? WHERE chat_id=?",
                    (value, iso(), chat_id),
                )
        except sqlite3.IntegrityError as exc:
            raise StateError("Valor de configuração do Codex inválido.") from exc

    def reset_codex_preferences(self, chat_id: int) -> bool:
        cursor = self.connection.execute(
            "DELETE FROM codex_preferences WHERE chat_id=?", (chat_id,)
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def create_job(
        self,
        update_id: int,
        chat_id: int,
        *,
        media_group_id: str | None = None,
        request_message_id: int | None = None,
    ) -> int:
        cursor = self.connection.execute(
            """INSERT INTO jobs
               (update_id,chat_id,status,media_group_id,request_message_id)
               VALUES (?,?,'queued',?,?)""",
            (update_id, chat_id, media_group_id, request_message_id),
        )
        job_id = int(cursor.lastrowid)
        self.connection.execute(
            "INSERT INTO job_updates(job_id,update_id,message_record_id) VALUES (?,?,?)",
            (job_id, update_id, request_message_id),
        )
        self.connection.commit()
        return job_id

    def attach_update_to_job(
        self, job_id: int, update_id: int, message_record_id: int | None
    ) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO job_updates(job_id,update_id,message_record_id) VALUES (?,?,?)",
            (job_id, update_id, message_record_id),
        )
        self.connection.commit()

    def set_job_workspace(self, job_id: int, workspace: Path) -> None:
        self.connection.execute(
            "UPDATE jobs SET workspace_path=? WHERE id=?", (str(workspace), job_id)
        )
        self.connection.commit()

    def update_job_context(
        self, job_id: int, *, thread_id: str | None = None, turn_id: str | None = None
    ) -> None:
        self.connection.execute(
            """UPDATE jobs SET thread_id=COALESCE(?,thread_id),
               turn_id=COALESCE(?,turn_id) WHERE id=?""",
            (thread_id, turn_id, job_id),
        )
        self.connection.commit()

    def set_job_response(self, job_id: int, message_record_id: int) -> None:
        self.connection.execute(
            """UPDATE jobs SET response_message_id=COALESCE(response_message_id,?)
               WHERE id=?""",
            (message_record_id, job_id),
        )
        self.connection.commit()

    def current_job(self) -> dict[str, Any] | None:
        row = self.connection.execute(
            """SELECT id,status,thread_id,turn_id FROM jobs
               WHERE status IN ('queued','running') ORDER BY id LIMIT 1"""
        ).fetchone()
        return dict(row) if row else None

    def update_job(self, job_id: int, status: str, *, pid: int | None = None, error: str | None = None) -> None:
        started = iso() if status == "running" else None
        finished = iso() if status in {"completed", "failed", "cancelled"} else None
        self.connection.execute(
            """UPDATE jobs SET status=?,pid=COALESCE(?,pid),
               started_at=COALESCE(?,started_at),finished_at=COALESCE(?,finished_at),error=?
               WHERE id=?""",
            (status, pid, started, finished, error, job_id),
        )
        self.connection.commit()

    def job_status(self, job_id: int) -> str | None:
        row = self.connection.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        return str(row["status"]) if row else None

    def job_accepts_credential_request(self, job_id: int, chat_id: int) -> bool:
        row = self.connection.execute(
            """SELECT 1 FROM jobs
               WHERE id=? AND chat_id=? AND status='running'""",
            (job_id, chat_id),
        ).fetchone()
        return row is not None

    def cancel_queued_jobs(self, chat_id: int) -> int:
        cursor = self.connection.execute(
            "UPDATE jobs SET status='cancelled',finished_at=? WHERE chat_id=? AND status='queued'",
            (iso(), chat_id),
        )
        self.connection.commit()
        return cursor.rowcount

    def recover_interrupted_jobs(self) -> int:
        self.connection.execute(
            """UPDATE artifacts SET upload_state='unknown',updated_at=?
               WHERE upload_state='uploading'""",
            (iso(),),
        )
        cursor = self.connection.execute(
            """UPDATE jobs SET status='failed',finished_at=?,
               error='Execução interrompida pelo encerramento anterior da interface.'
               WHERE status IN ('queued','running')""",
            (iso(),),
        )
        self.connection.commit()
        return cursor.rowcount

    def job_summary(self) -> dict[str, int]:
        rows = self.connection.execute(
            "SELECT status,COUNT(*) AS total FROM jobs WHERE status IN ('queued','running') GROUP BY status"
        ).fetchall()
        result = {"queued": 0, "running": 0}
        result.update({str(row["status"]): int(row["total"]) for row in rows})
        return result

    def record_message(
        self,
        update_id: int | None,
        chat_id: int,
        message_id: int | None,
        direction: str,
        text: str | None,
        status: str,
        *,
        reply_to_message_id: int | None = None,
        thread_id: str | None = None,
        turn_id: str | None = None,
        media_group_id: str | None = None,
        content_type: str | None = None,
        job_id: int | None = None,
        sender_user_id: int | None = None,
        telegram_message_thread_id: int | None = None,
        chat_type: str | None = None,
    ) -> int:
        cursor = self.connection.execute(
            """INSERT INTO messages
               (update_id,chat_id,message_id,direction,text,created_at,status,
                reply_to_message_id,thread_id,turn_id,media_group_id,content_type,job_id,
                sender_user_id,telegram_message_thread_id,chat_type)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                update_id, chat_id, message_id, direction, text, iso(), status,
                reply_to_message_id, thread_id, turn_id, media_group_id,
                content_type, job_id, sender_user_id,
                telegram_message_thread_id, chat_type,
            ),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def message(self, chat_id: int, telegram_message_id: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            """SELECT * FROM messages WHERE chat_id=? AND message_id=?
               ORDER BY id DESC LIMIT 1""",
            (chat_id, telegram_message_id),
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["attachments"] = [
            dict(item)
            for item in self.connection.execute(
                "SELECT * FROM attachments WHERE message_record_id=? ORDER BY id", (row["id"],)
            ).fetchall()
        ]
        return result

    def topic_session(self, chat_id: int, message_thread_id: int) -> str | None:
        row = self.connection.execute(
            "SELECT codex_thread_id FROM telegram_topic_sessions WHERE chat_id=? AND message_thread_id=?",
            (chat_id, message_thread_id),
        ).fetchone()
        return str(row["codex_thread_id"]) if row and row["codex_thread_id"] else None

    def set_topic_session(self, chat_id: int, message_thread_id: int, thread_id: str) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT INTO telegram_topic_sessions(chat_id,message_thread_id,codex_thread_id,last_used_at)
                   VALUES(?,?,?,?) ON CONFLICT(chat_id,message_thread_id) DO UPDATE SET
                   codex_thread_id=excluded.codex_thread_id,last_used_at=excluded.last_used_at""",
                (chat_id, message_thread_id, thread_id, iso()),
            )

    def update_message_context(
        self, message_record_id: int, *, thread_id: str | None, turn_id: str | None
    ) -> None:
        self.connection.execute(
            "UPDATE messages SET thread_id=?,turn_id=? WHERE id=?",
            (thread_id, turn_id, message_record_id),
        )
        self.connection.commit()

    def update_outbound_message(self, chat_id: int, message_id: int, text: str) -> None:
        """Mantém no SQLite o conteúdo atual de uma mensagem editada pelo gateway."""
        self.connection.execute(
            """UPDATE messages SET text=?
               WHERE id=(SELECT id FROM messages
                         WHERE chat_id=? AND message_id=? AND direction='out'
                         ORDER BY id DESC LIMIT 1)""",
            (text, chat_id, message_id),
        )
        self.connection.commit()

    def record_attachment(
        self,
        update_id: int,
        file_id: str,
        name: str | None,
        path: Path,
        mime: str | None,
        size: int,
        digest: str,
        *,
        file_unique_id: str | None = None,
        detected_mime: str | None = None,
        logical_type: str | None = None,
        origin: str = "current",
        message_record_id: int | None = None,
    ) -> int:
        cursor = self.connection.execute(
            """INSERT INTO attachments
               (update_id,telegram_file_id,original_name,local_path,mime_type,size_bytes,
                sha256,created_at,file_unique_id,detected_mime,logical_type,origin,message_record_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                update_id, file_id, name, str(path), mime, size, digest, iso(),
                file_unique_id, detected_mime, logical_type, origin, message_record_id,
            ),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def record_artifact(
        self,
        job_id: int,
        *,
        direction: str,
        local_path: Path,
        relative_path: str | None,
        requested_kind: str | None,
        effective_kind: str | None,
        caption: str | None,
        mime_type: str | None,
        size_bytes: int,
        sha256: str,
        message_record_id: int | None = None,
    ) -> int:
        now = iso()
        cursor = self.connection.execute(
            """INSERT INTO artifacts
               (job_id,message_record_id,direction,local_path,relative_path,
                requested_kind,effective_kind,caption,mime_type,size_bytes,sha256,
                upload_state,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,'prepared',?,?)""",
            (
                job_id, message_record_id, direction, str(local_path), relative_path,
                requested_kind, effective_kind, caption, mime_type, size_bytes, sha256,
                now, now,
            ),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def mark_artifact(
        self,
        artifact_id: int,
        state: str,
        *,
        telegram_message_id: int | None = None,
        telegram_file_id: str | None = None,
    ) -> None:
        if state not in {"prepared", "uploading", "sent", "failed", "unknown"}:
            raise StateError("Estado de artefato inválido.")
        self.connection.execute(
            """UPDATE artifacts SET upload_state=?,telegram_message_id=COALESCE(?,telegram_message_id),
               telegram_file_id=COALESCE(?,telegram_file_id),updated_at=? WHERE id=?""",
            (state, telegram_message_id, telegram_file_id, iso(), artifact_id),
        )
        self.connection.commit()

    def statistics(self) -> dict[str, Any]:
        owner = self.owner()
        return {
            "database": str(self.path),
            "owner_configured": owner is not None,
            "owner": ({"user_id": owner.user_id, "chat_id": owner.chat_id, "display_name": owner.display_name, "username": owner.username, "paired_at": owner.paired_at} if owner else None),
            "jobs": self.job_summary(),
        }
