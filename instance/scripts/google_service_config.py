#!/usr/bin/env python3
"""Carregamento comum das configurações não confidenciais Google."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from google_api import validate_api_base
from integration_config import missing_config_message


@dataclass(frozen=True)
class GoogleServiceConfig:
    api_base: str
    google_config: Path
    timeout_seconds: int
    page_size: int
    max_pages: int
    max_response_bytes: int
    extras: dict[str, Any]


def load_service_config(
    path: Path,
    *,
    project_root: Path,
    default_path: Path,
    example_path: Path,
    service: str,
    api_host: str,
    api_path: str,
    max_page_size: int,
) -> GoogleServiceConfig:
    try:
        values = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(
            missing_config_message(default_path.stem, path)
        ) from exc
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(
            f"Não foi possível carregar a configuração {service} '{path}'."
        ) from exc
    api_base = validate_api_base(
        values.get("api_base"),
        host=api_host,
        path=api_path,
        field="api_base",
    )
    raw_google_path = str(values.get("google_config", "")).strip()
    if not raw_google_path:
        raise ValueError("'google_config' não pode ficar vazio.")
    google_path = Path(raw_google_path).expanduser()
    if not google_path.is_absolute():
        google_path = (project_root / google_path).resolve()
    timeout = values.get("timeout_seconds", 30)
    page_size = values.get("page_size", 100)
    max_pages = values.get("max_pages", 20)
    max_response = values.get("max_response_bytes", 5_242_880)
    if not isinstance(timeout, int) or not 1 <= timeout <= 120:
        raise ValueError("'timeout_seconds' deve estar entre 1 e 120.")
    if not isinstance(page_size, int) or not 1 <= page_size <= max_page_size:
        raise ValueError(
            f"'page_size' deve estar entre 1 e {max_page_size}."
        )
    if not isinstance(max_pages, int) or not 1 <= max_pages <= 100:
        raise ValueError("'max_pages' deve estar entre 1 e 100.")
    if not isinstance(max_response, int) or not 1024 <= max_response <= 50_000_000:
        raise ValueError("'max_response_bytes' está fora do limite permitido.")
    common = {
        "api_base",
        "google_config",
        "timeout_seconds",
        "page_size",
        "max_pages",
        "max_response_bytes",
    }
    return GoogleServiceConfig(
        api_base,
        google_path,
        timeout,
        page_size,
        max_pages,
        max_response,
        {key: value for key, value in values.items() if key not in common},
    )
