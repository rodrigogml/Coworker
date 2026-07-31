"""Acesso seguro aos atributos de pessoas e organizações no KeePassXC."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

try:
    from pykeepass import PyKeePass
except ImportError as exc:  # pragma: no cover - depende do ambiente local
    raise SystemExit(
        "Dependência ausente. Execute: python -m pip install -r requirements.txt"
    ) from exc

try:
    from scripts import credential_vault
except ImportError:  # execução direta por python scripts/vault_entities.py
    import credential_vault  # type: ignore[no-redef]


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMA = PROJECT_ROOT / "config" / "vault-entities.toml"


class VaultEntityError(RuntimeError):
    """Indica uma falha sanitizada ao manipular uma entidade no cofre."""


def load_schema(path: Path = DEFAULT_SCHEMA) -> dict[str, Any]:
    """Carrega e valida a estrutura mínima do contrato de entidades."""
    try:
        with path.expanduser().resolve().open("rb") as stream:
            schema = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise VaultEntityError("Não foi possível carregar o contrato de entidades.") from exc
    if schema.get("version") != 1:
        raise VaultEntityError("Versão incompatível do contrato de entidades.")
    if not isinstance(schema.get("entity_types"), dict):
        raise VaultEntityError("O contrato não define tipos de entidade.")
    return schema


def _entity_definition(entry_path: str, schema: dict[str, Any]) -> dict[str, Any]:
    """Localiza o tipo de entidade pelo grupo definido no contrato."""
    normalized = credential_vault.validate_entry_path(entry_path)
    matches = [
        definition
        for definition in schema["entity_types"].values()
        if normalized.startswith(str(definition["group"]).rstrip("/") + "/")
    ]
    if len(matches) != 1:
        raise VaultEntityError("A entrada não pertence a um grupo de entidade conhecido.")
    return matches[0]


def _attribute_definition(
    entry_path: str,
    attribute: str,
    schema: dict[str, Any],
) -> dict[str, Any]:
    """Exige um atributo previamente definido para o tipo da entrada."""
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", attribute):
        raise VaultEntityError("Nome de atributo inválido.")
    attributes = _entity_definition(entry_path, schema).get("attributes", {})
    definition = attributes.get(attribute)
    if not isinstance(definition, dict):
        raise VaultEntityError("O atributo não está definido para esse tipo de entidade.")
    return definition


def _cpf_is_valid(value: str) -> bool:
    if len(value) != 11 or len(set(value)) == 1:
        return False
    numbers = [int(character) for character in value]
    first = (sum(numbers[index] * (10 - index) for index in range(9)) * 10) % 11
    first = 0 if first == 10 else first
    second = (sum(numbers[index] * (11 - index) for index in range(10)) * 10) % 11
    second = 0 if second == 10 else second
    return numbers[9:] == [first, second]


def _cnpj_digit(numbers: list[int], weights: list[int]) -> int:
    remainder = sum(number * weight for number, weight in zip(numbers, weights)) % 11
    return 0 if remainder < 2 else 11 - remainder


def _cnpj_is_valid(value: str) -> bool:
    if len(value) != 14 or len(set(value)) == 1:
        return False
    numbers = [int(character) for character in value]
    first = _cnpj_digit(numbers[:12], [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2])
    second = _cnpj_digit(
        numbers[:12] + [first],
        [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2],
    )
    return numbers[12:] == [first, second]


def normalize_value(value: str, definition: dict[str, Any]) -> str:
    """Normaliza e valida um valor sem incluí-lo em mensagens de erro."""
    normalized = value.strip()
    normalization = definition.get("normalization")
    if normalization == "uppercase_trim":
        normalized = normalized.upper()
    elif normalization == "uppercase_alphanumeric":
        normalized = "".join(
            character for character in normalized.upper() if character.isalnum()
        )
    allowed_values = definition.get("allowed_values")
    if isinstance(allowed_values, list):
        normalized = normalized.upper()
        if normalized not in allowed_values:
            raise VaultEntityError("O valor não pertence ao conjunto permitido.")
    pattern = definition.get("pattern")
    if pattern and re.fullmatch(str(pattern), normalized) is None:
        raise VaultEntityError("O valor não corresponde ao formato esperado.")
    validation = definition.get("validation")
    if validation == "cpf_checksum" and not _cpf_is_valid(normalized):
        raise VaultEntityError("O CPF não possui dígitos verificadores válidos.")
    if validation == "cnpj_checksum" and not _cnpj_is_valid(normalized):
        raise VaultEntityError("O CNPJ não possui dígitos verificadores válidos.")
    if not normalized:
        raise VaultEntityError("O valor do atributo não pode ficar vazio.")
    return normalized


def keepassxc_is_running() -> bool:
    """Detecta a interface no Windows para evitar gravações concorrentes."""
    if os.name != "nt":
        return False
    completed = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq KeePassXC.exe", "/NH", "/FO", "CSV"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return '"KeePassXC.exe"' in completed.stdout


def _fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _find_entry(database: PyKeePass, entry_path: str) -> Any:
    """Resolve uma entrada por caminho e recusa grupos ou títulos ambíguos."""
    parts = credential_vault.validate_entry_path(entry_path).split("/")
    group = database.root_group
    for group_name in parts[:-1]:
        matches = [child for child in group.subgroups if child.name == group_name]
        if len(matches) != 1:
            raise VaultEntityError("O caminho da entidade é inexistente ou ambíguo.")
        group = matches[0]
    matches = [entry for entry in group.entries if entry.title == parts[-1]]
    if len(matches) != 1:
        raise VaultEntityError("O título da entidade é inexistente ou ambíguo.")
    return matches[0]


def _open_database(
    *,
    config_path: Path = credential_vault.DEFAULT_CONFIG,
    master_password: str | None = None,
) -> tuple[PyKeePass, Path]:
    config = credential_vault.load_vault_config(config_path)
    credential_vault.require_file(config.vault_path, "Cofre")
    password = master_password
    if password is None:
        password = credential_vault.read_windows_credential(config.credential_target)
    try:
        database = PyKeePass(str(config.vault_path), password=password)
    except Exception as exc:
        raise VaultEntityError("Não foi possível abrir o cofre de entidades.") from exc
    finally:
        password = ""
    return database, config.vault_path


def read_entry_attribute(
    entry_path: str,
    attribute: str,
    *,
    config_path: Path = credential_vault.DEFAULT_CONFIG,
    schema_path: Path = DEFAULT_SCHEMA,
    master_password: str | None = None,
) -> str:
    """Lê internamente um atributo conhecido sem imprimi-lo."""
    schema = load_schema(schema_path)
    definition = _attribute_definition(entry_path, attribute, schema)
    database, _ = _open_database(
        config_path=config_path,
        master_password=master_password,
    )
    entry = _find_entry(database, entry_path)
    value = entry.get_custom_property(attribute)
    if value is None:
        raise VaultEntityError("A entidade não possui o atributo solicitado.")
    return normalize_value(value, definition)


def write_entry_attribute(
    entry_path: str,
    attribute: str,
    value: str,
    *,
    config_path: Path = credential_vault.DEFAULT_CONFIG,
    schema_path: Path = DEFAULT_SCHEMA,
    master_password: str | None = None,
    require_closed_gui: bool = True,
) -> dict[str, Any]:
    """Grava uma propriedade validada e devolve somente metadados não sensíveis."""
    schema = load_schema(schema_path)
    definition = _attribute_definition(entry_path, attribute, schema)
    normalized = normalize_value(value, definition)
    if require_closed_gui and keepassxc_is_running():
        raise VaultEntityError(
            "Feche o KeePassXC antes de alterar atributos para evitar gravação concorrente."
        )
    database, vault_path = _open_database(
        config_path=config_path,
        master_password=master_password,
    )
    original_fingerprint = _fingerprint(vault_path)
    entry = _find_entry(database, entry_path)
    entry.set_custom_property(
        attribute,
        normalized,
        protect=bool(definition.get("protected")),
    )
    if _fingerprint(vault_path) != original_fingerprint:
        raise VaultEntityError("O cofre mudou durante a operação; nenhuma gravação foi feita.")
    try:
        database.save()
    except Exception as exc:
        raise VaultEntityError("Não foi possível salvar o atributo no cofre.") from exc
    return {
        "ok": True,
        "entry": credential_vault.validate_entry_path(entry_path),
        "attribute": attribute,
        "protected": bool(definition.get("protected")),
        "value_exposed": False,
    }


def inspect_entry(
    entry_path: str,
    *,
    config_path: Path = credential_vault.DEFAULT_CONFIG,
    schema_path: Path = DEFAULT_SCHEMA,
    master_password: str | None = None,
) -> dict[str, Any]:
    """Lista somente presença e proteção dos atributos de uma entidade."""
    schema = load_schema(schema_path)
    definition = _entity_definition(entry_path, schema)
    database, _ = _open_database(
        config_path=config_path,
        master_password=master_password,
    )
    entry = _find_entry(database, entry_path)
    attributes = []
    missing_required = []
    for name, field in definition.get("attributes", {}).items():
        present = entry.get_custom_property(name) is not None
        if field.get("required") and not present:
            missing_required.append(name)
        attributes.append(
            {
                "name": name,
                "present": present,
                "protected": entry.is_custom_property_protected(name) if present else None,
                "expected_protected": bool(field.get("protected")),
            }
        )
    return {
        "ok": True,
        "entry": credential_vault.validate_entry_path(entry_path),
        "attributes": attributes,
        "missing_required": missing_required,
        "values_exposed": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Propriedades protegidas de pessoas e organizações no KeePassXC."
    )
    parser.add_argument("--config", type=Path, default=credential_vault.DEFAULT_CONFIG)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    commands = parser.add_subparsers(dest="command", required=True)

    inspect_parser = commands.add_parser("inspect", help="Lista campos sem valores.")
    inspect_parser.add_argument("--entry", required=True)

    set_parser = commands.add_parser("set", help="Grava um campo sem exibi-lo.")
    set_parser.add_argument("--entry", required=True)
    set_parser.add_argument("--attribute", required=True)
    value_source = set_parser.add_mutually_exclusive_group(required=True)
    value_source.add_argument("--stdin", action="store_true")
    value_source.add_argument("--prompt", action="store_true")
    return parser


def _read_stdin_value() -> str:
    """Lê UTF-8 com ou sem BOM, inclusive no Windows PowerShell clássico."""
    binary_stream = getattr(sys.stdin, "buffer", None)
    if binary_stream is not None:
        try:
            return binary_stream.readline().decode("utf-8-sig").rstrip("\r\n")
        except UnicodeDecodeError as exc:
            raise VaultEntityError("A entrada padrão deve usar UTF-8.") from exc
    return (
        sys.stdin.readline()
        .removeprefix("\ufeff")
        .removeprefix("ï»¿")
        .rstrip("\r\n")
    )


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "inspect":
            result = inspect_entry(
                args.entry,
                config_path=args.config,
                schema_path=args.schema,
            )
        else:
            value = _read_stdin_value() if args.stdin else getpass.getpass(
                f"Valor de {args.attribute}: "
            )
            result = write_entry_attribute(
                args.entry,
                args.attribute,
                value,
                config_path=args.config,
                schema_path=args.schema,
            )
            value = ""
    except (VaultEntityError, credential_vault.VaultToolError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
