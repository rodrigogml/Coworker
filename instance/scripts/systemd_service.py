"""Administração segura do gateway Coworker como serviço systemd."""

from __future__ import annotations

import getpass
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


class SystemdServiceError(RuntimeError):
    """Erro operacional seguro do supervisor Linux."""


SERVICE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,79}$")


@dataclass(frozen=True)
class SystemdDefinition:
    name: str
    project_root: Path
    python: Path
    gateway: Path
    data_dir: Path
    codex_home: Path
    credential_path: Path
    credential_name: str = "coworker-master-password"
    user: str = ""
    unit_dir: Path = Path("/etc/systemd/system")

    @property
    def unit_path(self) -> Path:
        return self.unit_dir / f"{self.name}.service"


def validate_service_name(name: str) -> str:
    value = str(name or "").strip()
    if not SERVICE_NAME_PATTERN.fullmatch(value):
        raise SystemdServiceError("Nome de serviço systemd inválido.")
    return value


def _unit_quote(value: Path | str) -> str:
    text = str(value)
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_definition(
    project_root: Path,
    *,
    instance_id: str,
    service_name: str | None = None,
    user: str | None = None,
    unit_dir: Path = Path("/etc/systemd/system"),
    credential_path: Path | None = None,
    credential_name: str = "coworker-master-password",
) -> SystemdDefinition:
    root = project_root.resolve()
    name = validate_service_name(service_name or f"coworker-{instance_id}")
    if not credential_name or any(char in credential_name for char in "/\\\0\r\n"):
        raise SystemdServiceError("Nome da credencial systemd inválido.")
    gateway = root / "interfaces" / "telegram" / "gateway.py"
    if not gateway.is_file():
        raise SystemdServiceError(f"Gateway não encontrado em '{gateway}'.")
    python = Path(os.environ.get("PYTHON", "") or os.sys.executable).resolve()
    data_dir = root / "data"
    return SystemdDefinition(
        name=name,
        project_root=root,
        python=python,
        gateway=gateway,
        data_dir=data_dir,
        codex_home=data_dir / "codex",
        credential_path=(credential_path or data_dir / "secrets" / "master-password.cred").resolve(),
        credential_name=credential_name,
        user=user or getpass.getuser(),
        unit_dir=unit_dir.resolve(),
    )


def render_unit(definition: SystemdDefinition) -> str:
    """Gera uma unidade sem segredos e sem comandos shell."""
    if not definition.user:
        raise SystemdServiceError("Usuário do serviço não pode ficar vazio.")
    return "\n".join(
        (
            "[Unit]",
            f"Description=Coworker Telegram gateway ({definition.name})",
            "Wants=network-online.target",
            "After=network-online.target",
            "",
            "[Service]",
            "Type=simple",
            f"User={definition.user}",
            f"WorkingDirectory={_unit_quote(definition.project_root)}",
            f"ExecStart={_unit_quote(definition.python)} {_unit_quote(definition.gateway)}",
            f"Environment=COWORKER_INSTANCE_ID={definition.name}",
            f"Environment=CODEX_HOME={_unit_quote(definition.codex_home)}",
            f"LoadCredentialEncrypted={definition.credential_name}:{_unit_quote(definition.credential_path)}",
            "Restart=on-failure",
            "RestartSec=5s",
            "NoNewPrivileges=true",
            "PrivateTmp=true",
            "ProtectSystem=full",
            "ProtectHome=true",
            f"ReadWritePaths={_unit_quote(definition.data_dir)}",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
            "",
        )
    )


def _systemctl(*arguments: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["systemctl", *arguments],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
        )
    except OSError as exc:
        raise SystemdServiceError("systemctl não foi encontrado no PATH.") from exc


def _check(result: subprocess.CompletedProcess[str], action: str) -> None:
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise SystemdServiceError(
            f"systemctl não conseguiu {action}. {detail or 'Verifique permissões e journalctl.'}"
        )


def install_service(definition: SystemdDefinition, *, start: bool = False) -> dict[str, object]:
    if os.name == "nt":
        raise SystemdServiceError("systemd está disponível somente no Linux.")
    if not definition.credential_path.is_file():
        raise SystemdServiceError(
            f"Credencial systemd não encontrada em '{definition.credential_path}'. "
            "Execute o provisionamento Linux do cofre antes de instalar o serviço."
        )
    definition.unit_dir.mkdir(parents=True, exist_ok=True)
    definition.unit_path.write_text(render_unit(definition), encoding="utf-8")
    os.chmod(definition.unit_path, 0o644)
    _check(_systemctl("daemon-reload"), "recarregar unidades")
    _check(_systemctl("enable", definition.name), "habilitar o serviço")
    if start:
        _check(_systemctl("start", definition.name), "iniciar o serviço")
    return {"ok": True, "platform": "linux", "name": definition.name, "unit": str(definition.unit_path), "started": start}


def service_status(name: str) -> dict[str, object]:
    value = validate_service_name(name)
    result = _systemctl("is-active", value)
    state = (result.stdout or "").strip() or "inactive"
    enabled = _systemctl("is-enabled", value)
    installed = enabled.returncode == 0 or result.returncode == 0
    return {"ok": True, "platform": "linux", "name": value, "installed": installed, "state_name": state}


def control_service(name: str, action: str) -> dict[str, object]:
    value = validate_service_name(name)
    if action not in {"start", "stop", "restart"}:
        raise SystemdServiceError("Ação systemd inválida.")
    _check(_systemctl(action, value), f"{action} o serviço")
    return {**service_status(value), action + "ed": True}


def remove_service(name: str, *, unit_dir: Path = Path("/etc/systemd/system")) -> dict[str, object]:
    value = validate_service_name(name)
    _check(_systemctl("disable", "--now", value), "desabilitar o serviço")
    unit = unit_dir.resolve() / f"{value}.service"
    if unit.exists():
        unit.unlink()
    _check(_systemctl("daemon-reload"), "recarregar unidades")
    return {"ok": True, "platform": "linux", "name": value, "removed": True}
