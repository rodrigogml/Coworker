"""Testes das operações não confidenciais do cofre."""

from __future__ import annotations

import argparse
import importlib.util
import io
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
        root = Path(self.temporary_directory.name).resolve()
        self.gui = root / "KeePassXC.exe"
        self.cli = root / "keepassxc-cli.exe"
        self.vault = root / "vault.kdbx"
        self.gui.touch()
        self.cli.touch()

    def arguments(self) -> argparse.Namespace:
        """Monta argumentos comuns dos comandos."""
        return argparse.Namespace(
            gui=str(self.gui),
            cli=str(self.cli),
            vault=str(self.vault),
            credential_target="Coworker/Test",
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
            'path = "data/secrets/vault.kdbx"\n\n'
            "[windows_credential]\n"
            'target = "Coworker/Test"\n',
            encoding="utf-8",
        )
        config = CREDENTIAL_VAULT.load_vault_config(config_path)
        self.assertEqual("Coworker/Test", config.credential_target)
        self.assertEqual(
            CREDENTIAL_VAULT.PROJECT_ROOT / "data" / "secrets" / "vault.kdbx",
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

    def test_entry_path_rejects_option_like_segments(self) -> None:
        """Uma referência não pode ser interpretada como opção do CLI."""
        with self.assertRaises(CREDENTIAL_VAULT.VaultToolError):
            CREDENTIAL_VAULT.validate_entry_path("APIs/--show-protected/Teste")

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
                credential_target="Coworker/Test",
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
                credential_target="Coworker/Test",
            )

        self.assertNotIn("segredo-que-nao-pode-vazar", str(raised.exception))

    def test_writes_credentials_without_secret_in_arguments(self) -> None:
        """OAuth pode persistir um refresh token sem devolvê-lo ao agente."""
        self.vault.touch()
        completed = CREDENTIAL_VAULT.subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="",
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
            patch.object(
                CREDENTIAL_VAULT.subprocess,
                "run",
                return_value=completed,
            ) as run,
        ):
            CREDENTIAL_VAULT.write_entry_credentials(
                "APIs/Google/Accounts/Pessoal",
                "pessoal@example.com",
                "refresh-token-secreto",
                cli_path=self.cli,
                vault_path=self.vault,
                credential_target="Coworker/Test",
            )

        arguments = run.call_args.args[0]
        standard_input = run.call_args.kwargs["input"]
        self.assertNotIn("refresh-token-secreto", arguments)
        self.assertIn("refresh-token-secreto", standard_input)
        self.assertEqual("pessoal@example.com", arguments[4])
        self.assertNotIn("refresh-token-secreto", completed.stdout)

    def test_write_failure_never_includes_secret(self) -> None:
        """Falhas ao persistir OAuth permanecem sanitizadas."""
        self.vault.touch()
        success = CREDENTIAL_VAULT.subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="",
            stderr="",
        )
        failure = CREDENTIAL_VAULT.subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="refresh-token-secreto",
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
                return_value=success,
            ),
            patch.object(
                CREDENTIAL_VAULT.subprocess,
                "run",
                return_value=failure,
            ),
            self.assertRaises(CREDENTIAL_VAULT.VaultToolError) as raised,
        ):
            CREDENTIAL_VAULT.write_entry_credentials(
                "APIs/Google/Accounts/Pessoal",
                "pessoal@example.com",
                "refresh-token-secreto",
                cli_path=self.cli,
                vault_path=self.vault,
                credential_target="Coworker/Test",
            )

        self.assertNotIn("refresh-token-secreto", str(raised.exception))

    def test_writes_simple_secret_without_username_or_secret_in_arguments(self) -> None:
        self.vault.touch()
        completed = CREDENTIAL_VAULT.subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
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
            patch.object(
                CREDENTIAL_VAULT.subprocess,
                "run",
                return_value=completed,
            ) as run,
        ):
            CREDENTIAL_VAULT.write_entry_secret(
                "APIs/Telegram/teste",
                "token-secreto",
                cli_path=self.cli,
                vault_path=self.vault,
                credential_target="Coworker/Test",
            )

        arguments = run.call_args.args[0]
        standard_input = run.call_args.kwargs["input"]
        self.assertNotIn("--username", arguments)
        self.assertNotIn("token-secreto", arguments)
        self.assertIn("token-secreto", standard_input)

    def test_store_reads_secret_from_stdin_and_never_returns_it(self) -> None:
        arguments = self.arguments()
        arguments.entry = "APIs/Todoist"
        arguments.stdin = True
        with (
            patch.object(sys, "stdin", io.StringIO("token-secreto\n")),
            patch.object(CREDENTIAL_VAULT, "write_entry_secret") as write,
        ):
            result = CREDENTIAL_VAULT.command_store(arguments)

        write.assert_called_once_with(
            "APIs/Todoist",
            "token-secreto",
            cli_path=self.cli,
            vault_path=self.vault,
            credential_target="Coworker/Test",
        )
        self.assertNotIn("token-secreto", str(result))
        self.assertFalse(result["secret_exposed"])

    def test_write_is_blocked_while_keepassxc_gui_is_open(self) -> None:
        completed = CREDENTIAL_VAULT.subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout='"KeePassXC.exe","47300"',
            stderr="",
        )
        with (
            patch.object(CREDENTIAL_VAULT.os, "name", "nt"),
            patch.object(
                CREDENTIAL_VAULT.subprocess,
                "run",
                return_value=completed,
            ),
            self.assertRaisesRegex(CREDENTIAL_VAULT.VaultToolError, "Feche"),
        ):
            CREDENTIAL_VAULT.ensure_keepassxc_gui_closed()

    def test_migrate_moves_secret_without_returning_it(self) -> None:
        arguments = self.arguments()
        arguments.source = "APIs/app Notion"
        arguments.target = "APIs/Notion"
        arguments.confirm = True
        with patch.object(CREDENTIAL_VAULT, "migrate_entry_secret") as migrate:
            result = CREDENTIAL_VAULT.command_migrate(arguments)

        migrate.assert_called_once_with(
            "APIs/app Notion",
            "APIs/Notion",
            cli_path=self.cli,
            vault_path=self.vault,
            credential_target="Coworker/Test",
        )
        self.assertNotIn("token-secreto", str(result))
        self.assertTrue(result["source_removed"])
        self.assertFalse(result["secret_exposed"])

    def test_copy_requires_confirmation_and_returns_no_value(self) -> None:
        arguments = self.arguments()
        arguments.source = "APIs/Origem"
        arguments.target = "APIs/Destino"
        arguments.source_field = "Password"
        arguments.target_field = None
        arguments.confirm = False
        with self.assertRaises(CREDENTIAL_VAULT.VaultToolError):
            CREDENTIAL_VAULT.command_copy(arguments)

    def test_copy_password_uses_keepass_library_without_cli_value(self) -> None:
        self.vault.touch()
        arguments = self.arguments()
        arguments.source = "APIs/Origem"
        arguments.target = "APIs/Destino"
        arguments.source_field = "Password"
        arguments.target_field = None
        arguments.confirm = True

        class Group:
            def __init__(self) -> None:
                self.subgroups = []
                self.entries = []

        class Entry:
            def __init__(self, title: str, password: str) -> None:
                self.title = title
                self.username = ""
                self.password = password

        root = Group()
        apis = Group()
        apis.name = "APIs"
        root.subgroups = [apis]
        source = Entry("Origem", "segredo-nao-deve-retornar")
        target = Entry("Destino", "senha-antiga")
        apis.entries = [source, target]

        class Database:
            root_group = root

            def __init__(self, *_args, **_kwargs) -> None:
                self.saved = False

            def save(self) -> None:
                self.saved = True

        with (
            patch.object(CREDENTIAL_VAULT, "PyKeePass", Database),
            patch.object(CREDENTIAL_VAULT, "read_windows_credential", return_value="mestra"),
            patch.object(CREDENTIAL_VAULT, "ensure_keepassxc_gui_closed"),
        ):
            result = CREDENTIAL_VAULT.command_copy(arguments)

        self.assertTrue(result["copied"])
        self.assertFalse(result["secret_exposed"])
        self.assertEqual("segredo-nao-deve-retornar", target.password)
        self.assertNotIn("segredo-nao-deve-retornar", str(result))

    def test_add_prepares_groups_and_updates_an_existing_entry(self) -> None:
        self.vault.touch()
        arguments = self.arguments()
        arguments.entry = "APIs/Telegram/rodriclone"
        arguments.username = None
        arguments.url = None
        with (
            patch.object(
                CREDENTIAL_VAULT,
                "windows_credential_exists",
                return_value=True,
            ),
            patch.object(
                CREDENTIAL_VAULT,
                "read_windows_credential",
                return_value="senha-mestra",
            ),
            patch.object(CREDENTIAL_VAULT, "_ensure_vault_groups") as ensure,
            patch.object(
                CREDENTIAL_VAULT,
                "_vault_item_exists",
                return_value=True,
            ),
            patch.object(
                CREDENTIAL_VAULT,
                "launch_interactive",
                return_value=123,
            ) as launch,
        ):
            result = CREDENTIAL_VAULT.command_add(arguments)

        ensure.assert_called_once_with(
            self.cli,
            self.vault,
            "APIs/Telegram/rodriclone",
            "senha-mestra",
        )
        self.assertEqual("edit", launch.call_args.args[1][0])
        self.assertEqual("update", result["operation"])

    def test_group_preparation_creates_every_missing_parent(self) -> None:
        missing = CREDENTIAL_VAULT.subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr=""
        )
        created = CREDENTIAL_VAULT.subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        with patch.object(
            CREDENTIAL_VAULT,
            "run_keepassxc",
            side_effect=[missing, created, missing, created],
        ) as run:
            CREDENTIAL_VAULT._ensure_vault_groups(
                self.cli,
                self.vault,
                "APIs/Telegram/rodriclone",
                "senha-mestra",
            )

        arguments = [call.args[1] for call in run.call_args_list]
        self.assertEqual(
            [
                ["ls", "--quiet", str(self.vault), "APIs"],
                ["mkdir", "--quiet", str(self.vault), "APIs"],
                ["ls", "--quiet", str(self.vault), "APIs/Telegram"],
                ["mkdir", "--quiet", str(self.vault), "APIs/Telegram"],
            ],
            arguments,
        )

    def test_add_requires_local_enrollment_before_opening_window(self) -> None:
        self.vault.touch()
        arguments = self.arguments()
        arguments.entry = "APIs/Telegram/rodriclone"
        arguments.username = None
        arguments.url = None
        with (
            patch.object(
                CREDENTIAL_VAULT,
                "windows_credential_exists",
                return_value=False,
            ),
            patch.object(CREDENTIAL_VAULT, "launch_interactive") as launch,
            self.assertRaises(CREDENTIAL_VAULT.VaultToolError),
        ):
            CREDENTIAL_VAULT.command_add(arguments)

        launch.assert_not_called()

    def test_unenroll_requires_confirmation(self) -> None:
        """A senha cadastrada não pode ser removida acidentalmente."""
        arguments = self.arguments()
        arguments.confirm = False
        with self.assertRaises(CREDENTIAL_VAULT.VaultToolError):
            CREDENTIAL_VAULT.command_unenroll(arguments)


if __name__ == "__main__":
    unittest.main()
