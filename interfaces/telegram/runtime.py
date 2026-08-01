"""Estado persistente e controle cooperativo do processo do gateway."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


RUNTIME_FILENAME = "gateway-runtime.json"
STOP_FILENAME = "gateway-stop-request.json"
RESTART_FILENAME = "gateway-restart-request.json"


class GatewayRuntimeError(RuntimeError):
    """Representa conflito ou falha no controle do processo do gateway."""


def _runtime_path(state_dir: Path) -> Path:
    return state_dir / RUNTIME_FILENAME


def _stop_path(state_dir: Path) -> Path:
    return state_dir / STOP_FILENAME


def _restart_path(state_dir: Path) -> Path:
    return state_dir / RESTART_FILENAME


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [
            ctypes.c_ulong,
            ctypes.c_int,
            ctypes.c_ulong,
        ]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetExitCodeProcess.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ulong),
        ]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def runtime_status(state_dir: Path) -> dict[str, Any]:
    runtime_file = _runtime_path(state_dir)
    value = _read_json(runtime_file)
    try:
        pid = int(value.get("pid", 0)) if value else 0
    except (TypeError, ValueError):
        pid = 0
    running = process_exists(pid)
    return {
        "running": running,
        "pid": pid or None,
        "started_at": str(value.get("started_at") or "") if value else "",
        "instance_id": str(value.get("instance_id") or "") if value else "",
        "stale": runtime_file.exists() and not running,
        "runtime_file": str(runtime_file),
        "stop_requested": _stop_path(state_dir).exists(),
        "restart_requested": _restart_path(state_dir).exists(),
    }


def clear_stale_runtime(state_dir: Path) -> bool:
    status = runtime_status(state_dir)
    if not status["stale"]:
        return False
    _runtime_path(state_dir).unlink(missing_ok=True)
    _stop_path(state_dir).unlink(missing_ok=True)
    _restart_path(state_dir).unlink(missing_ok=True)
    return True


def claim_runtime(state_dir: Path, instance_id: str) -> dict[str, Any]:
    state_dir.mkdir(parents=True, exist_ok=True)
    clear_stale_runtime(state_dir)
    runtime_file = _runtime_path(state_dir)
    payload = {
        "pid": os.getpid(),
        "instance_id": instance_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        with runtime_file.open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
    except FileExistsError as exc:
        current = runtime_status(state_dir)
        raise GatewayRuntimeError(
            f"O gateway já está em execução (PID {current.get('pid')})."
        ) from exc
    _stop_path(state_dir).unlink(missing_ok=True)
    _restart_path(state_dir).unlink(missing_ok=True)
    return payload


def release_runtime(state_dir: Path, pid: int) -> None:
    value = _read_json(_runtime_path(state_dir))
    try:
        recorded_pid = int(value.get("pid", 0)) if value else 0
    except (TypeError, ValueError):
        recorded_pid = 0
    if recorded_pid == pid:
        _runtime_path(state_dir).unlink(missing_ok=True)
        _stop_path(state_dir).unlink(missing_ok=True)
        _restart_path(state_dir).unlink(missing_ok=True)


def request_stop(state_dir: Path) -> dict[str, Any]:
    status = runtime_status(state_dir)
    if not status["running"]:
        clear_stale_runtime(state_dir)
        return {**status, "requested": False}
    payload = {
        "pid": status["pid"],
        "requested_at": datetime.now(timezone.utc).isoformat(),
    }
    state_dir.mkdir(parents=True, exist_ok=True)
    _stop_path(state_dir).write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {**status, "requested": True}


def stop_requested(state_dir: Path, pid: int) -> bool:
    value = _read_json(_stop_path(state_dir))
    if not value:
        return False
    try:
        return int(value.get("pid", 0)) == pid
    except (TypeError, ValueError):
        return False



def request_restart(state_dir: Path) -> dict[str, Any]:
    status = runtime_status(state_dir)
    if not status["running"]:
        clear_stale_runtime(state_dir)
        return {**status, "requested": False}
    payload = {
        "pid": status["pid"],
        "requested_at": datetime.now(timezone.utc).isoformat(),
    }
    state_dir.mkdir(parents=True, exist_ok=True)
    _restart_path(state_dir).write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {**status, "requested": True}


def restart_requested(state_dir: Path, pid: int) -> bool:
    value = _read_json(_restart_path(state_dir))
    if not value:
        return False
    try:
        return int(value.get("pid", 0)) == pid
    except (TypeError, ValueError):
        return False



def cancel_restart(state_dir: Path, pid: int) -> bool:
    if not restart_requested(state_dir, pid):
        return False
    _restart_path(state_dir).unlink(missing_ok=True)
    return True
