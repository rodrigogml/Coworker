#!/usr/bin/env python3
"""Agenda o reinício seguro do gateway por um relançador destacado."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
GATEWAY = PROJECT_ROOT / "interfaces" / "telegram" / "gateway.py"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from interfaces.telegram.config import DEFAULT_CONFIG, TelegramConfigError, load_config
from interfaces.telegram.runtime import (
    cancel_restart,
    clear_stale_runtime,
    process_exists,
    request_restart,
    runtime_status,
)


LOCK_FILENAME = "gateway-restart-worker.json"
LOG_FILENAME = "gateway-restart.log"


class RestartError(RuntimeError):
    """Representa falha no agendamento ou no handoff do gateway."""


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _log(path: Path, message: str) -> None:
    timestamp = datetime.now(timezone.utc).isoformat()
    with path.open("a", encoding="utf-8") as stream:
        stream.write(f"{timestamp} {message}\n")


def _detached_kwargs(
    log_stream: Any,
    *,
    breakaway: bool = True,
) -> dict[str, Any]:
    values: dict[str, Any] = {
        "cwd": PROJECT_ROOT,
        "stdin": subprocess.DEVNULL,
        "stdout": log_stream,
        "stderr": log_stream,
        "shell": False,
        "close_fds": True,
    }
    if os.name == "nt":
        flags = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NO_WINDOW
        )
        if breakaway:
            flags |= getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
        values["creationflags"] = flags
    else:
        values["start_new_session"] = True
    return values


def _spawn_detached(command: list[str], log_stream: Any) -> subprocess.Popen[Any]:
    try:
        return subprocess.Popen(command, **_detached_kwargs(log_stream))
    except OSError:
        if os.name != "nt":
            raise
        return subprocess.Popen(
            command,
            **_detached_kwargs(log_stream, breakaway=False),
        )


def _claim_handoff(lock_path: Path, expected_pid: int) -> str:
    existing = _read_json(lock_path)
    if existing:
        try:
            owner_pid = int(existing.get("owner_pid", 0))
        except (TypeError, ValueError):
            owner_pid = 0
        if process_exists(owner_pid):
            raise RestartError(
                f"Já existe um relançador ativo (PID {owner_pid})."
            )
        lock_path.unlink(missing_ok=True)
    token = secrets.token_hex(16)
    payload = {
        "token": token,
        "owner_pid": os.getpid(),
        "expected_gateway_pid": expected_pid,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        with lock_path.open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
    except FileExistsError as exc:
        raise RestartError("Outro relançador foi criado simultaneamente.") from exc
    return token


def _release_handoff(lock_path: Path, token: str) -> None:
    current = _read_json(lock_path)
    if current and secrets.compare_digest(str(current.get("token") or ""), token):
        lock_path.unlink(missing_ok=True)


def schedule_restart(config_path: Path) -> dict[str, Any]:
    resolved = config_path.expanduser().resolve()
    config = load_config(resolved, require_codex=False)
    if config.codex.access_mode != "super":
        raise RestartError("O reinício autônomo exige codex.access_mode = \"super\".")
    status = runtime_status(config.state_dir)
    if not status["running"] or not status["pid"]:
        raise RestartError("O gateway gerenciado não está em execução.")
    if status["instance_id"] != config.identity.instance_id:
        raise RestartError("O registro de execução pertence a outra instância.")

    config.state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = config.state_dir / LOCK_FILENAME
    log_path = config.state_dir / LOG_FILENAME
    expected_pid = int(status["pid"])
    token = _claim_handoff(lock_path, expected_pid)
    try:
        requested = request_restart(config.state_dir)
        if not requested["requested"] or requested["pid"] != expected_pid:
            raise RestartError("O gateway mudou antes do agendamento do reinício.")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            current = _read_json(lock_path) or {}
            try:
                helper_pid = int(current.get("owner_pid", 0))
            except (TypeError, ValueError):
                helper_pid = 0
            if helper_pid != os.getpid() and process_exists(helper_pid):
                return {
                    "ok": True,
                    "scheduled": True,
                    "gateway_pid": expected_pid,
                    "helper_pid": helper_pid,
                    "log": str(log_path),
                }
            time.sleep(0.1)
        raise RestartError("O gateway não iniciou o relançador em 10 segundos.")
    except Exception:
        cancel_restart(config.state_dir, expected_pid)
        _release_handoff(lock_path, token)
        raise


def spawn_relauncher(config_path: Path, expected_pid: int) -> int:
    resolved = config_path.expanduser().resolve()
    config = load_config(resolved, require_codex=False)
    if config.codex.access_mode != "super":
        raise RestartError('O relançador exige codex.access_mode = "super".')
    lock_path = config.state_dir / LOCK_FILENAME
    log_path = config.state_dir / LOG_FILENAME
    lock = _read_json(lock_path)
    if not lock:
        raise RestartError("A solicitação de handoff não foi encontrada.")
    try:
        recorded_pid = int(lock.get("expected_gateway_pid", 0))
    except (TypeError, ValueError) as exc:
        raise RestartError("A solicitação de handoff está inválida.") from exc
    token = str(lock.get("token") or "")
    if recorded_pid != expected_pid or not token:
        raise RestartError("A solicitação de handoff não corresponde ao gateway atual.")
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "_worker",
        "--config",
        str(resolved),
        "--expected-pid",
        str(expected_pid),
        "--token",
        token,
    ]
    try:
        with log_path.open("a", encoding="utf-8") as log_stream:
            helper = _spawn_detached(command, log_stream)
        _write_json(
            lock_path,
            {
                **lock,
                "owner_pid": helper.pid,
                "helper_started_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        return helper.pid
    except (OSError, subprocess.SubprocessError):
        _log(log_path, "falha ao iniciar o relançador externo")
        _release_handoff(lock_path, token)
        raise


def _wait_for_old_gateway(expected_pid: int, timeout_seconds: int) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not process_exists(expected_pid):
            return
        time.sleep(0.5)
    raise RestartError(
        f"O gateway PID {expected_pid} não encerrou em {timeout_seconds} segundos."
    )


def _launch_gateway(config_path: Path, state_dir: Path, log_path: Path) -> int:
    clear_stale_runtime(state_dir)
    command = [
        sys.executable,
        str(GATEWAY),
        "--config",
        str(config_path),
        "run",
    ]
    with log_path.open("a", encoding="utf-8") as log_stream:
        process = _spawn_detached(command, log_stream)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RestartError("A nova cópia do gateway encerrou durante a inicialização.")
        status = runtime_status(state_dir)
        if status["running"] and status["pid"] == process.pid:
            return process.pid
        time.sleep(0.2)
    process.terminate()
    raise RestartError("A nova cópia do gateway não confirmou a inicialização.")


def run_worker(
    config_path: Path,
    expected_pid: int,
    token: str,
) -> dict[str, Any]:
    resolved = config_path.expanduser().resolve()
    config = load_config(resolved, require_codex=False)
    if config.codex.access_mode != "super":
        raise RestartError('O relançador exige codex.access_mode = "super".')
    lock_path = config.state_dir / LOCK_FILENAME
    log_path = config.state_dir / LOG_FILENAME
    lock = _read_json(lock_path)
    if not lock or not secrets.compare_digest(str(lock.get("token") or ""), token):
        raise RestartError("O relançador não possui uma solicitação válida.")
    _write_json(
        lock_path,
        {
            **lock,
            "owner_pid": os.getpid(),
            "worker_started_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    try:
        _log(log_path, f"handoff iniciado para o gateway PID {expected_pid}")
        requested = request_restart(config.state_dir)
        if not requested["requested"] or requested["pid"] != expected_pid:
            raise RestartError("O gateway mudou antes da solicitação de reinício.")
        timeout = max(1800, min(config.codex.timeout_seconds * 4 + 600, 86400))
        _wait_for_old_gateway(expected_pid, timeout)
        _log(log_path, f"gateway PID {expected_pid} encerrado; iniciando substituto")
        for attempt in range(1, 4):
            try:
                new_pid = _launch_gateway(resolved, config.state_dir, log_path)
            except RestartError as exc:
                _log(log_path, f"tentativa {attempt}/3 falhou: {exc}")
                if attempt == 3:
                    raise
                time.sleep(attempt * 2)
                continue
            _log(log_path, f"gateway reiniciado com PID {new_pid}")
            return {"ok": True, "restarted": True, "pid": new_pid}
        raise RestartError("As tentativas de reinício foram esgotadas.")
    except Exception as exc:
        _log(log_path, f"falha no reinício: {type(exc).__name__}: {exc}")
        raise
    finally:
        _release_handoff(lock_path, token)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reinicia o gateway com handoff externo.")
    commands = parser.add_subparsers(dest="command", required=True)
    request = commands.add_parser("request", help="Agenda o reinício autônomo.")
    request.add_argument("--config", default=str(DEFAULT_CONFIG))
    worker = commands.add_parser("_worker", help=argparse.SUPPRESS)
    worker.add_argument("--config", required=True)
    worker.add_argument("--expected-pid", type=int, required=True)
    worker.add_argument("--token", required=True)
    return parser


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args()
    try:
        if args.command == "request":
            result = schedule_restart(Path(args.config))
        else:
            result = run_worker(
                Path(args.config),
                args.expected_pid,
                args.token,
            )
    except (RestartError, TelegramConfigError, OSError, subprocess.SubprocessError) as exc:
        print(
            json.dumps(
                {"ok": False, "error": str(exc), "error_type": type(exc).__name__},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
