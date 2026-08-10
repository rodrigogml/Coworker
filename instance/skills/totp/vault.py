"""Leitura e escrita seletivas de entradas TOTP no KeePassXC."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from scripts import credential_vault

from .core import TotpConfig, TotpError, TotpRecord, build_config, entry_path

try:
    from pykeepass import PyKeePass
except ImportError:  # pragma: no cover
    PyKeePass = None  # type: ignore[assignment]


ATTRIBUTES = {
    "TOTP_ISSUER": "issuer",
    "TOTP_ACCOUNT": "account",
    "TOTP_ALGORITHM": "algorithm",
    "TOTP_DIGITS": "digits",
    "TOTP_PERIOD": "period",
}
PROTECTION_TITLE = "__protection__"


class TotpVaultError(RuntimeError):
    """Falha sanitizada ao acessar o cofre TOTP."""


def _require_database() -> Any:
    if PyKeePass is None:
        raise TotpVaultError("A dependência PyKeePass não está instalada.")
    return PyKeePass


def _open(config_path: Path | None = None) -> tuple[Any, Path]:
    config = credential_vault.load_vault_config(config_path or credential_vault.DEFAULT_CONFIG)
    credential_vault.require_file(config.vault_path, "Cofre")
    password = credential_vault.read_windows_credential(config.credential_target)
    try:
        database = _require_database()(str(config.vault_path), password=password)
    except Exception as exc:
        raise TotpVaultError("Não foi possível abrir o cofre TOTP.") from exc
    finally:
        password = ""
    return database, config.vault_path


def _group(database: Any, names: list[str], create: bool = False) -> Any:
    current = database.root_group
    for name in names:
        matches = [item for item in current.subgroups if item.name == name]
        if len(matches) > 1:
            raise TotpVaultError("O grupo TOTP é ambíguo.")
        if matches:
            current = matches[0]
        elif create:
            current = database.add_group(current, name)
        else:
            raise TotpVaultError("O grupo TOTP não existe.")
    return current


def _find(database: Any, entry: str) -> Any:
    parts = entry.split("/")
    if len(parts) != 2 or parts[0] != "TOTP":
        raise TotpVaultError("A entrada TOTP é inválida.")
    group = _group(database, ["TOTP"])
    matches = [item for item in group.entries if item.title == parts[1]]
    if len(matches) != 1:
        raise TotpVaultError("A entrada TOTP é inexistente ou ambígua.")
    return matches[0]


def store(config: TotpConfig, *, config_path: Path | None = None) -> TotpRecord:
    credential_vault.ensure_keepassxc_gui_closed()
    database, vault_path = _open(config_path)
    path = entry_path(config)
    group = _group(database, ["TOTP"], create=True)
    title = path.split("/", 1)[1]
    matches = [item for item in group.entries if item.title == title]
    if len(matches) > 1:
        raise TotpVaultError("A entrada TOTP é ambígua.")
    target = matches[0] if matches else database.add_entry(group, title, "", config.secret)
    target.password = config.secret
    values = {
        "TOTP_ISSUER": config.issuer,
        "TOTP_ACCOUNT": config.account,
        "TOTP_ALGORITHM": config.algorithm,
        "TOTP_DIGITS": str(config.digits),
        "TOTP_PERIOD": str(config.period),
    }
    for name, value in values.items():
        target.set_custom_property(name, value, protect=True)
    try:
        database.save()
    except Exception as exc:
        raise TotpVaultError("Não foi possível salvar a entrada TOTP.") from exc
    return TotpRecord(config, path)


def read(entry: str, *, config_path: Path | None = None) -> TotpRecord:
    database, _ = _open(config_path)
    target = _find(database, entry)
    try:
        values = {name: target.get_custom_property(name) for name in ATTRIBUTES}
        config = build_config(
            issuer=str(values["TOTP_ISSUER"] or ""),
            account=str(values["TOTP_ACCOUNT"] or ""),
            secret=str(target.password or ""),
            algorithm=str(values["TOTP_ALGORITHM"] or "SHA1"),
            digits=str(values["TOTP_DIGITS"] or "6"),
            period=str(values["TOTP_PERIOD"] or "30"),
        )
    except (KeyError, TotpError) as exc:
        raise TotpVaultError("A entrada TOTP não possui uma configuração válida.") from exc
    return TotpRecord(config, entry)


def list_records(*, config_path: Path | None = None) -> list[dict[str, str]]:
    database, _ = _open(config_path)
    try:
        group = _group(database, ["TOTP"])
    except TotpVaultError:
        return []
    result: list[dict[str, str]] = []
    for item in group.entries:
        issuer = str(item.get_custom_property("TOTP_ISSUER") or "")
        account = str(item.get_custom_property("TOTP_ACCOUNT") or "")
        if issuer and account:
            result.append({"entry": f"TOTP/{item.title}", "issuer": issuer, "account": account})
    return sorted(result, key=lambda value: (value["issuer"].casefold(), value["account"].casefold()))


def find_records(selector: str, *, config_path: Path | None = None) -> list[dict[str, str]]:
    normalized = str(selector or "").strip().casefold()
    records = list_records(config_path=config_path)
    if not normalized:
        return records
    return [
        item for item in records
        if normalized in {item["entry"].casefold(), item["issuer"].casefold(), item["account"].casefold()}
        or normalized in f'{item["issuer"]} {item["account"]}'.casefold()
    ]


def get_protection_hash(*, config_path: Path | None = None) -> str | None:
    """Retorna somente o hash da senha do módulo, nunca uma senha TOTP."""
    database, _ = _open(config_path)
    try:
        group = _group(database, ["TOTP"])
    except TotpVaultError:
        return None
    matches = [item for item in group.entries if item.title == PROTECTION_TITLE]
    if len(matches) > 1:
        raise TotpVaultError("A proteção TOTP está ambígua.")
    return str(matches[0].password or "") if matches else None


def set_protection_hash(value: str, *, config_path: Path | None = None) -> None:
    if not value or len(value) > 512 or "\n" in value or "\r" in value:
        raise TotpVaultError("Hash de proteção TOTP inválido.")
    credential_vault.ensure_keepassxc_gui_closed()
    database, _ = _open(config_path)
    group = _group(database, ["TOTP"], create=True)
    matches = [item for item in group.entries if item.title == PROTECTION_TITLE]
    if len(matches) > 1:
        raise TotpVaultError("A proteção TOTP está ambígua.")
    target = matches[0] if matches else database.add_entry(group, PROTECTION_TITLE, "", value)
    target.password = value
    target.set_custom_property("TOTP_PROTECTION", "pbkdf2-sha256", protect=True)
    try:
        database.save()
    except Exception as exc:
        raise TotpVaultError("Não foi possível salvar a proteção TOTP.") from exc


def delete(entry: str, *, config_path: Path | None = None) -> None:
    credential_vault.ensure_keepassxc_gui_closed()
    database, _ = _open(config_path)
    target = _find(database, entry)
    if target.title == PROTECTION_TITLE:
        raise TotpVaultError("A proteção TOTP não pode ser removida por esta operação.")
    try:
        database.delete_entry(target)
        database.save()
    except Exception as exc:
        raise TotpVaultError("Não foi possível excluir a entrada TOTP.") from exc
