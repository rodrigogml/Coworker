"""Testes das operações não confidenciais do cofre."""

from __future__ import annotations

import argparse
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PROJECT_ROOT / "scripts" / "credential_vault.py"
SPEC = importlib.util.spec_from_file_location("credential_vault", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Não foi possível carregar credential_vault.py.")
CREDENTIAL_VAULT = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = CREDENTIAL_VAULT
SPEC.loader.exec_module(CREDENTIAL_VAULT)


class CredentialVaultTest(unittest.TestCase):
    """Valida configuração, caminhos e ausência de exposição de segredos."""

    def setUp(self) -> None:
        """Cria executáveis e cofre descartáveis."""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        root = Path(self.temporary_directory.name)
        self.gui = root / "KeePassXC.exe"
        self.cli = root / "keepassxc-cli.exe"
        self.vault = root / "botina.kdbx"
        self.gui.touch()
        self.cli.touch()

    def arguments(self) -> argparse.Namespace:
        """Monta argumentos comuns dos comandos."""
        return argparse.Namespace(
            gui=str(self.gui),
            cli=str(self.cli),
            vault=str(self.vault),
            credential_target="BOTina/Test",
        )

    def test_status_does_not_expose_secrets(self) -> None:
        """O diagnóstico informa somente ferramenta, versão e caminho."""
        self.vault.touch()
        with (
            patch.object(
                CREDENTIAL_VAULT,
                "executable_version",
                return_value="2.7.12",
            ),
            patch.object(
                CREDENTIAL_VAULT,
                "windows_credential_exists",
                return_value=True,
            ),
        ):
            result = CREDENTIAL_VAULT.command_status(self.arguments())
        self.assertTrue(result["vault_exists"])
        self.assertTrue(result["master_password_enrolled"])
        self.assertFalse(result["secrets_may_be_printed"])
        self.assertNotIn("password", result)

    def test_loads_private_toml_configuration(self) -> None:
        """Caminhos relativos da configuração são resolvidos pelo projeto."""
        config_path = Path(self.temporary_directory.name) / "secrets.toml"
        config_path.write_text(
            "[executables]\n"
            'gui = "tools/KeePassXC.exe"\n'
            'cli = "tools/keepassxc-cli.exe"\n\n'
            "[vault]\n"
            'path = "data/secrets/botina.kdbx"\n\n'
            "[windows_credential]\n"
            'target = "BOTina/Test"\n',
            encoding="utf-8",
        )
        config = CREDENTIAL_VAULT.load_vault_config(config_path)
        self.assertEqual("BOTina/Test", config.credential_target)
        self.assertEqual(
            CREDENTIAL_VAULT.PROJECT_ROOT / "data" / "secrets" / "botina.kdbx",
            config.vault_path,
        )

    def test_create_rejects_existing_vault(self) -> None:
        """A criação não pode sobrescrever um cofre existente."""
        self.vault.touch()
        with self.assertRaises(CREDENTIAL_VAULT.VaultToolError):
            CREDENTIAL_VAULT.command_create(self.arguments())

    def test_entry_path_rejects_control_characters(self) -> None:
        """Uma entrada não pode injetar uma segunda linha de comando."""
        with self.assertRaises(CREDENTIAL_VAULT.VaultToolError):
            CREDENTIAL_VAULT.validate_entry_path("APIs/Gmail\ncomando")

    def test_check_result_rejects_invalid_identifier(self) -> None:
        """Um resultado arbitrário não pode ser lido fora do diretório previsto."""
        arguments = argparse.Namespace(request_id="../arquivo")
        with self.assertRaises(CREDENTIAL_VAULT.VaultToolError):
            CREDENTIAL_VAULT.command_check_result(arguments)

    def test_interactive_launcher_does_not_use_shell(self) -> None:
        """Argumentos interativos são encaminhados como lista para evitar injeção."""
        with patch.object(CREDENTIAL_VAULT.subprocess, "Popen") as popen:
            popen.return_value.pid = 123
            process_id = CREDENTIAL_VAULT.launch_interactive(
                self.cli,
                ["show", str(self.vault), "APIs/Teste&Comando"],
                "Teste",
            )
        self.assertEqual(123, process_id)
        launched_arguments = popen.call_args.args[0]
        self.assertIsInstance(launched_arguments, list)
        self.assertIn("APIs/Teste&Comando", launched_arguments)

    def test_check_uses_enrolled_password_without_exposing_it(self) -> None:
        """A verificação automática devolve somente a existência da entrada."""
        self.vault.touch()
        arguments = self.arguments()
        arguments.entry = "APIs/CloudFlare"
        completed = CREDENTIAL_VAULT.subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="CloudFlare\n",
            stderr="",
        )
        with (
            patch.object(
                CREDENTIAL_VAULT,
                "windows_credential_exists",
                return_value=True,
            ),
            patch.object(
                CREDENTIAL_VAULT,
                "read_windows_credential",
                return_value="senha-de-teste",
            ),
            patch.object(
                CREDENTIAL_VAULT,
                "run_keepassxc",
                return_value=completed,
            ),
        ):
            result = CREDENTIAL_VAULT.command_check(arguments)
        self.assertTrue(result["entry_exists"])
        self.assertFalse(result["interactive"])
        self.assertNotIn("senha-de-teste", str(result))

    def test_reads_username_and_password_from_one_entry(self) -> None:
        """Uma integração obtém o par composto em uma única consulta ao cofre."""
        self.vault.touch()
        completed = CREDENTIAL_VAULT.subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="usuario-de-teste\nsenha-de-teste\n",
            stderr="",
        )
        with (
            patch.object(
                CREDENTIAL_VAULT,
                "read_windows_credential",
                return_value="senha-mestra",
            ),
            patch.object(
                CREDENTIAL_VAULT,
                "run_keepassxc",
                return_value=completed,
            ) as run_keepassxc,
        ):
            credentials = CREDENTIAL_VAULT.read_entry_credentials(
                "APIs/Omie",
                cli_path=self.cli,
                vault_path=self.vault,
                credential_target="BOTina/Test",
            )

        self.assertEqual(("usuario-de-teste", "senha-de-teste"), credentials)
        arguments = run_keepassxc.call_args.args[1]
        self.assertEqual(
            [
                "show",
                "--show-protected",
                "--attributes",
                "Username",
                "--attributes",
                "Password",
                str(self.vault),
                "APIs/Omie",
            ],
            arguments,
        )

    def test_composed_credential_requires_both_fields(self) -> None:
        """Uma entrada incompleta falha sem incluir o valor presente na mensagem."""
        self.vault.touch()
        completed = CREDENTIAL_VAULT.subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="\nsegredo-que-nao-pode-vazar\n",
            stderr="",
        )
        with (
            patch.object(
                CREDENTIAL_VAULT,
                "read_windows_credential",
                return_value="senha-mestra",
            ),
            patch.object(
                CREDENTIAL_VAULT,
                "run_keepassxc",
                return_value=completed,
            ),
            self.assertRaises(CREDENTIAL_VAULT.VaultToolError) as raised,
        ):
            CREDENTIAL_VAULT.read_entry_credentials(
                "APIs/Omie",
                cli_path=self.cli,
                vault_path=self.vault,
                credential_target="BOTina/Test",
            )

        self.assertNotIn("segredo-que-nao-pode-vazar", str(raised.exception))

    def test_unenroll_requires_confirmation(self) -> None:
        """A senha cadastrada não pode ser removida acidentalmente."""
        arguments = self.arguments()
        arguments.confirm = False
        with self.assertRaises(CREDENTIAL_VAULT.VaultToolError):
            CREDENTIAL_VAULT.command_unenroll(arguments)


if __name__ == "__main__":
    unittest.main()
