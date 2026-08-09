#!/usr/bin/env python3
"""Consulta e configura o scheduler da instância sem depender do Telegram."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scheduler import SchedulerStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scheduler independente da instância.")
    parser.add_argument("--database", default=str(PROJECT_ROOT / "data" / "scheduler" / "scheduler.sqlite3"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="Lista tarefas cadastradas.")
    commands.add_parser("status", help="Mostra tarefas e banco ativos.")
    for name, enabled in (("enable", True), ("disable", False)):
        action = commands.add_parser(name, help=f"{'Ativa' if enabled else 'Desativa'} uma tarefa.")
        action.add_argument("task_uid")
        action.set_defaults(enabled=enabled)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    store = SchedulerStore(Path(args.database))
    try:
        tasks = store.list_tasks()
        if args.command in {"list", "status"}:
            print(json.dumps({"ok": True, "database": str(Path(args.database).resolve()),
                              "count": len(tasks), "tasks": [task.__dict__ for task in tasks]},
                             ensure_ascii=False, indent=2))
            return 0
        if not store.set_enabled(args.task_uid, args.enabled):
            print(json.dumps({"ok": False, "error": "task_uid não encontrado."}, ensure_ascii=False))
            return 1
        print(json.dumps({"ok": True, "task_uid": args.task_uid, "enabled": args.enabled}, ensure_ascii=False))
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
