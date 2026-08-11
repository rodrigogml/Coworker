#!/usr/bin/env python3
"""Abre o Codex CLI interativo com a política da instância atual.

Este ponto de entrada é local e iterativo. A configuração privada continua sendo
carregada de ``instance/data/config/telegram.toml``; nenhum segredo é copiado para
argumentos ou para arquivos fora de ``instance/data``.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PROJECT_ROOT / "data" / "config" / "telegram.toml"
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) in sys.path:
    sys.path.remove(str(SCRIPT_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from interfaces.telegram.codex import (  # noqa: E402
    CodexAdapter,
    CodexExecutionError,
    CodexOptions,
    ProcessRegistry,
)
from interfaces.telegram.config import TelegramConfigError, load_config  # noqa: E402


def build_command(adapter: CodexAdapter) -> list[str]:
    """Monta a linha interativa usando as mesmas conversões do gateway."""

    config = adapter.config
    options = CodexOptions(
        model=config.model,
        reasoning_effort=config.reasoning_effort,
        speed=config.speed,
        verbosity=config.verbosity,
    )
    command = [str(config.executable), "--cd", str(adapter.project_root)]
    for override in adapter.permission_overrides():
        command.extend(["--config", override])
    command.extend(["--config", f'approval_policy="{config.approval_policy}"'])
    for override in adapter.option_overrides(options):
        command.extend(["--config", override])
    for directory in config.additional_directories:
        command.extend(["--add-dir", str(directory)])
    return command


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        description="Abre o Codex CLI interativo com a configuração da instância."
    )


def main() -> int:
    build_parser().parse_args()
    try:
        config = load_config(DEFAULT_CONFIG)
        adapter = CodexAdapter(config.codex, config.project_root, ProcessRegistry())
        # O gateway faz esta sincronização antes de usar o Codex. O modo local
        # deve produzir exatamente o mesmo conjunto de regras.
        adapter.sync_rules()
        completed = subprocess.run(
            build_command(adapter),
            cwd=config.project_root,
            env=adapter._environment(),
            check=False,
            shell=False,
        )
        return completed.returncode
    except (TelegramConfigError, CodexExecutionError, FileNotFoundError) as exc:
        print(f"Não foi possível iniciar o Codex interativo: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
