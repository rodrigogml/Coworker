"""Testes do contrato e das propriedades protegidas de entidades."""

from __future__ import annotations

import importlib.util
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from pykeepass import PyKeePass, create_database


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PROJECT_ROOT / "scripts" / "vault_entities.py"
SPEC = importlib.util.spec_from_file_location("vault_entities", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Não foi possível carregar vault_entities.py.")
VAULT_ENTITIES = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = VAULT_ENTITIES
SPEC.loader.exec_module(VAULT_ENTITIES)


class VaultEntitiesTests(unittest.TestCase):
    """Valida escrita protegida, leitura interna e ausência de exposição."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        root = Path(self.temporary_directory.name)
        self.vault = root / "test.kdbx"
        self.password = "test-master-password"
        database = create_database(str(self.vault), password=self.password)
        people = database.add_group(database.root_group, "Pessoas")
        physical = database.add_group(people, "Fisicas")
        database.add_entry(physical, "Pessoa Teste", "", "")
        database.save()
        self.config = root / "secrets.toml"
        self.config.write_text(
            "[executables]\n"
            'gui = "KeePassXC.exe"\n'
            'cli = "keepassxc-cli.exe"\n\n'
            "[vault]\n"
            f'path = "{self.vault.as_posix()}"\n\n'
            "[windows_credential]\n"
            'target = "Coworker/Test"\n',
            encoding="utf-8",
        )
        self.entry = "Pessoas/Fisicas/Pessoa Teste"

    def write(self, attribute: str, value: str) -> dict[str, object]:
        return VAULT_ENTITIES.write_entry_attribute(
            self.entry,
            attribute,
            value,
            config_path=self.config,
            master_password=self.password,
            require_closed_gui=False,
        )

    def test_writes_and_reads_protected_cpf_without_returning_value(self) -> None:
        result = self.write("CPF", "12345678909")
        self.assertTrue(result["protected"])
        self.assertFalse(result["value_exposed"])
        self.assertNotIn("12345678909", str(result))
        self.assertEqual(
            VAULT_ENTITIES.read_entry_attribute(
                self.entry,
                "CPF",
                config_path=self.config,
                master_password=self.password,
            ),
            "12345678909",
        )
        database = PyKeePass(str(self.vault), password=self.password)
        entry = database.find_entries(title="Pessoa Teste", first=True)
        self.assertTrue(entry.is_custom_property_protected("CPF"))

    def test_rejects_invalid_cpf_without_including_it_in_error(self) -> None:
        invalid = "12345678900"
        with self.assertRaises(VAULT_ENTITIES.VaultEntityError) as context:
            self.write("CPF", invalid)
        self.assertNotIn(invalid, str(context.exception))

    def test_inspection_lists_metadata_but_never_values(self) -> None:
        self.write("NOME_TIPO", "completo")
        result = VAULT_ENTITIES.inspect_entry(
            self.entry,
            config_path=self.config,
            master_password=self.password,
        )
        self.assertFalse(result["values_exposed"])
        self.assertEqual(result["missing_required"], [])
        self.assertNotIn("COMPLETO", str(result))

    def test_refuses_write_while_keepassxc_is_open(self) -> None:
        with patch.object(VAULT_ENTITIES, "keepassxc_is_running", return_value=True):
            with self.assertRaisesRegex(VAULT_ENTITIES.VaultEntityError, "Feche"):
                VAULT_ENTITIES.write_entry_attribute(
                    self.entry,
                    "NOME_TIPO",
                    "COMPLETO",
                    config_path=self.config,
                    master_password=self.password,
                )

    def test_unknown_attribute_is_rejected(self) -> None:
        with self.assertRaisesRegex(VAULT_ENTITIES.VaultEntityError, "não está definido"):
            self.write("APELIDO_ALEATORIO", "Teste")

    def test_cli_accepts_utf8_bom_from_windows_powershell(self) -> None:
        class BinaryInput:
            def __init__(self) -> None:
                self.buffer = io.BytesIO(b"\xef\xbb\xbfCOMPLETO\r\n")

        arguments = [
            "vault_entities.py",
            "--config",
            str(self.config),
            "set",
            "--entry",
            self.entry,
            "--attribute",
            "NOME_TIPO",
            "--stdin",
        ]
        output = io.StringIO()
        with (
            patch.object(sys, "argv", arguments),
            patch.object(sys, "stdin", BinaryInput()),
            patch.object(VAULT_ENTITIES, "keepassxc_is_running", return_value=False),
            patch.object(
                VAULT_ENTITIES.credential_vault,
                "read_windows_credential",
                return_value=self.password,
            ),
            redirect_stdout(output),
        ):
            self.assertEqual(VAULT_ENTITIES.main(), 0)
        self.assertNotIn("COMPLETO", output.getvalue())


if __name__ == "__main__":
    unittest.main()
