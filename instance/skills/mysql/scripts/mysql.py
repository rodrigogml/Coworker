#!/usr/bin/env python3
"""Consultas somente leitura ao cliente mysql.exe, com credenciais do cofre."""
from __future__ import annotations
import argparse, json, os, shutil, subprocess, sys, tempfile, tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
CONFIG = ROOT / "data" / "config" / "mysql.toml"
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
from credential_vault import VaultToolError, read_entry_credentials, read_entry_attachment

class MySQLSkillError(RuntimeError): pass

def load_config(path: Path = CONFIG) -> dict[str, Any]:
    try: values = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc: raise MySQLSkillError("Configure o MySQL com 'python scripts/integration_config.py init mysql'.") from exc
    except (OSError, tomllib.TOMLDecodeError) as exc: raise MySQLSkillError("A configuração MySQL não pôde ser lida.") from exc
    if values.get("enabled") is not True: raise MySQLSkillError("A skill MySQL está desabilitada.")
    executable = str(values.get("mysql_executable", "")).strip()
    if not executable: raise MySQLSkillError("Informe o executável mysql.exe na configuração MySQL.")
    exe = Path(executable).expanduser()
    if not exe.is_file():
        discovered = shutil.which(executable)
        if not discovered: raise MySQLSkillError(f"mysql.exe não encontrado em '{exe}'.")
        executable = discovered
    values["mysql_executable"] = executable
    return values

def profile(values: dict[str, Any], name: str | None) -> tuple[str, dict[str, Any]]:
    selected = str(name or values.get("default_profile") or "").strip()
    profiles = values.get("profiles")
    if not selected or not isinstance(profiles, dict) or not isinstance(profiles.get(selected), dict):
        raise MySQLSkillError("O perfil MySQL solicitado não está configurado.")
    item = dict(profiles[selected]); host = str(item.get("host", "")).strip()
    try: port = int(item.get("port", 3306)); timeout = int(item.get("connect_timeout", 15))
    except (TypeError, ValueError) as exc: raise MySQLSkillError("Porta ou timeout MySQL inválido.") from exc
    if not host or any(c in host for c in "\r\n\0 \t") or not 1 <= port <= 65535 or timeout <= 0: raise MySQLSkillError("Host, porta ou timeout MySQL inválido.")
    if not str(item.get("credential_ref", "")).strip(): raise MySQLSkillError("A referência da credencial MySQL não está configurada.")
    item.update(host=host, port=port, connect_timeout=timeout)
    return selected, item

def _command(config: dict[str, Any], item: dict[str, Any], sql: str | None = None) -> tuple[list[str], dict[str, str], tempfile.TemporaryDirectory[str] | None]:
    ref = str(item["credential_ref"]).strip(); mode = str(item.get("credential_mode", "password")).strip().lower()
    env = os.environ.copy(); temp = None
    try: user, secret = read_entry_credentials(ref)
    except VaultToolError as exc: raise MySQLSkillError("A credencial MySQL não pôde ser acessada.") from exc
    command = [config["mysql_executable"], "--protocol=TCP", "--host", item["host"], "--port", str(item["port"]), "--connect-timeout", str(item["connect_timeout"]), "--batch", "--raw", "--skip-column-names"]
    if user: command += ["--user", user]
    if mode == "password":
        if secret: env["MYSQL_PWD"] = secret
    elif mode == "certificate":
        try: filename, data = read_entry_attachment(ref, str(item.get("attachment_name") or ""))
        except VaultToolError as exc: raise MySQLSkillError("O certificado MySQL não pôde ser acessado.") from exc
        temp = tempfile.TemporaryDirectory(prefix="coworker-mysql-"); cert = Path(temp.name) / filename; cert.write_bytes(data)
        command += ["--ssl-cert", str(cert)]
    else: raise MySQLSkillError("credential_mode deve ser 'password' ou 'certificate'.")
    ssl_mode = str(item.get("ssl_mode", "preferred")).strip().lower()
    if ssl_mode not in {"disabled", "preferred", "required", "verify_ca", "verify_identity"}:
        raise MySQLSkillError("ssl_mode MySQL inválido.")
    command += ["--ssl-mode", ssl_mode]
    if item.get("database"): command += ["--database", str(item["database"])]
    if sql is not None: command += ["--execute", sql]
    return command, env, temp

def run(values: dict[str, Any], name: str | None, sql: str | None) -> dict[str, Any]:
    if sql is not None:
        normalized = sql.lstrip().lower()
        if (not (normalized.startswith("select") or normalized.startswith("show") or normalized.startswith("describe") or normalized.startswith("desc") or normalized.startswith("explain"))
                or ";" in normalized.rstrip(";" )
                or any(token in normalized for token in ("insert ", "update ", "delete ", "drop ", "alter ", "create ", "truncate ", "grant ", "revoke ", "call "))):
            raise MySQLSkillError("A skill MySQL aceita somente consultas de leitura.")
    selected, item = profile(values, name); command, env, temp = _command(values, item, sql)
    try:
        completed = subprocess.run(command, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=item["connect_timeout"] + 5, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc: raise MySQLSkillError("Não foi possível executar o mysql.exe.") from exc
    finally:
        env.pop("MYSQL_PWD", None)
        if temp: temp.cleanup()
    if completed.returncode: raise MySQLSkillError("A operação MySQL falhou; verifique perfil e credenciais.")
    return {"ok": True, "profile": selected, "host": item["host"], "database": item.get("database", ""), "output": completed.stdout[:16384]}

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--profile"); sub = parser.add_subparsers(dest="operation", required=True)
    sub.add_parser("doctor"); query = sub.add_parser("query"); query.add_argument("--sql", required=True)
    args = parser.parse_args(argv)
    try:
        values = load_config(); result = run(values, args.profile, None if args.operation == "doctor" else args.sql)
        if args.operation == "doctor": result["operation"] = "connect"
    except MySQLSkillError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr); return 1
    print(json.dumps(result, ensure_ascii=False)); return 0
if __name__ == "__main__": raise SystemExit(main())
