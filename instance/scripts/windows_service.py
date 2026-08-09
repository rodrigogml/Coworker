"""Instala e hospeda o gateway Coworker no Service Control Manager do Windows.

O módulo mantém o gateway como processo filho e traduz os eventos do SCM para a
parada cooperativa já suportada pela interface Telegram. Nenhum segredo é gravado
na definição do serviço; a conta de execução é cadastrada diretamente no SCM.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import subprocess
import sys
import threading
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SERVICE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
STARTUP_TYPES = {"automatic", "automatic_delayed", "manual"}
ACCOUNT_MODES = {"current_user", "local_system"}


class WindowsServiceError(RuntimeError):
    """Erro operacional seguro da integração com o SCM."""


def service_exception_message(exc: BaseException, *, action: str = "operar", name: str = "") -> str:
    """Traduz falhas do SCM em orientação curta e acionável."""
    code = getattr(exc, "winerror", None)
    if code is None and getattr(exc, "args", ()):
        first = exc.args[0]
        if isinstance(first, int):
            code = first
    target = f" o serviço '{name}'" if name else " o serviço"
    if code == 5:
        return f"Não foi possível {action}{target}: acesso negado pelo SCM. Abra o configurador em um PowerShell elevado (Administrador)."
    if code == 1053:
        return f"Não foi possível {action}{target}: o serviço não respondeu no prazo (erro 1053). Verifique o gateway e reinstale a definição do serviço se ela ainda apontar para o layout antigo."
    if code == 1058:
        return f"Não foi possível {action}{target}: o serviço está desabilitado (erro 1058)."
    if code == 1060:
        return f"Não foi possível {action}{target}: o serviço não está instalado (erro 1060)."
    detail = str(exc).strip()
    suffix = f" (erro {code})" if code is not None else ""
    return f"Não foi possível {action}{target}{suffix}: {detail or 'falha desconhecida do SCM'}."


@dataclass(frozen=True)
class ServiceDefinition:
    project_root: Path
    gateway: Path
    python: Path
    state_dir: Path
    gateway_state_dir: Path
    codex_home: Path
    instance_id: str
    name: str
    display_name: str
    startup: str = "automatic_delayed"
    account_mode: str = "current_user"
    stop_timeout_seconds: int = 60


def validate_service_name(name: str) -> str:
    value = str(name or "").strip()
    if not SERVICE_NAME_PATTERN.fullmatch(value):
        raise WindowsServiceError(
            "O nome do serviço deve ter de 1 a 80 caracteres e usar somente "
            "letras, números, ponto, hífen ou sublinhado."
        )
    return value


def _require_windows() -> None:
    if os.name != "nt":
        raise WindowsServiceError("A instalação de serviço está disponível somente no Windows.")


def _win32() -> tuple[Any, Any, Any]:
    _require_windows()
    try:
        import win32service  # type: ignore
        import win32serviceutil  # type: ignore
        import servicemanager  # type: ignore
    except ImportError as exc:
        raise WindowsServiceError(
            "A dependência pywin32 não está instalada. Execute "
            "python -m pip install -r requirements.txt."
        ) from exc
    return win32service, win32serviceutil, servicemanager


def build_definition(
    project_root: Path,
    *,
    instance_id: str,
    display_name: str,
    service_name: str | None = None,
    startup: str = "automatic_delayed",
    account_mode: str = "current_user",
    stop_timeout_seconds: int = 60,
) -> ServiceDefinition:
    root = project_root.resolve()
    name = validate_service_name(service_name or instance_id)
    if startup not in STARTUP_TYPES:
        raise WindowsServiceError(f"Inicialização inválida: {startup}.")
    if account_mode not in ACCOUNT_MODES:
        raise WindowsServiceError(f"Conta de serviço inválida: {account_mode}.")
    if stop_timeout_seconds < 10 or stop_timeout_seconds > 3600:
        raise WindowsServiceError("O timeout de parada deve estar entre 10 e 3600 segundos.")
    gateway = root / "interfaces" / "telegram" / "gateway.py"
    python = Path(sys.executable).resolve()
    if not gateway.is_file():
        raise WindowsServiceError(f"Gateway não encontrado em '{gateway}'.")
    state_dir = root / "data" / "service" / name
    gateway_state_dir = state_dir
    codex_home = root / "data" / "codex"
    telegram_config = root / "data" / "config" / "telegram.toml"
    if telegram_config.is_file():
        try:
            values = tomllib.loads(telegram_config.read_text(encoding="utf-8"))
            configured = str(((values.get("codex") or {}).get("home_dir") or "")).strip()
            if configured:
                candidate = Path(configured).expanduser()
                codex_home = candidate if candidate.is_absolute() else (root / candidate)
            # The gateway's cooperative stop marker belongs to its configured
            # runtime state, not to the service's own logs/definition folder.
            try:
                sys.path.insert(0, str(root))
                from interfaces.telegram.config import load_config
                gateway_state_dir = load_config(telegram_config, require_codex=False).state_dir
            except Exception:
                # The gateway defaults to the per-instance local state path
                # even when the private TOML is not complete yet.
                try:
                    from interfaces.telegram.config import default_state_dir
                    gateway_state_dir = default_state_dir(str(instance_id))
                except Exception:
                    # The service definition can still be inspected/removed
                    # while an incomplete private config is being repaired.
                    pass
        except (OSError, tomllib.TOMLDecodeError):
            pass
    return ServiceDefinition(
        root, gateway, python, state_dir.resolve(), gateway_state_dir.resolve(), codex_home.resolve(), str(instance_id), name,
        str(display_name).strip() or name, startup, account_mode, stop_timeout_seconds,
    )


def _definition_payload(definition: ServiceDefinition) -> dict[str, Any]:
    return {
        "project_root": str(definition.project_root),
        "gateway": str(definition.gateway),
        "python": str(definition.python),
        "state_dir": str(definition.state_dir),
        "gateway_state_dir": str(definition.gateway_state_dir),
        "codex_home": str(definition.codex_home),
        "instance_id": definition.instance_id,
        "name": definition.name,
        "display_name": definition.display_name,
        "startup": definition.startup,
        "account_mode": definition.account_mode,
        "stop_timeout_seconds": definition.stop_timeout_seconds,
    }


def write_definition(definition: ServiceDefinition) -> Path:
    definition.state_dir.mkdir(parents=True, exist_ok=True)
    path = definition.state_dir / "service.json"
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(_definition_payload(definition), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    return path


def _binary_path(definition: ServiceDefinition, config_path: Path) -> str:
    values = [str(definition.python), str(Path(__file__).resolve()), "run", "--config", str(config_path)]
    return " ".join('"' + value.replace('"', '\\"') + '"' for value in values)


def _service_handle(win32service: Any, name: str, access: int) -> tuple[Any, Any]:
    manager = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
    try:
        service = win32service.OpenService(manager, name, access)
    except Exception:
        win32service.CloseServiceHandle(manager)
        raise
    return manager, service


def service_status(name: str) -> dict[str, Any]:
    win32service, _util, _manager = _win32()
    name = validate_service_name(name)
    try:
        manager, service = _service_handle(
            win32service, name,
            win32service.SERVICE_QUERY_STATUS | win32service.SERVICE_QUERY_CONFIG,
        )
    except Exception as exc:
        if getattr(exc, "winerror", None) == 1060:
            return {"ok": True, "installed": False, "name": name}
        raise WindowsServiceError(
            f"NÃ£o foi possÃ­vel consultar o serviÃ§o '{name}'. Verifique as permissÃµes administrativas."
        ) from exc
    try:
        status = win32service.QueryServiceStatusEx(service)
        config = win32service.QueryServiceConfig(service)
        return {
            "ok": True, "installed": True, "name": name,
            "state": int(status["CurrentState"]),
            "state_name": _state_name(win32service, int(status["CurrentState"])),
            "start_type": int(config[1]), "binary_path": str(config[3]),
        }
    finally:
        win32service.CloseServiceHandle(service)
        win32service.CloseServiceHandle(manager)


def _state_name(win32service: Any, value: int) -> str:
    names = {
        win32service.SERVICE_STOPPED: "stopped",
        win32service.SERVICE_START_PENDING: "start_pending",
        win32service.SERVICE_STOP_PENDING: "stop_pending",
        win32service.SERVICE_RUNNING: "running",
        win32service.SERVICE_CONTINUE_PENDING: "continue_pending",
        win32service.SERVICE_PAUSE_PENDING: "pause_pending",
        win32service.SERVICE_PAUSED: "paused",
    }
    return names.get(value, str(value))


def _account_credentials(definition: ServiceDefinition, *, non_interactive: bool) -> tuple[str | None, str | None]:
    if definition.account_mode == "local_system":
        return None, None
    username = os.environ.get("USERDOMAIN")
    user = getpass.getuser()
    account = f"{username}\\{user}" if username else f".\\{user}"
    if non_interactive:
        raise WindowsServiceError(
            "A conta current_user exige instalação interativa para que a senha seja "
            "entregue diretamente ao SCM; não use senha em argumentos."
        )
    password = getpass.getpass(f"Senha da conta {account} para o serviço: ")
    if not password:
        raise WindowsServiceError("A senha da conta do serviço não pode ficar vazia.")
    return account, password


def _configure_recovery(definition: ServiceDefinition) -> None:
    command = [
        "sc.exe", "failure", definition.name, "reset=", "86400",
        "actions=", 'restart/5000/restart/30000/""/0',
    ]
    completed = subprocess.run(command, capture_output=True, text=True, shell=False, check=False)
    if completed.returncode != 0:
        raise WindowsServiceError(
            "O serviço foi criado, mas a política de recuperação não pôde ser configurada. "
            + (completed.stderr.strip() or completed.stdout.strip())
        )


def install_service(definition: ServiceDefinition, *, non_interactive: bool = False) -> dict[str, Any]:
    win32service, _util, _manager = _win32()
    config_path = write_definition(definition)
    current = service_status(definition.name)
    binary = _binary_path(definition, config_path)
    if current["installed"]:
        if current.get("binary_path") == binary:
            return {**current, "installed_now": False, "config": str(config_path)}
        raise WindowsServiceError(
            f"O serviço '{definition.name}' já existe com outra definição. Remova-o "
            "explicitamente antes de reinstalar."
        )
    account, password = _account_credentials(definition, non_interactive=non_interactive)
    start_type = {
        "automatic": win32service.SERVICE_AUTO_START,
        "automatic_delayed": win32service.SERVICE_AUTO_START,
        "manual": win32service.SERVICE_DEMAND_START,
    }[definition.startup]
    manager = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_ALL_ACCESS)
    service = None
    try:
        service = win32service.CreateService(
            manager, definition.name, definition.display_name,
            win32service.SERVICE_ALL_ACCESS, win32service.SERVICE_WIN32_OWN_PROCESS,
            start_type, win32service.SERVICE_ERROR_NORMAL, binary, None, 0, None,
            account, password,
        )
        if definition.startup == "automatic_delayed":
            try:
                win32service.ChangeServiceConfig2(
                    service, win32service.SERVICE_CONFIG_DELAYED_AUTO_START_INFO, True
                )
            except Exception as exc:
                raise WindowsServiceError("Não foi possível configurar início automático atrasado.") from exc
    finally:
        if service is not None:
            win32service.CloseServiceHandle(service)
        win32service.CloseServiceHandle(manager)
    try:
        _configure_recovery(definition)
    except Exception:
        # Do not leave an unmanaged partial installation behind when recovery
        # policy configuration fails.
        try:
            remove_service(definition.name)
        except Exception:
            pass
        raise
    return {"ok": True, "installed": True, "installed_now": True, "name": definition.name, "config": str(config_path)}


def remove_service(name: str, *, timeout_seconds: int = 60) -> dict[str, Any]:
    win32service, _util, _manager = _win32()
    try:
        import win32con  # type: ignore
        delete_access = win32con.DELETE
    except (ImportError, AttributeError) as exc:
        raise WindowsServiceError(
            "A dependência pywin32 não expõe a permissão DELETE do SCM."
        ) from exc
    status = service_status(name)
    if not status["installed"]:
        return {**status, "removed": False, "already_removed": True}
    name = validate_service_name(name)
    try:
        manager, service = _service_handle(
            win32service, name,
            win32service.SERVICE_STOP | delete_access | win32service.SERVICE_QUERY_STATUS,
        )
    except Exception as exc:
        raise WindowsServiceError(
            service_exception_message(exc, action="acessar", name=name)
        ) from exc
    try:
        if status["state_name"] not in {"stopped", "stop_pending"}:
            try:
                win32service.ControlService(service, win32service.SERVICE_CONTROL_STOP)
            except Exception:
                pass
            deadline = time.monotonic() + timeout_seconds
            stopped = False
            while time.monotonic() < deadline:
                current = win32service.QueryServiceStatusEx(service)
                if _state_name(win32service, int(current["CurrentState"])) == "stopped":
                    stopped = True
                    break
                time.sleep(0.25)
            if not stopped:
                raise WindowsServiceError(
                    f"O serviÃ§o '{name}' nÃ£o confirmou a parada em {timeout_seconds} segundos."
                )
        win32service.DeleteService(service)
    finally:
        win32service.CloseServiceHandle(service)
        win32service.CloseServiceHandle(manager)
    return {"ok": True, "removed": True, "name": name}


def control_service(name: str, action: str) -> dict[str, Any]:
    win32service, _util, _manager = _win32()
    name = validate_service_name(name)
    try:
        manager, service = _service_handle(
            win32service, name,
            win32service.SERVICE_START | win32service.SERVICE_STOP | win32service.SERVICE_QUERY_STATUS,
        )
    except Exception as exc:
        raise WindowsServiceError(
            service_exception_message(exc, action="acessar", name=name)
        ) from exc
    try:
        try:
            if action == "start":
                win32service.StartService(service, [])
            elif action == "stop":
                win32service.ControlService(service, win32service.SERVICE_CONTROL_STOP)
            else:
                raise WindowsServiceError(f"Ação de serviço inválida: {action}.")
        except WindowsServiceError:
            raise
        except Exception as exc:
            verb = "iniciar" if action == "start" else "parar"
            raise WindowsServiceError(
                service_exception_message(exc, action=verb, name=name)
            ) from exc
    finally:
        win32service.CloseServiceHandle(service)
        win32service.CloseServiceHandle(manager)
    return service_status(name)


def _read_definition(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WindowsServiceError(f"Não foi possível ler a definição do serviço '{path}'.") from exc
    if not isinstance(value, dict):
        raise WindowsServiceError("A definição do serviço deve ser um objeto JSON.")
    return value


def run_service_from_config(path: Path) -> None:
    win32service, _util, servicemanager = _win32()
    import win32event  # type: ignore
    values = _read_definition(path)
    project_root = Path(str(values["project_root"])).resolve()
    state_dir = Path(str(values["state_dir"])).resolve()
    gateway_state_dir = Path(str(values.get("gateway_state_dir") or state_dir)).resolve()
    codex_home = Path(str(values.get("codex_home") or project_root / "data" / "codex")).resolve()
    instance_id = str(values["instance_id"])
    python = Path(str(values["python"])).resolve()
    gateway = Path(str(values["gateway"])).resolve()
    stop_timeout = int(values.get("stop_timeout_seconds", 60))

    class GatewayService(_util.ServiceFramework):
        _svc_name_ = str(values["name"])
        _svc_display_name_ = str(values.get("display_name") or _svc_name_)
        _svc_description_ = f"Coworker Telegram gateway — {instance_id}"

        def __init__(self, args: list[str]) -> None:
            super().__init__(args)
            self.stop_event = win32event.CreateEvent(None, 0, 0, None)
            self.child: subprocess.Popen[bytes] | None = None
            self.stop_thread: threading.Thread | None = None
            self.stop_lock = threading.Lock()

        def SvcStop(self) -> None:  # noqa: N802
            # O callback do SCM precisa retornar rapidamente. Aguardar o
            # gateway aqui faz o SCM concluir que o serviço parou de responder
            # (erro 1053), mesmo quando o filho está encerrando corretamente.
            self.ReportServiceStatus(
                win32service.SERVICE_STOP_PENDING,
                waitHint=max(1000, stop_timeout * 1000),
                checkPoint=1,
            )
            win32event.SetEvent(self.stop_event)
            with self.stop_lock:
                if self.stop_thread is None or not self.stop_thread.is_alive():
                    self.stop_thread = threading.Thread(
                        target=self._stop_child,
                        name=f"{self._svc_name_}-stop",
                        daemon=True,
                    )
                    self.stop_thread.start()

        def SvcDoRun(self) -> None:  # noqa: N802
            servicemanager.LogInfoMsg(f"{self._svc_name_} iniciando o gateway")
            self._run_child()

        def _run_child(self) -> None:
            state_dir.mkdir(parents=True, exist_ok=True)
            log = state_dir / "service.log"
            environment = os.environ.copy()
            environment["CODEX_HOME"] = str(codex_home)
            environment["COWORKER_INSTANCE_ID"] = instance_id
            with log.open("ab") as stream:
                self.child = subprocess.Popen(
                    [str(python), str(gateway), "run"], cwd=project_root,
                    stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
                    shell=False, env=environment,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                self.child.wait()
            if self.child.returncode not in (0, None):
                servicemanager.LogErrorMsg(f"{self._svc_name_} encerrou com código {self.child.returncode}")

        def _stop_child(self) -> None:
            if self.child is None or self.child.poll() is not None:
                return
            try:
                sys.path.insert(0, str(project_root))
                from interfaces.telegram.runtime import request_stop
                request_stop(gateway_state_dir)
            except Exception:
                pass
            deadline = time.monotonic() + stop_timeout
            checkpoint = 1
            while self.child.poll() is None and time.monotonic() < deadline:
                try:
                    self.child.wait(timeout=1)
                    break
                except subprocess.TimeoutExpired:
                    checkpoint += 1
                    try:
                        self.ReportServiceStatus(
                            win32service.SERVICE_STOP_PENDING,
                            waitHint=2000,
                            checkPoint=checkpoint,
                        )
                    except Exception:
                        pass
            if self.child.poll() is None:
                self.child.terminate()
                try:
                    self.child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.child.kill()

    servicemanager.Initialize(GatewayService._svc_name_, None)
    servicemanager.PrepareToHostSingle(GatewayService)
    try:
        servicemanager.StartServiceCtrlDispatcher()
    except Exception as exc:
        if getattr(exc, "winerror", None) == 1063:
            raise WindowsServiceError(
                "O host de serviço só pode ser executado pelo Service Control Manager. "
                "Use o comando de início do serviço; não execute a ação 'run' diretamente no prompt."
            ) from exc
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "remove", "start", "stop", "status", "run"))
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--config", type=Path)
    parser.add_argument("--instance-id", default="")
    parser.add_argument("--display-name", default="")
    parser.add_argument("--service-name", default="")
    parser.add_argument("--startup", choices=sorted(STARTUP_TYPES), default="automatic_delayed")
    parser.add_argument("--account-mode", choices=sorted(ACCOUNT_MODES), default="current_user")
    parser.add_argument("--stop-timeout", type=int, default=60)
    parser.add_argument("--non-interactive", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.action == "run":
            if not args.config:
                raise WindowsServiceError("--config é obrigatório para run.")
            run_service_from_config(args.config.resolve())
            return 0
        if args.action == "status":
            result = service_status(args.service_name or args.instance_id)
        elif args.action == "remove":
            result = remove_service(args.service_name or args.instance_id, timeout_seconds=args.stop_timeout)
        elif args.action in {"start", "stop"}:
            result = control_service(args.service_name or args.instance_id, args.action)
        else:
            definition = build_definition(
                args.project_root, instance_id=args.instance_id, display_name=args.display_name,
                service_name=args.service_name or None, startup=args.startup,
                account_mode=args.account_mode, stop_timeout_seconds=args.stop_timeout,
            )
            result = install_service(definition, non_interactive=args.non_interactive)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (WindowsServiceError, OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    except Exception as exc:
        # pywin32 exposes SCM access and state failures as pywintypes.error,
        # which is not consistently an OSError across supported versions.
        # Keep the CLI structured and actionable instead of leaking a traceback.
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
