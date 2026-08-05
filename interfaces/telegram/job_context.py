"""Persistência fechada de entradas intermediárias do trabalho Telegram."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


INPUT_ENVIRONMENT_VARIABLE = "COWORKER_JOB_INPUT"
DERIVED_ENVIRONMENT_VARIABLE = "COWORKER_JOB_DERIVED"
_NAMESPACE_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")


class JobContextError(RuntimeError):
    """Indica contexto ausente, destino inseguro ou colisão de conteúdo."""


@dataclass(frozen=True)
class JobJsonFile:
    """Resultado seguro da persistência, sem devolver o conteúdo gravado."""

    path: Path
    created: bool


def _resolve_job_directory(
    environment_variable: str,
    expected_name: str,
    project_root: Path,
    environment: Mapping[str, str] | None = None,
) -> Path:
    effective_environment = os.environ if environment is None else environment
    raw = str(effective_environment.get(environment_variable, "")).strip()
    if not raw:
        raise JobContextError(f"{environment_variable} não foi definido pelo gateway.")
    try:
        project_data = (project_root / "data").resolve(strict=True)
        directory = Path(raw).expanduser().resolve(strict=True)
        directory.relative_to(project_data)
    except (OSError, ValueError) as exc:
        raise JobContextError("O diretório do trabalho não pertence a data/.") from exc
    if not directory.is_dir() or directory.name.casefold() != expected_name:
        raise JobContextError("O diretório do trabalho é inválido.")
    return directory


def resolve_job_input_file(
    value: str | Path,
    *,
    project_root: Path,
    environment: Mapping[str, str] | None = None,
) -> Path:
    """Resolve somente arquivo regular localizado no ``input/`` do trabalho atual."""
    input_directory = _resolve_job_directory(
        INPUT_ENVIRONMENT_VARIABLE,
        "input",
        project_root,
        environment,
    )
    raw = Path(value).expanduser()
    candidate = raw if raw.is_absolute() else input_directory / raw
    try:
        if candidate.is_symlink():
            raise JobContextError("O arquivo de entrada não é regular e seguro.")
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(input_directory)
    except (OSError, ValueError) as exc:
        raise JobContextError("O arquivo não pertence à entrada do trabalho.") from exc
    if not resolved.is_file() or resolved.is_symlink():
        raise JobContextError("O arquivo de entrada não é regular e seguro.")
    return resolved


def resolve_job_derived_directory(
    project_root: Path,
    environment: Mapping[str, str] | None = None,
) -> Path:
    """Resolve o ``derived/`` atual e confirma seu confinamento em ``data/``."""
    return _resolve_job_directory(
        DERIVED_ENVIRONMENT_VARIABLE,
        "derived",
        project_root,
        environment,
    )


def _existing_result(destination: Path, serialized: bytes) -> JobJsonFile:
    try:
        if destination.is_symlink() or not destination.is_file():
            raise JobContextError("O destino existente não é um arquivo regular seguro.")
        if destination.read_bytes() != serialized:
            raise JobContextError(
                "Já existe um documento diferente para a mesma chave idempotente."
            )
    except OSError as exc:
        raise JobContextError("Não foi possível validar o documento existente.") from exc
    return JobJsonFile(destination, created=False)


def write_job_json(
    namespace: str,
    idempotency_key: str,
    document: Mapping[str, Any],
    *,
    project_root: Path,
    environment: Mapping[str, str] | None = None,
) -> JobJsonFile:
    """Grava JSON determinístico no trabalho atual, sem destino ou overwrite livres."""
    if not isinstance(namespace, str) or not _NAMESPACE_PATTERN.fullmatch(namespace):
        raise JobContextError("O namespace do documento é inválido.")
    if (
        not isinstance(idempotency_key, str)
        or not idempotency_key
        or idempotency_key != idempotency_key.strip()
    ):
        raise JobContextError("A chave idempotente do documento é obrigatória.")
    if not isinstance(document, Mapping):
        raise JobContextError("O documento deve ser um objeto JSON.")
    try:
        serialized = (
            json.dumps(
                dict(document),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise JobContextError("O documento não pode ser serializado como JSON.") from exc

    derived = resolve_job_derived_directory(project_root, environment)
    digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:16]
    destination = derived / f"{namespace}-{digest}.json"
    if destination.exists():
        return _existing_result(destination, serialized)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=derived,
            prefix=f".{namespace}-",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary_path, destination)
        except FileExistsError:
            return _existing_result(destination, serialized)
    except OSError as exc:
        raise JobContextError("Não foi possível criar o documento do trabalho.") from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
    return JobJsonFile(destination, created=True)
