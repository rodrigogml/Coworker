#!/usr/bin/env python3
"""Gerencia bancos SQLite operacionais privados da instância Coworker.

O comando aceita somente nomes e operações estruturadas. SQL, caminhos de arquivo,
extensões e valores de schema arbitrários nunca entram pela interface pública.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import sys
import time
import tomllib
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_DIRECTORY_NAME = "instance_db"
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,47}$")
IDENTIFIER_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
ALLOWED_TYPES = {"text", "integer", "decimal", "boolean", "date", "datetime", "json"}
META_TABLE = "instance_db_meta"
MAX_VALUE_LENGTH = 1_000_000
SECRET_NAME_PATTERN = re.compile(r"(?:password|passwd|token|secret|api[_-]?key|private[_-]?key)", re.I)


class InstanceDbError(Exception):
    """Erro controlado da ferramenta de bancos operacionais."""


class JsonArgumentParser(argparse.ArgumentParser):
    """Converte erros de sintaxe em resposta JSON consumível pelo agente."""

    def error(self, message: str) -> None:
        json_print({"ok": False, "error": message, "error_type": "ArgumentError"})
        raise SystemExit(2)


def now_utc() -> str:
    """Retorna timestamp UTC estável para auditoria local."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def json_print(value: Mapping[str, Any]) -> None:
    """Imprime somente JSON UTF-8."""
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def valid_name(value: str, label: str) -> str:
    """Valida nomes públicos que serão convertidos em recursos locais."""
    if not isinstance(value, str) or not NAME_PATTERN.fullmatch(value):
        raise InstanceDbError(
            f"'{label}' deve usar letras minúsculas, números, '_' ou '-', começando por letra."
        )
    return value


def valid_identifier(value: str, label: str) -> str:
    """Valida identificadores SQL controlados pela ferramenta."""
    if not isinstance(value, str) or not IDENTIFIER_PATTERN.fullmatch(value):
        raise InstanceDbError(f"'{label}' não é um identificador permitido.")
    if SECRET_NAME_PATTERN.search(value):
        raise InstanceDbError(f"'{label}' não pode representar um segredo.")
    if value.startswith("sqlite_"):
        raise InstanceDbError(f"'{label}' usa um prefixo reservado pelo SQLite.")
    return value


def quote_identifier(value: str) -> str:
    """Cita um identificador já validado; nunca recebe entrada não validada."""
    return '"' + valid_identifier(value, "identificador").replace('"', '""') + '"'


def instance_root(project_root: Path = PROJECT_ROOT) -> Path:
    """Resolve e cria somente a raiz privada dos bancos da instância."""
    data_root = (project_root / "data").resolve()
    root = (data_root / DB_DIRECTORY_NAME).resolve()
    if root.parent != data_root:
        raise InstanceDbError("A raiz dos bancos não pertence a data/.")
    root.mkdir(parents=True, exist_ok=True)
    return root


def database_path(name: str, *, project_root: Path = PROJECT_ROOT) -> Path:
    """Converte nome lógico em caminho fixo dentro de data/instance_db/."""
    name = valid_name(name, "database")
    root = instance_root(project_root)
    path = (root / f"{name}.sqlite3").resolve()
    if path.parent != root or path.suffix != ".sqlite3":
        raise InstanceDbError("O banco deve permanecer dentro de data/instance_db/.")
    if path.exists() and path.is_symlink():
        raise InstanceDbError("O banco não pode ser um link simbólico.")
    return path


def instance_id(project_root: Path = PROJECT_ROOT) -> str:
    """Obtém o identificador da instância sem exigir configuração para testes."""
    identity = project_root / "data" / "config" / "identity.toml"
    if identity.is_file():
        try:
            values = tomllib.loads(identity.read_text(encoding="utf-8"))
            value = values.get("identity", {}).get("instance_id")
            if isinstance(value, str) and value.strip():
                return value.strip()
        except (OSError, tomllib.TOMLDecodeError):
            raise InstanceDbError("data/config/identity.toml está inválido.")
    return project_root.name.lower()


def configure_connection(connection: sqlite3.Connection) -> None:
    """Aplica as proteções de conexão comuns a todos os bancos privados."""
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.execute("PRAGMA secure_delete = ON")
    try:
        connection.execute("PRAGMA trusted_schema = OFF")
    except sqlite3.DatabaseError:
        pass


def connect(path: Path, *, create: bool = False) -> sqlite3.Connection:
    """Abre um banco existente ou recém-criado, sem aceitar URI SQLite."""
    if not path.is_file() and not create:
        raise InstanceDbError("Banco não encontrado. Execute database create primeiro.")
    connection = sqlite3.connect(path, timeout=10, uri=False)
    configure_connection(connection)
    return connection


def metadata(connection: sqlite3.Connection) -> dict[str, str]:
    """Lê a identidade registrada do banco."""
    try:
        rows = connection.execute(
            f"SELECT key, value FROM {quote_identifier(META_TABLE)}"
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        raise InstanceDbError("O arquivo não é um banco operacional reconhecido.") from exc
    return {row["key"]: row["value"] for row in rows}


def require_owned(connection: sqlite3.Connection, project_root: Path) -> dict[str, str]:
    """Impede que uma instância opere o banco registrado por outra."""
    values = metadata(connection)
    owner = values.get("owner_instance_id")
    if owner != instance_id(project_root):
        raise InstanceDbError("O banco pertence a outra instância.")
    return values


def create_database(name: str, *, purpose: str | None, project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    """Cria um banco novo e registra seu proprietário."""
    path = database_path(name, project_root=project_root)
    if path.exists():
        raise InstanceDbError("O banco já existe.")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{time.time_ns()}.tmp")
    try:
        connection = connect(temporary, create=True)
        try:
            with connection:
                connection.execute(
                    f"CREATE TABLE {quote_identifier(META_TABLE)} (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                values = {
                    "database_name": name,
                    "owner_instance_id": instance_id(project_root),
                    "created_at": now_utc(),
                    "purpose": (purpose or "").strip()[:500],
                    "schema_version": "1",
                }
                connection.executemany(
                    f"INSERT INTO {quote_identifier(META_TABLE)} (key, value) VALUES (?, ?)",
                    values.items(),
                )
        finally:
            connection.close()
        temporary.replace(path)
    finally:
        for sidecar in (temporary, Path(f"{temporary}-wal"), Path(f"{temporary}-shm")):
            if sidecar.exists():
                sidecar.unlink()
    return {"ok": True, "created": True, "database": name, "path": str(path)}


def list_databases(*, project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    """Lista somente bancos operacionais válidos da instância."""
    root = instance_root(project_root)
    databases = []
    for path in sorted(root.glob("*.sqlite3")):
        try:
            with connect(path) as connection:
                values = metadata(connection)
            databases.append({"database": path.stem, **values})
        except (InstanceDbError, sqlite3.DatabaseError):
            databases.append({"database": path.stem, "status": "unrecognized"})
    return {"ok": True, "databases": databases}


def open_owned(name: str, *, project_root: Path = PROJECT_ROOT) -> tuple[sqlite3.Connection, Path]:
    """Abre e valida um banco pertencente à instância atual."""
    path = database_path(name, project_root=project_root)
    connection = connect(path)
    try:
        require_owned(connection, project_root)
    except Exception:
        connection.close()
        raise
    return connection, path


def parse_column(spec: str) -> tuple[str, str, set[str]]:
    """Converte COLUNA:TIPO[:FLAG...] em definição validada."""
    parts = spec.split(":")
    if len(parts) < 2:
        raise InstanceDbError("Coluna deve usar NOME:TIPO[:primary|required|unique].")
    name = valid_identifier(parts[0], "column")
    type_name = parts[1].lower()
    if type_name not in ALLOWED_TYPES:
        raise InstanceDbError(f"Tipo de coluna inválido: '{type_name}'.")
    flags = set(parts[2:])
    if not flags <= {"primary", "required", "unique"}:
        raise InstanceDbError("Flag de coluna inválida.")
    if "primary" in flags and type_name != "integer":
        raise InstanceDbError("A chave primária deve ser integer.")
    return name, type_name, flags


def table_columns(connection: sqlite3.Connection, table: str) -> dict[str, dict[str, Any]]:
    """Obtém schema de tabela sem aceitar nome arbitrário."""
    table = valid_identifier(table, "table")
    rows = connection.execute(f"PRAGMA table_info({quote_identifier(table)})").fetchall()
    if not rows or table == META_TABLE:
        raise InstanceDbError("Tabela não encontrada ou reservada.")
    return {
        row["name"]: {
            "type": row["type"].lower(),
            "required": bool(row["notnull"]),
            "primary": bool(row["pk"]),
            "default": row["dflt_value"],
        }
        for row in rows
    }


def create_table(database: str, table: str, columns: Iterable[str], *, project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    """Cria tabela com colunas e restrições declarativas."""
    table = valid_identifier(table, "table")
    definitions = [parse_column(value) for value in columns]
    if not definitions:
        raise InstanceDbError("Informe ao menos uma coluna.")
    names = [item[0] for item in definitions]
    if len(names) != len(set(names)):
        raise InstanceDbError("Não repita nomes de colunas.")
    connection, _ = open_owned(database, project_root=project_root)
    try:
        fragments = []
        for name, type_name, flags in definitions:
            fragment = f"{quote_identifier(name)} {type_name.upper()}"
            if "primary" in flags:
                fragment += " PRIMARY KEY"
            if "required" in flags:
                fragment += " NOT NULL"
            if "unique" in flags:
                fragment += " UNIQUE"
            fragments.append(fragment)
        with connection:
            connection.execute(
                f"CREATE TABLE {quote_identifier(table)} ({', '.join(fragments)})"
            )
    finally:
        connection.close()
    return {"ok": True, "database": database, "table": table, "created": True}


def add_column(database: str, table: str, spec: str, *, project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    """Adiciona uma coluna declarativa sem aceitar alteração SQL livre."""
    name, type_name, flags = parse_column(spec)
    if flags:
        raise InstanceDbError("Uma coluna adicionada só pode declarar nome e tipo.")
    connection, _ = open_owned(database, project_root=project_root)
    try:
        columns = table_columns(connection, table)
        if name in columns:
            raise InstanceDbError(f"A coluna '{name}' já existe.")
        with connection:
            connection.execute(
                f"ALTER TABLE {quote_identifier(table)} ADD COLUMN {quote_identifier(name)} {type_name.upper()}"
            )
    finally:
        connection.close()
    return {"ok": True, "database": database, "table": table, "column": name, "created": True}


def describe_table(database: str, table: str, *, project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    """Retorna o schema de uma tabela própria em formato estruturado."""
    connection, _ = open_owned(database, project_root=project_root)
    try:
        columns = table_columns(connection, table)
        return {"ok": True, "database": database, "table": table, "columns": columns}
    finally:
        connection.close()


def parse_assignment(spec: str, label: str) -> tuple[str, str]:
    """Converte COLUNA=VALOR preservando '=' dentro do valor."""
    name, separator, value = spec.partition("=")
    if not separator:
        raise InstanceDbError(f"'{label}' deve usar COLUNA=VALOR.")
    return valid_identifier(name.strip(), label), value


def convert_value(value: str, definition: Mapping[str, Any], label: str) -> Any:
    """Converte valores segundo o tipo do schema, sem executar conteúdo."""
    type_name = definition["type"]
    if SECRET_NAME_PATTERN.search(label):
        raise InstanceDbError(f"'{label}' não pode receber segredos.")
    if len(value) > MAX_VALUE_LENGTH:
        raise InstanceDbError(f"'{label}' excede o limite de {MAX_VALUE_LENGTH} caracteres.")
    if type_name == "text" or type_name in {"date", "datetime"}:
        return value
    if type_name == "integer":
        try:
            return int(value)
        except ValueError as exc:
            raise InstanceDbError(f"'{label}' deve ser integer.") from exc
    if type_name == "decimal":
        try:
            number = Decimal(value)
        except InvalidOperation as exc:
            raise InstanceDbError(f"'{label}' deve ser decimal.") from exc
        if not number.is_finite():
            raise InstanceDbError(f"'{label}' deve ser decimal finito.")
        return format(number, "f")
    if type_name == "boolean":
        if value.lower() not in {"true", "false", "1", "0"}:
            raise InstanceDbError(f"'{label}' deve ser true ou false.")
        return 1 if value.lower() in {"true", "1"} else 0
    if type_name == "json":
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise InstanceDbError(f"'{label}' deve conter JSON válido.") from exc
        return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
    raise InstanceDbError(f"Tipo não suportado: '{type_name}'.")


def row_values(connection: sqlite3.Connection, table: str, assignments: Iterable[str]) -> dict[str, Any]:
    """Valida e converte todos os campos de uma operação de linha."""
    columns = table_columns(connection, table)
    result: dict[str, Any] = {}
    for raw in assignments:
        name, value = parse_assignment(raw, "value")
        if name not in columns:
            raise InstanceDbError(f"Coluna desconhecida: '{name}'.")
        if name in result:
            raise InstanceDbError(f"Coluna repetida: '{name}'.")
        result[name] = convert_value(value, columns[name], name)
    if not result:
        raise InstanceDbError("Informe ao menos um valor.")
    return result


def where_clause(connection: sqlite3.Connection, table: str, assignments: Iterable[str]) -> tuple[str, list[Any]]:
    """Cria filtros parametrizados somente para colunas existentes."""
    columns = table_columns(connection, table)
    parts, values = [], []
    for raw in assignments:
        name, value = parse_assignment(raw, "where")
        if name not in columns:
            raise InstanceDbError(f"Coluna desconhecida: '{name}'.")
        parts.append(f"{quote_identifier(name)} = ?")
        values.append(convert_value(value, columns[name], name))
    return (" AND ".join(parts) if parts else "1 = 1"), values


def serialize_row(row: sqlite3.Row, columns: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Converte linha SQLite em objeto JSON com tipos públicos estáveis."""
    result: dict[str, Any] = {}
    for key in row.keys():
        value = row[key]
        type_name = columns.get(key, {}).get("type")
        if type_name == "decimal" and value is not None:
            value = format(Decimal(str(value)), "f")
        elif type_name == "boolean" and value is not None:
            value = bool(value)
        elif type_name == "json" and isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                pass
        result[key] = value
    return result


def mutate_row(database: str, table: str, assignments: list[str], *, upsert_key: str | None, project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    """Insere ou atualiza uma linha com transação parametrizada."""
    table = valid_identifier(table, "table")
    connection, _ = open_owned(database, project_root=project_root)
    try:
        values = row_values(connection, table, assignments)
        if upsert_key is None:
            columns = ", ".join(quote_identifier(key) for key in values)
            placeholders = ", ".join("?" for _ in values)
            with connection:
                cursor = connection.execute(
                    f"INSERT INTO {quote_identifier(table)} ({columns}) VALUES ({placeholders})",
                    list(values.values()),
                )
            return {"ok": True, "operation": "insert", "database": database, "table": table, "row_id": cursor.lastrowid}
        key = valid_identifier(upsert_key, "key")
        if key not in values:
            raise InstanceDbError("A chave do upsert deve estar entre os valores informados.")
        columns = table_columns(connection, table)
        if not columns[key].get("primary"):
            unique = connection.execute(
                'SELECT 1 FROM pragma_index_list(?) WHERE "unique" = 1 AND origin = \'u\' LIMIT 1',
                (table,),
            ).fetchone()
            if unique is None:
                raise InstanceDbError("A chave do upsert deve ser primary ou unique.")
        key_value = values[key]
        existing = connection.execute(
            f"SELECT 1 FROM {quote_identifier(table)} WHERE {quote_identifier(key)} = ? LIMIT 1",
            (key_value,),
        ).fetchone()
        if existing:
            updates = [name for name in values if name != key]
            if updates:
                set_clause = ", ".join(f"{quote_identifier(name)} = ?" for name in updates)
                with connection:
                    connection.execute(
                        f"UPDATE {quote_identifier(table)} SET {set_clause} WHERE {quote_identifier(key)} = ?",
                        [values[name] for name in updates] + [key_value],
                    )
            operation = "update"
        else:
            cols = ", ".join(quote_identifier(name) for name in values)
            placeholders = ", ".join("?" for _ in values)
            with connection:
                connection.execute(
                    f"INSERT INTO {quote_identifier(table)} ({cols}) VALUES ({placeholders})",
                    list(values.values()),
                )
            operation = "insert"
        return {"ok": True, "operation": operation, "database": database, "table": table, "key": {key: key_value}}
    finally:
        connection.close()


def list_rows(database: str, table: str, filters: list[str], limit: int, *, project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    """Lista linhas paginadas com filtros parametrizados."""
    if not 1 <= limit <= 500:
        raise InstanceDbError("limit deve estar entre 1 e 500.")
    table = valid_identifier(table, "table")
    connection, _ = open_owned(database, project_root=project_root)
    try:
        clause, values = where_clause(connection, table, filters)
        columns = table_columns(connection, table)
        rows = connection.execute(
            f"SELECT * FROM {quote_identifier(table)} WHERE {clause} LIMIT ?",
            values + [limit],
        ).fetchall()
        return {"ok": True, "database": database, "table": table, "count": len(rows), "rows": [serialize_row(row, columns) for row in rows]}
    finally:
        connection.close()


def drop_object(database: str, table: str, *, project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    """Remove uma tabela pertencente ao banco operacional da instância."""
    table = valid_identifier(table, "table")
    connection, _ = open_owned(database, project_root=project_root)
    try:
        table_columns(connection, table)
        with connection:
            connection.execute(f"DROP TABLE {quote_identifier(table)}")
    finally:
        connection.close()
    return {"ok": True, "database": database, "table": table, "deleted": True}


def delete_database(name: str, *, confirm: bool, project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    """Move banco próprio para lixeira privada, sem destruição imediata."""
    if not confirm:
        raise InstanceDbError("Exclusão do banco exige --confirm.")
    path = database_path(name, project_root=project_root)
    connection, _ = open_owned(name, project_root=project_root)
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()
    trash = instance_root(project_root) / ".trash"
    trash.mkdir(exist_ok=True)
    destination = trash / f"{name}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{time.time_ns()}.sqlite3"
    if destination.exists():
        raise InstanceDbError("O destino da lixeira já existe.")
    path.replace(destination)
    for sidecar in (Path(f"{path}-wal"), Path(f"{path}-shm")):
        if sidecar.exists():
            sidecar.unlink()
    return {"ok": True, "database": name, "deleted": True, "recoverable_path": str(destination)}


def backup_database(name: str, *, project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    """Cria backup consistente em subdiretório privado e sem sobrescrita."""
    connection, _ = open_owned(name, project_root=project_root)
    root = instance_root(project_root) / "backups" / name
    root.mkdir(parents=True, exist_ok=True)
    destination = root / f"{name}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.sqlite3"
    if destination.exists():
        raise InstanceDbError("O backup já existe; tente novamente em outro segundo.")
    temporary = destination.with_name(f".{destination.name}.{time.time_ns()}.tmp")
    try:
        target = sqlite3.connect(temporary)
        try:
            connection.backup(target)
        finally:
            target.close()
        temporary.replace(destination)
    finally:
        connection.close()
        if temporary.exists():
            temporary.unlink()
    return {"ok": True, "database": name, "backup": str(destination)}


def build_parser() -> argparse.ArgumentParser:
    """Constrói a interface pública estruturada."""
    parser = JsonArgumentParser(description="Bancos SQLite operacionais privados da instância.")
    resources = parser.add_subparsers(
        dest="resource", required=True, parser_class=JsonArgumentParser
    )
    database = resources.add_parser("database")
    database_commands = database.add_subparsers(
        dest="operation", required=True, parser_class=JsonArgumentParser
    )
    create = database_commands.add_parser("create")
    create.add_argument("name")
    create.add_argument("--purpose")
    database_commands.add_parser("list")
    delete = database_commands.add_parser("delete")
    delete.add_argument("name")
    delete.add_argument("--confirm", action="store_true")
    backup = database_commands.add_parser("backup")
    backup.add_argument("name")

    table = resources.add_parser("table")
    table_commands = table.add_subparsers(
        dest="operation", required=True, parser_class=JsonArgumentParser
    )
    table_create = table_commands.add_parser("create")
    table_create.add_argument("database")
    table_create.add_argument("table")
    table_create.add_argument("--column", action="append", required=True)
    table_add = table_commands.add_parser("add-column")
    table_add.add_argument("database")
    table_add.add_argument("table")
    table_add.add_argument("--column", required=True)
    table_list = table_commands.add_parser("list")
    table_list.add_argument("database")
    table_describe = table_commands.add_parser("describe")
    table_describe.add_argument("database")
    table_describe.add_argument("table")
    table_drop = table_commands.add_parser("drop")
    table_drop.add_argument("database")
    table_drop.add_argument("table")

    row = resources.add_parser("row")
    row_commands = row.add_subparsers(
        dest="operation", required=True, parser_class=JsonArgumentParser
    )
    for operation in ("insert", "upsert"):
        command = row_commands.add_parser(operation)
        command.add_argument("database")
        command.add_argument("table")
        command.add_argument("--value", action="append", required=True)
        if operation == "upsert":
            command.add_argument("--key", required=True)
    list_command = row_commands.add_parser("list")
    list_command.add_argument("database")
    list_command.add_argument("table")
    list_command.add_argument("--where", action="append", default=[])
    list_command.add_argument("--limit", type=int, default=50)
    show_command = row_commands.add_parser("show")
    show_command.add_argument("database")
    show_command.add_argument("table")
    show_command.add_argument("--where", action="append", required=True)
    delete_row = row_commands.add_parser("delete")
    delete_row.add_argument("database")
    delete_row.add_argument("table")
    delete_row.add_argument("--where", action="append", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Executa a operação e retorna JSON com código de erro apropriado."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.resource == "database" and args.operation == "create":
            result = create_database(args.name, purpose=args.purpose)
        elif args.resource == "database" and args.operation == "list":
            result = list_databases()
        elif args.resource == "database" and args.operation == "delete":
            result = delete_database(args.name, confirm=args.confirm)
        elif args.resource == "database" and args.operation == "backup":
            result = backup_database(args.name)
        elif args.resource == "table" and args.operation == "create":
            result = create_table(args.database, args.table, args.column)
        elif args.resource == "table" and args.operation == "add-column":
            result = add_column(args.database, args.table, args.column)
        elif args.resource == "table" and args.operation == "list":
            connection, _ = open_owned(args.database)
            try:
                rows = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name != ? ORDER BY name",
                    (META_TABLE,),
                ).fetchall()
                result = {"ok": True, "database": args.database, "tables": [row["name"] for row in rows]}
            finally:
                connection.close()
        elif args.resource == "table" and args.operation == "describe":
            result = describe_table(args.database, args.table)
        elif args.resource == "table" and args.operation == "drop":
            result = drop_object(args.database, args.table)
        elif args.resource == "row" and args.operation in {"insert", "upsert"}:
            result = mutate_row(args.database, args.table, args.value, upsert_key=getattr(args, "key", None))
        elif args.resource == "row" and args.operation == "list":
            result = list_rows(args.database, args.table, args.where, args.limit)
        elif args.resource == "row" and args.operation == "show":
            result = list_rows(args.database, args.table, args.where, 1)
            if result["count"] == 0:
                raise InstanceDbError("Registro não encontrado.")
        elif args.resource == "row" and args.operation == "delete":
            connection, _ = open_owned(args.database)
            try:
                clause, values = where_clause(connection, args.table, args.where)
                with connection:
                    cursor = connection.execute(
                        f"DELETE FROM {quote_identifier(args.table)} WHERE {clause}", values
                    )
                result = {"ok": True, "database": args.database, "table": args.table, "deleted_count": cursor.rowcount}
            finally:
                connection.close()
        else:
            raise InstanceDbError("Operação desconhecida.")
        json_print(result)
        return 0
    except (InstanceDbError, OSError, sqlite3.Error) as exc:
        json_print({"ok": False, "error": str(exc), "error_type": type(exc).__name__})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
