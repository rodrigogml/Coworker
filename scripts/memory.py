#!/usr/bin/env python3
"""Interface de linha de comando para a memória SQLite da BOTina."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import sys
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE = PROJECT_ROOT / "data" / "memory.sqlite3"
MIGRATIONS_DIR = PROJECT_ROOT / "migrations"

KINDS = ("fact", "preference", "decision", "inference", "reference", "routine")
SENSITIVITIES = ("normal", "personal", "confidential")
STATUSES = ("active", "superseded", "archived")

SECRET_KEY_NAMES = {
    "api_key",
    "apikey",
    "access_token",
    "auth_token",
    "client_secret",
    "password",
    "passwd",
    "private_key",
    "refresh_token",
    "secret",
    "senha",
    "token",
}
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.IGNORECASE),
    re.compile(
        r"\b(?:password|passwd|senha|secret|token|api[_-]?key|client[_-]?secret)"
        r"\s*[:=]\s*\S+",
        re.IGNORECASE,
    ),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE),
)


class MemoryError(Exception):
    """Erro esperado e seguro para apresentação ao chamador."""


def utc_now() -> str:
    """Retorna o instante atual em UTC no formato ISO 8601."""
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def print_json(payload: Any, *, stream: Any = sys.stdout) -> None:
    """Imprime uma resposta JSON consistente para consumo por agentes."""
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), file=stream)


def normalize_required(value: str, field: str) -> str:
    """Valida e normaliza um texto obrigatório."""
    normalized = value.strip()
    if not normalized:
        raise MemoryError(f"O campo '{field}' não pode ficar vazio.")
    return normalized


def validate_optional_date(value: str | None, field: str) -> str | None:
    """Valida uma data ou instante ISO 8601, preservando o valor informado."""
    if value is None:
        return None
    normalized = normalize_required(value, field)
    candidate = normalized[:-1] + "+00:00" if normalized.endswith("Z") else normalized
    try:
        datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise MemoryError(f"O campo '{field}' deve usar o formato ISO 8601.") from exc
    return normalized


def parse_metadata(raw_value: str | None) -> dict[str, Any]:
    """Converte metadados JSON e exige um objeto na raiz."""
    if raw_value is None:
        return {}
    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise MemoryError("O campo 'metadata' deve conter JSON válido.") from exc
    if not isinstance(parsed, dict):
        raise MemoryError("O campo 'metadata' deve ser um objeto JSON.")
    return parsed


def metadata_contains_secret_key(value: Any) -> bool:
    """Detecta nomes de campos normalmente usados para armazenar segredos."""
    if isinstance(value, dict):
        for key, child in value.items():
            normalized_key = re.sub(r"[^a-z0-9]+", "_", str(key).lower()).strip("_")
            if normalized_key in SECRET_KEY_NAMES:
                return True
            if metadata_contains_secret_key(child):
                return True
    elif isinstance(value, list):
        return any(metadata_contains_secret_key(child) for child in value)
    return False


def reject_probable_secrets(content: str, metadata: dict[str, Any]) -> None:
    """Impede gravações que aparentem conter credenciais ou chaves privadas."""
    if metadata_contains_secret_key(metadata) or any(
        pattern.search(content) for pattern in SECRET_PATTERNS
    ):
        raise MemoryError(
            "A memória aparenta conter um segredo. Guarde o valor em um cofre de "
            "credenciais e registre somente sua referência com '--credential-ref'."
        )


def normalize_tags(tags: list[str] | None) -> list[str]:
    """Remove espaços, vazios e duplicidades da lista de tags."""
    normalized = {tag.strip().lower() for tag in tags or [] if tag.strip()}
    return sorted(normalized)


def database_path(raw_path: str) -> Path:
    """Resolve o caminho do banco sem exigir que ele já exista."""
    return Path(raw_path).expanduser().resolve()


def connect(path: Path, *, create: bool = False) -> sqlite3.Connection:
    """Abre uma conexão SQLite configurada para o uso local da BOTina."""
    if not path.exists() and not create:
        raise MemoryError(
            f"Banco não encontrado em '{path}'. Execute primeiro o comando 'init'."
        )
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


@contextmanager
def connection_scope(
    path: Path, *, create: bool = False
) -> Iterator[sqlite3.Connection]:
    """Confirma ou reverte a transação e sempre fecha a conexão."""
    connection = connect(path, create=create)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def sql_literal(value: str) -> str:
    """Escapa um literal confiável usado no controle interno de migrations."""
    return "'" + value.replace("'", "''") + "'"


def apply_migrations(connection: sqlite3.Connection) -> list[str]:
    """Aplica migrations ainda não executadas e verifica alterações indevidas."""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            name TEXT PRIMARY KEY,
            checksum TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    connection.commit()

    applied = {
        row["name"]: row["checksum"]
        for row in connection.execute(
            "SELECT name, checksum FROM schema_migrations ORDER BY name"
        )
    }
    executed: list[str] = []

    migration_files = sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"))
    if not migration_files:
        raise MemoryError(f"Nenhuma migration foi encontrada em '{MIGRATIONS_DIR}'.")

    for migration in migration_files:
        script = migration.read_text(encoding="utf-8")
        checksum = hashlib.sha256(script.encode("utf-8")).hexdigest()
        previous_checksum = applied.get(migration.name)
        if previous_checksum is not None:
            if previous_checksum != checksum:
                raise MemoryError(
                    f"A migration já aplicada '{migration.name}' foi alterada."
                )
            continue

        applied_at = utc_now()
        transaction = (
            "BEGIN IMMEDIATE;\n"
            f"{script}\n"
            "INSERT INTO schema_migrations(name, checksum, applied_at) VALUES ("
            f"{sql_literal(migration.name)}, "
            f"{sql_literal(checksum)}, "
            f"{sql_literal(applied_at)}"
            ");\n"
            "COMMIT;"
        )
        try:
            connection.executescript(transaction)
        except sqlite3.Error:
            connection.rollback()
            raise
        executed.append(migration.name)

    return executed


def ensure_initialized(connection: sqlite3.Connection) -> None:
    """Confirma a presença do schema antes de executar uma operação."""
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'memories'"
    ).fetchone()
    if row is None:
        raise MemoryError("Banco ainda não inicializado. Execute o comando 'init'.")


def row_to_memory(connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    """Transforma uma linha e suas tags em um objeto serializável."""
    memory = dict(row)
    memory["metadata"] = json.loads(memory.pop("metadata_json"))
    memory["tags"] = [
        tag_row["tag"]
        for tag_row in connection.execute(
            "SELECT tag FROM memory_tags WHERE memory_id = ? ORDER BY tag",
            (memory["id"],),
        )
    ]
    return memory


def fetch_memory(connection: sqlite3.Connection, memory_id: str) -> sqlite3.Row:
    """Obtém uma memória pelo identificador ou informa que ela não existe."""
    row = connection.execute(
        "SELECT * FROM memories WHERE id = ?", (memory_id,)
    ).fetchone()
    if row is None:
        raise MemoryError(f"Memória '{memory_id}' não encontrada.")
    return row


def insert_memory(
    connection: sqlite3.Connection,
    *,
    kind: str,
    subject: str,
    content: str,
    source: str,
    scope: str,
    sensitivity: str,
    confidence: float,
    credential_ref: str | None,
    metadata: dict[str, Any],
    tags: list[str],
    expires_at: str | None,
    supersedes_id: str | None = None,
) -> str:
    """Insere uma memória já validada e retorna seu identificador."""
    memory_id = f"mem_{uuid.uuid4().hex}"
    timestamp = utc_now()
    connection.execute(
        """
        INSERT INTO memories (
            id, kind, subject, content, source, scope, sensitivity, confidence,
            status, credential_ref, metadata_json, created_at, updated_at,
            expires_at, supersedes_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?)
        """,
        (
            memory_id,
            kind,
            subject,
            content,
            source,
            scope,
            sensitivity,
            confidence,
            credential_ref,
            json.dumps(metadata, ensure_ascii=False, sort_keys=True),
            timestamp,
            timestamp,
            expires_at,
            supersedes_id,
        ),
    )
    connection.executemany(
        "INSERT INTO memory_tags(memory_id, tag) VALUES (?, ?)",
        [(memory_id, tag) for tag in tags],
    )
    return memory_id


def command_init(args: argparse.Namespace) -> dict[str, Any]:
    """Inicializa o banco e aplica migrations."""
    path = database_path(args.database)
    with connection_scope(path, create=True) as connection:
        migrations = apply_migrations(connection)
    return {
        "ok": True,
        "database": str(path),
        "applied_migrations": migrations,
    }


def command_remember(args: argparse.Namespace) -> dict[str, Any]:
    """Registra uma nova memória."""
    subject = normalize_required(args.subject, "subject")
    content = normalize_required(args.content, "content")
    source = normalize_required(args.source, "source")
    scope = normalize_required(args.scope, "scope")
    credential_ref = (
        normalize_required(args.credential_ref, "credential-ref")
        if args.credential_ref
        else None
    )
    metadata = parse_metadata(args.metadata)
    reject_probable_secrets(content, metadata)
    expires_at = validate_optional_date(args.expires_at, "expires-at")
    tags = normalize_tags(args.tag)

    path = database_path(args.database)
    with connection_scope(path) as connection:
        ensure_initialized(connection)
        memory_id = insert_memory(
            connection,
            kind=args.kind,
            subject=subject,
            content=content,
            source=source,
            scope=scope,
            sensitivity=args.sensitivity,
            confidence=args.confidence,
            credential_ref=credential_ref,
            metadata=metadata,
            tags=tags,
            expires_at=expires_at,
        )
        row = fetch_memory(connection, memory_id)
        result = row_to_memory(connection, row)
    return {"ok": True, "memory": result}


def build_filters(
    args: argparse.Namespace, *, include_query: bool
) -> tuple[str, list[Any]]:
    """Monta filtros parametrizados compartilhados por listagem e pesquisa."""
    clauses: list[str] = []
    parameters: list[Any] = []

    if not args.include_inactive:
        clauses.append("m.status = 'active'")
    if args.kind:
        clauses.append("m.kind = ?")
        parameters.append(args.kind)
    if args.scope:
        clauses.append("m.scope = ?")
        parameters.append(args.scope)
    if args.tag:
        clauses.append(
            "EXISTS (SELECT 1 FROM memory_tags mt "
            "WHERE mt.memory_id = m.id AND mt.tag = ? COLLATE NOCASE)"
        )
        parameters.append(args.tag.strip())
    if include_query:
        token = f"%{args.query.strip()}%"
        clauses.append(
            "(m.subject LIKE ? COLLATE NOCASE OR "
            "m.content LIKE ? COLLATE NOCASE OR "
            "m.source LIKE ? COLLATE NOCASE)"
        )
        parameters.extend((token, token, token))

    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    return where, parameters


def query_memories(
    connection: sqlite3.Connection,
    where: str,
    parameters: list[Any],
    limit: int,
) -> list[dict[str, Any]]:
    """Executa uma consulta de memórias com limite explícito."""
    rows = connection.execute(
        "SELECT m.* FROM memories m"
        f"{where} ORDER BY m.updated_at DESC, m.id LIMIT ?",
        (*parameters, limit),
    ).fetchall()
    return [row_to_memory(connection, row) for row in rows]


def command_search(args: argparse.Namespace) -> dict[str, Any]:
    """Pesquisa memórias por texto e filtros."""
    normalize_required(args.query, "query")
    path = database_path(args.database)
    with connection_scope(path) as connection:
        ensure_initialized(connection)
        where, parameters = build_filters(args, include_query=True)
        memories = query_memories(connection, where, parameters, args.limit)
    return {"ok": True, "count": len(memories), "memories": memories}


def command_list(args: argparse.Namespace) -> dict[str, Any]:
    """Lista memórias usando filtros opcionais."""
    path = database_path(args.database)
    with connection_scope(path) as connection:
        ensure_initialized(connection)
        where, parameters = build_filters(args, include_query=False)
        memories = query_memories(connection, where, parameters, args.limit)
    return {"ok": True, "count": len(memories), "memories": memories}


def command_show(args: argparse.Namespace) -> dict[str, Any]:
    """Mostra uma memória pelo identificador."""
    path = database_path(args.database)
    with connection_scope(path) as connection:
        ensure_initialized(connection)
        memory = row_to_memory(connection, fetch_memory(connection, args.id))
    return {"ok": True, "memory": memory}


def command_supersede(args: argparse.Namespace) -> dict[str, Any]:
    """Substitui uma memória preservando a versão anterior como inativa."""
    path = database_path(args.database)
    with connection_scope(path) as connection:
        ensure_initialized(connection)
        old_row = fetch_memory(connection, args.id)
        old_memory = row_to_memory(connection, old_row)
        if old_memory["status"] != "active":
            raise MemoryError("Somente uma memória ativa pode ser substituída.")

        content = (
            normalize_required(args.content, "content")
            if args.content is not None
            else old_memory["content"]
        )
        subject = (
            normalize_required(args.subject, "subject")
            if args.subject is not None
            else old_memory["subject"]
        )
        scope = (
            normalize_required(args.scope, "scope")
            if args.scope is not None
            else old_memory["scope"]
        )
        credential_ref = (
            normalize_required(args.credential_ref, "credential-ref")
            if args.credential_ref is not None
            else old_memory["credential_ref"]
        )
        metadata = (
            parse_metadata(args.metadata)
            if args.metadata is not None
            else old_memory["metadata"]
        )
        tags = (
            normalize_tags(args.tag)
            if args.tag is not None
            else old_memory["tags"]
        )
        expires_at = (
            validate_optional_date(args.expires_at, "expires-at")
            if args.expires_at is not None
            else old_memory["expires_at"]
        )
        reject_probable_secrets(content, metadata)

        new_id = insert_memory(
            connection,
            kind=args.kind or old_memory["kind"],
            subject=subject,
            content=content,
            source=normalize_required(args.source, "source"),
            scope=scope,
            sensitivity=args.sensitivity or old_memory["sensitivity"],
            confidence=(
                args.confidence
                if args.confidence is not None
                else old_memory["confidence"]
            ),
            credential_ref=credential_ref,
            metadata=metadata,
            tags=tags,
            expires_at=expires_at,
            supersedes_id=args.id,
        )
        connection.execute(
            "UPDATE memories SET status = 'superseded', updated_at = ? WHERE id = ?",
            (utc_now(), args.id),
        )
        new_memory = row_to_memory(connection, fetch_memory(connection, new_id))
    return {
        "ok": True,
        "superseded_id": args.id,
        "memory": new_memory,
    }


def command_forget(args: argparse.Namespace) -> dict[str, Any]:
    """Remove definitivamente uma memória do banco ativo."""
    if not args.confirm:
        raise MemoryError("A exclusão exige a opção '--confirm'.")
    path = database_path(args.database)
    with connection_scope(path) as connection:
        ensure_initialized(connection)
        fetch_memory(connection, args.id)
        connection.execute("DELETE FROM memories WHERE id = ?", (args.id,))
    return {
        "ok": True,
        "forgotten_id": args.id,
        "recoverable_from_active_database": False,
    }


def command_status(args: argparse.Namespace) -> dict[str, Any]:
    """Apresenta a versão do banco e contagens por estado."""
    path = database_path(args.database)
    with connection_scope(path) as connection:
        ensure_initialized(connection)
        counts = {
            row["status"]: row["count"]
            for row in connection.execute(
                "SELECT status, COUNT(*) AS count FROM memories GROUP BY status"
            )
        }
        migrations = [
            dict(row)
            for row in connection.execute(
                "SELECT name, checksum, applied_at "
                "FROM schema_migrations ORDER BY name"
            )
        ]
    return {
        "ok": True,
        "database": str(path),
        "sqlite_version": sqlite3.sqlite_version,
        "counts": {status: counts.get(status, 0) for status in STATUSES},
        "migrations": migrations,
    }


def command_backup(args: argparse.Namespace) -> dict[str, Any]:
    """Cria um backup consistente usando a API de backup do SQLite."""
    source_path = database_path(args.database)
    output_path = Path(args.output).expanduser().resolve()
    if output_path == source_path:
        raise MemoryError("O destino do backup deve ser diferente do banco principal.")
    if output_path.exists() and not args.overwrite:
        raise MemoryError("O backup já existe. Use '--overwrite' para substituí-lo.")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    temporary_path = output_path.with_name(f".{output_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with connection_scope(source_path) as source:
            ensure_initialized(source)
            target = sqlite3.connect(temporary_path)
            try:
                source.backup(target)
            finally:
                target.close()
        if output_path.exists():
            output_path.unlink()
        shutil.move(str(temporary_path), str(output_path))
    finally:
        if temporary_path.exists():
            temporary_path.unlink()

    return {"ok": True, "database": str(source_path), "backup": str(output_path)}


def add_query_filters(parser: argparse.ArgumentParser) -> None:
    """Adiciona filtros compartilhados aos comandos de consulta."""
    parser.add_argument("--kind", choices=KINDS)
    parser.add_argument("--scope")
    parser.add_argument("--tag")
    parser.add_argument("--include-inactive", action="store_true")
    parser.add_argument("--limit", type=int, default=50, choices=range(1, 501))


def build_parser() -> argparse.ArgumentParser:
    """Constrói o parser da interface de linha de comando."""
    parser = argparse.ArgumentParser(
        description="Memória SQLite local da assistente BOTina."
    )
    parser.add_argument(
        "--database",
        default=str(DEFAULT_DATABASE),
        help=f"Caminho do banco SQLite (padrão: {DEFAULT_DATABASE}).",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    init_parser = commands.add_parser("init", help="Inicializa ou atualiza o banco.")
    init_parser.set_defaults(handler=command_init)

    remember_parser = commands.add_parser("remember", help="Registra uma memória.")
    remember_parser.add_argument("--kind", choices=KINDS, required=True)
    remember_parser.add_argument("--subject", required=True)
    remember_parser.add_argument("--content", required=True)
    remember_parser.add_argument("--source", required=True)
    remember_parser.add_argument("--scope", default="global")
    remember_parser.add_argument(
        "--sensitivity", choices=SENSITIVITIES, default="normal"
    )
    remember_parser.add_argument(
        "--confidence", type=float, choices=[value / 10 for value in range(11)], default=1.0
    )
    remember_parser.add_argument("--credential-ref")
    remember_parser.add_argument("--metadata")
    remember_parser.add_argument("--tag", action="append")
    remember_parser.add_argument("--expires-at")
    remember_parser.set_defaults(handler=command_remember)

    search_parser = commands.add_parser("search", help="Pesquisa memórias.")
    search_parser.add_argument("query")
    add_query_filters(search_parser)
    search_parser.set_defaults(handler=command_search)

    list_parser = commands.add_parser("list", help="Lista memórias.")
    add_query_filters(list_parser)
    list_parser.set_defaults(handler=command_list)

    show_parser = commands.add_parser("show", help="Mostra uma memória.")
    show_parser.add_argument("id")
    show_parser.set_defaults(handler=command_show)

    supersede_parser = commands.add_parser(
        "supersede", help="Substitui uma memória por uma versão corrigida."
    )
    supersede_parser.add_argument("id")
    supersede_parser.add_argument("--source", required=True)
    supersede_parser.add_argument("--kind", choices=KINDS)
    supersede_parser.add_argument("--subject")
    supersede_parser.add_argument("--content")
    supersede_parser.add_argument("--scope")
    supersede_parser.add_argument("--sensitivity", choices=SENSITIVITIES)
    supersede_parser.add_argument(
        "--confidence", type=float, choices=[value / 10 for value in range(11)]
    )
    supersede_parser.add_argument("--credential-ref")
    supersede_parser.add_argument("--metadata")
    supersede_parser.add_argument("--tag", action="append")
    supersede_parser.add_argument("--expires-at")
    supersede_parser.set_defaults(handler=command_supersede)

    forget_parser = commands.add_parser("forget", help="Exclui uma memória.")
    forget_parser.add_argument("id")
    forget_parser.add_argument("--confirm", action="store_true")
    forget_parser.set_defaults(handler=command_forget)

    status_parser = commands.add_parser("status", help="Inspeciona o banco.")
    status_parser.set_defaults(handler=command_status)

    backup_parser = commands.add_parser("backup", help="Cria um backup consistente.")
    backup_parser.add_argument("--output", required=True)
    backup_parser.add_argument("--overwrite", action="store_true")
    backup_parser.set_defaults(handler=command_backup)

    return parser


def main() -> int:
    """Executa o comando solicitado e converte falhas em respostas JSON."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args()
    try:
        result = args.handler(args)
    except (MemoryError, sqlite3.Error, OSError) as exc:
        print_json(
            {"ok": False, "error": str(exc), "error_type": type(exc).__name__},
            stream=sys.stderr,
        )
        return 1
    print_json(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
