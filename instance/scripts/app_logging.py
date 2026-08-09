"""Logging estruturado, local e com retenção para uma instância Coworker."""

from __future__ import annotations

import json
import logging
import logging.handlers
import time
from pathlib import Path
from typing import Any


LOG_NAME = "coworker"
_handler: logging.Handler | None = None


def _safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def log_event(logger: logging.Logger, level: int, event: str, **fields: Any) -> None:
    """Registra um evento JSON sem aceitar texto confidencial por padrão."""
    payload = {"event": event, **{key: _safe(value) for key, value in fields.items()}}
    logger.log(level, json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _purge_old_logs(directory: Path, retention_days: int) -> None:
    cutoff = time.time() - retention_days * 86400
    for path in directory.glob("coworker.log*"):
        if not path.is_file():
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            continue


def configure_logging(config: Any) -> logging.Logger:
    """Configura o logger da aplicação e devolve o logger raiz do projeto."""
    global _handler
    logger = logging.getLogger(LOG_NAME)
    logger.setLevel(getattr(logging, config.level))
    logger.propagate = False
    if _handler is not None:
        logger.removeHandler(_handler)
        _handler.close()
        _handler = None
    if not config.enabled:
        return logger
    directory = Path(config.directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    _purge_old_logs(directory, config.retention_days)
    handler = logging.handlers.TimedRotatingFileHandler(
        directory / "coworker.log",
        when="midnight",
        interval=1,
        backupCount=config.retention_days,
        encoding="utf-8",
        delay=True,
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(handler)
    _handler = handler
    log_event(logger, logging.INFO, "logging_configured", configured_level=config.level, directory=directory, retention_days=config.retention_days)
    return logger


def close_logging() -> None:
    global _handler
    logger = logging.getLogger(LOG_NAME)
    if _handler is not None:
        logger.removeHandler(_handler)
        _handler.close()
        _handler = None
