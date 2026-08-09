#!/usr/bin/env python3
"""Diagnósticos SSH com chave privada protegida pelo KeePassXC."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import tempfile
import tomllib
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = PROJECT_ROOT / "data" / "config" / "ssh.toml"
MAX_KEY_SIZE = 128 * 1024
MAX_OUTPUT = 4096

try:
    from scripts import credential_vault
except ImportError:  # pragma: no cover
    import sys
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
    import credential_vault  # type: ignore[no-redef]


class SSHSkillError(RuntimeError):
    """Erro esperado e seguro para apresentação ao chamador."""


def _load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    if not path.is_file():
        raise SSHSkillError("Configure a integração SSH com 'python scripts/integration_config.py init ssh'.")
    try:
        with path.open("rb") as stream:
            values = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SSHSkillError("A configuração SSH não pôde ser lida.") from exc
    if not isinstance(values, dict):
        raise SSHSkillError("A configuração SSH é inválida.")
    return values


def _profile(values: dict[str, Any], name: str | None) -> tuple[str, dict[str, Any]]:
    selected = str(name or values.get("default_profile") or "").strip()
    profiles = values.get("profiles")
    if not selected or not isinstance(profiles, dict) or not isinstance(profiles.get(selected), dict):
        raise SSHSkillError("O perfil SSH solicitado não está configurado.")
    profile = dict(profiles[selected])
    host = str(profile.get("host") or "").strip()
    reference = str(profile.get("credential_ref") or "").strip()
    try:
        port = int(profile.get("port", 22))
    except (TypeError, ValueError) as exc:
        raise SSHSkillError("A porta SSH é inválida.") from exc
    if not host or any(char in host for char in "\r\n\0 \t") or not 1 <= port <= 65535:
        raise SSHSkillError("Host ou porta SSH inválidos.")
    if not reference:
        raise SSHSkillError("A referência da credencial SSH não está configurada.")
    profile.update(host=host, port=port, credential_ref=reference,
                   attachment_name=str(profile.get("attachment_name") or "").strip())
    return selected, profile


def _validate_key(filename: str, data: bytes) -> None:
    if not filename or len(filename) > 255 or filename.lower().endswith(".pub"):
        raise SSHSkillError("O anexo não é uma chave privada SSH válida.")
    if len(data) > MAX_KEY_SIZE or b"\0" in data:
        raise SSHSkillError("O anexo da chave excede o limite ou contém dados inválidos.")
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError as exc:
        raise SSHSkillError("O anexo da chave não está em formato textual suportado.") from exc
    headers = ("OPENSSH PRIVATE KEY", "RSA PRIVATE KEY", "EC PRIVATE KEY", "DSA PRIVATE KEY")
    if not any(f"-----BEGIN {header}-----" in text for header in headers):
        raise SSHSkillError("O anexo não contém um cabeçalho de chave privada SSH.")


def _read_material(profile: dict[str, Any]) -> tuple[str, str, bytes]:
    try:
        username = credential_vault.read_entry_username(profile["credential_ref"])
        passphrase = credential_vault.read_entry_secret(profile["credential_ref"])
        try:
            filename, key = credential_vault.read_entry_attachment(
                profile["credential_ref"], profile.get("attachment_name") or None
            )
        except credential_vault.VaultToolError as expected_error:
            # Migrações antigas podem ter preservado o nome enviado pelo Telegram.
            # Sem nome configurado, ou com mais de um anexo, a leitura continua recusada.
            if not profile.get("attachment_name"):
                raise
            try:
                filename, key = credential_vault.read_entry_attachment(
                    profile["credential_ref"], None
                )
            except credential_vault.VaultToolError:
                raise expected_error
    except credential_vault.VaultToolError as exc:
        raise SSHSkillError("A credencial SSH não pôde ser acessada.") from exc
    if not username:
        raise SSHSkillError("A credencial SSH não contém usuário.")
    _validate_key(filename, key)
    return username, passphrase, key


def _safe_output(value: str, passphrase: str, key: bytes) -> str:
    text = " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())
    if passphrase:
        text = text.replace(passphrase, "[Censurado por segurança]")
    try:
        key_text = key.decode("ascii")
    except UnicodeDecodeError:
        key_text = ""
    if key_text:
        text = text.replace(key_text, "[Censurado por segurança]")
    return text[:MAX_OUTPUT]


def _check(profile: dict[str, Any]) -> dict[str, Any]:
    username, passphrase, key = _read_material(profile)
    workdir = Path(tempfile.mkdtemp(prefix="coworker-ssh-"))
    helper: Path | None = None
    try:
        key_path = workdir / "id_key"
        descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(key)
        try:
            os.chmod(key_path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        env = os.environ.copy()
        env.update({"SSH_ASKPASS_REQUIRE": "force", "DISPLAY": "coworker"})
        if passphrase:
            if os.name == "nt":
                helper = workdir / "askpass.cmd"
                helper.write_text("@echo %COWORKER_SSH_ASKPASS%\r\n", encoding="ascii", newline="")
            else:
                helper = workdir / "askpass.sh"
                helper.write_text("#!/bin/sh\nprintf '%s' \"$COWORKER_SSH_ASKPASS\"\n", encoding="ascii")
                os.chmod(helper, 0o700)
            env["SSH_ASKPASS"] = str(helper)
            env["COWORKER_SSH_ASKPASS"] = passphrase
        command = ["ssh", "-o", f"BatchMode={'no' if passphrase else 'yes'}", "-o", "ConnectTimeout=15",
                   "-o", "StrictHostKeyChecking=accept-new", "-i", str(key_path), "-p", str(profile["port"]),
                   f"{username}@{profile['host']}", "uname -a"]
        try:
            completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                       timeout=30, check=False, env=env)
        except FileNotFoundError as exc:
            raise SSHSkillError("O executável OpenSSH não foi encontrado.") from exc
        except subprocess.TimeoutExpired as exc:
            raise SSHSkillError("O diagnóstico SSH excedeu o tempo limite.") from exc
        if completed.returncode:
            raise SSHSkillError("O diagnóstico SSH falhou; verifique acesso, host e chave.")
        return {"ok": True, "host": profile["host"], "user": username, "operation": "uname",
                "impact": "diagnóstico remoto somente leitura", "output": _safe_output(completed.stdout, passphrase, key)}
    finally:
        passphrase = ""
        key = b""
        if helper is not None:
            helper.unlink(missing_ok=True)
        shutil.rmtree(workdir, ignore_errors=True)


def doctor(profile: dict[str, Any], name: str) -> dict[str, Any]:
    username, passphrase, key = _read_material(profile)
    try:
        return {"ok": True, "profile": name, "host_configured": True, "port": profile["port"],
                "username_configured": bool(username), "key_attachment_valid": True,
                "passphrase_configured": bool(passphrase)}
    finally:
        passphrase = ""
        key = b""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Diagnóstico SSH seguro da Coworker.")
    parser.add_argument("--profile", required=True)
    parser.add_argument("action", choices=("doctor", "check"))
    try:
        args = parser.parse_args(argv)
        name, profile = _profile(_load_config(), args.profile)
        result = doctor(profile, name) if args.action == "doctor" else _check(profile)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (SSHSkillError, credential_vault.VaultToolError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    except Exception:
        print(json.dumps({"ok": False, "error": "A operação SSH falhou de forma segura."}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
