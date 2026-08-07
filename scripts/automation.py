"""Administração local de tarefas de automação, sem SQL ou JSON arbitrário."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from interfaces.telegram.automation_state import AutomationState, AutomationStateError
from interfaces.telegram.scheduler import ScheduledTask, SchedulerError, SchedulerStore


def _state() -> AutomationState:
    root = Path(__file__).resolve().parents[1]
    return AutomationState(root / "data" / "automation" / "scheduler.sqlite3")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("list", "doctor", "history", "enable", "disable", "create"))
    parser.add_argument("task_uid", nargs="?")
    parser.add_argument("--topic-title")
    parser.add_argument("--group-alias")
    parser.add_argument("--chat-id", type=int)
    parser.add_argument("--prompt")
    parser.add_argument("--script")
    parser.add_argument("--trigger", choices=("interval", "once", "event"), default="interval")
    parser.add_argument("--interval-seconds", type=int, default=3600)
    parser.add_argument("--run-at")
    parser.add_argument("--thread-policy", choices=("new", "new_with_state", "resume"), default="new")
    parser.add_argument("--topic-policy", choices=("task", "run", "case"), default="run")
    parser.add_argument("--resumable", action="store_true")
    parser.add_argument("--enabled", action="store_true")
    args = parser.parse_args(argv)
    state = _state()
    try:
        if args.command == "doctor":
            result = {"ok": True, "path": str(state.path)}
        elif args.command == "create":
            if not args.task_uid or not args.topic_title or bool(args.prompt) == bool(args.script):
                parser.error("create exige task_uid, --topic-title e exatamente --prompt ou --script")
            if args.enabled and (args.chat_id is None or not args.group_alias):
                parser.error("tarefas habilitadas exigem --group-alias e --chat-id")
            definition = {
                "task_uid": args.task_uid, "topic_title": args.topic_title,
                "topic_policy": args.topic_policy, "thread_policy": args.thread_policy,
                "trigger": args.trigger, "resumable": args.resumable,
                "prompt": args.prompt, "script_id": args.task_uid if args.script else None,
                "enabled": args.enabled,
            }
            state.save_task(definition, group_alias=args.group_alias)
            scheduler = SchedulerStore(state.path)
            scheduler.save(ScheduledTask(
                args.task_uid, args.topic_title, args.trigger, args.thread_policy,
                script_path=args.script, prompt=args.prompt,
                interval_seconds=args.interval_seconds, enabled=args.enabled,
                run_at=args.run_at,
                resumable=args.resumable, topic_policy=args.topic_policy,
                telegram_chat_id=args.chat_id, group_alias=args.group_alias,
            ), Path(__file__).resolve().parents[1])
            scheduler.close()
            result = {"ok": True, "task_uid": args.task_uid, "enabled": args.enabled}
        elif args.command == "list":
            rows = state.connection.execute("SELECT * FROM automation_tasks ORDER BY task_uid").fetchall()
            result = {"ok": True, "tasks": [dict(row) for row in rows]}
        elif args.command in {"enable", "disable"}:
            if not args.task_uid:
                parser.error(f"{args.command} exige task_uid")
            state.set_task_enabled(args.task_uid, args.command == "enable")
            result = {"ok": True, "task_uid": args.task_uid, "enabled": args.command == "enable"}
        else:
            if not args.task_uid:
                parser.error("history exige task_uid")
            rows = state.connection.execute(
                "SELECT * FROM automation_runs WHERE task_uid=? ORDER BY created_at DESC",
                (args.task_uid,),
            ).fetchall()
            result = {"ok": True, "task_uid": args.task_uid, "runs": [dict(row) for row in rows]}
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    except (OSError, AutomationStateError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    finally:
        state.close()


if __name__ == "__main__":
    raise SystemExit(main())
