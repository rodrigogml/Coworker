from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "instance"))

from scripts.app_logging import close_logging, configure_logging, log_event  # noqa: E402


class LoggingConfig:
    enabled = True
    level = "DEBUG"
    retention_days = 15

    def __init__(self, directory: Path) -> None:
        self.directory = directory


def test_application_log_is_json_and_uses_configured_directory(tmp_path: Path) -> None:
    logger = configure_logging(LoggingConfig(tmp_path))
    try:
        log_event(logger, logging.INFO, "reaction_test", chat_id=10, message_id=20, emoji="✅")
        for handler in logger.handlers:
            handler.flush()
        content = (tmp_path / "coworker.log").read_text(encoding="utf-8")
    finally:
        close_logging()
    assert '"event": "reaction_test"' in content
    assert '"message_id": 20' in content
    assert "✅" in content


def test_application_log_purges_files_older_than_retention(tmp_path: Path) -> None:
    old = tmp_path / "coworker.log.2020-01-01"
    old.write_text("old", encoding="utf-8")
    old_time = time.time() - 16 * 86400
    os.utime(old, (old_time, old_time))
    configure_logging(LoggingConfig(tmp_path))
    close_logging()
    assert not old.exists()
