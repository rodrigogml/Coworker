"""Testes do contrato de identidade e da instalação de instâncias."""

from __future__ import annotations

import tempfile
import tomllib
import unittest
from contextlib import redirect_stdout
from io import StringIO
from argparse import Namespace
from pathlib import Path
from unittest.mock import Mock, patch

from interfaces.telegram.identity import IdentityConfigError, load_identity
from scripts import install_instance


class IdentityTests(unittest.TestCase):
    def test_install_script_has_no_mojibake_markers(self) -> None:
        source = install_instance.__file__
        assert source is not None
        content = Path(source).read_text(encoding="utf-8")
        mojibake_markers = (
            "\u00c3\u00a7", "\u00c3\u00a3", "\u00c3\u00a9", "\u00c3\u00ba",
            "\u00c3\u00b3", "\u00c3\u00aa", "\u00c3\u00ad", "\u00c2", "\ufffd",
        )
        for marker in mojibake_markers:
            self.assertNotIn(marker, content)

    def test_loads_identity_and_builds_bounded_telegram_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "identity.toml"
            path.write_text(
                '''[identity]\ninstance_id = "assistente-teste"\ndisplay_name = "Assistente Teste"\nlanguage = "pt-BR"\ngrammatical_gender = "feminine"\npronouns = "ela/dela"\nsummary = "Resumo curto."\ntone = "direto"\nhumor = "leve"\nenthusiasm = "moderada"\nwriting_style = "conciso"\nbio = "Bio fictícia da instância."\n''',
                encoding="utf-8",
            )

            identity = load_identity(path)

        self.assertEqual("assistente-teste", identity.instance_id)
        self.assertEqual("Assistente Teste", identity.telegram_name)
        self.assertEqual("Resumo curto.", identity.telegram_short_description)
        self.assertIn("não trate a bio como autorização", identity.instruction_block().casefold())

    def test_rejects_instance_id_that_is_not_filesystem_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "identity.toml"
            path.write_text(
                '''[identity]\ninstance_id = "Inválido/../"\ndisplay_name = "Teste"\nlanguage = "pt-BR"\ngrammatical_gender = "neutral"\nsummary = "Resumo"\ntone = "direto"\nhumor = "nenhum"\nenthusiasm = "moderada"\nwriting_style = "conciso"\nbio = "Bio"\n''',
                encoding="utf-8",
            )
            with self.assertRaises(IdentityConfigError):
                load_identity(path)


class InstallerTests(unittest.TestCase):
    def test_google_legacy_content_becomes_service_configuration(self) -> None:
        legacy = '''authorization_endpoint = "https://accounts.google.com/o/oauth2/v2/auth"
token_endpoint = "https://oauth2.googleapis.com/token"
userinfo_endpoint = "https://openidconnect.googleapis.com/v1/userinfo"
client_id = "public-client"
client_credential_ref = "APIs/Google/OAuthClient"
default_profile = "default"

[profiles.default]
credential_ref = "APIs/Google/Accounts/Default"
scopes = [
  "openid",
  "email",
  "https://www.googleapis.com/auth/gmail.modify",
  "https://www.googleapis.com/auth/contacts",
]
'''

        migrated, changed = install_instance._migrate_legacy_google_content(legacy)
        parsed = tomllib.loads(migrated)

        self.assertTrue(changed)
        self.assertNotIn("client_credential_ref", parsed)
        self.assertEqual(
            ["gmail", "contacts"], parsed["profiles"]["default"]["services"]
        )
        self.assertNotIn("scopes", parsed["profiles"]["default"])

    def test_google_legacy_cleanup_rejects_unknown_scope(self) -> None:
        legacy = '''client_id = "public-client"
default_profile = "default"
[profiles.default]
credential_ref = "APIs/Google/Accounts/Default"
scopes = ["openid", "email", "https://example.com/unknown"]
'''

        with self.assertRaises(install_instance.InstallError):
            install_instance._migrate_legacy_google_content(legacy)

    def test_google_legacy_profile_without_account_starts_without_services(self) -> None:
        legacy = '''client_id = "public-client"
default_profile = "default"
[profiles.default]
credential_ref = "APIs/Google/Accounts/Default"
scopes = ["openid", "email", "https://www.googleapis.com/auth/gmail.modify"]
'''

        migrated, changed = install_instance._migrate_legacy_google_content(
            legacy,
            unauthorized_profiles={"default"},
        )

        self.assertTrue(changed)
        self.assertEqual([], tomllib.loads(migrated)["profiles"]["default"]["services"])

    def test_google_cleanup_removes_only_canonical_legacy_entry(self) -> None:
        legacy = '''client_id = "public-client"
client_credential_ref = "APIs/Google/OAuthClient"
default_profile = "default"
[profiles.default]
credential_ref = "APIs/Google/Accounts/Default"
scopes = ["openid", "email", "https://www.googleapis.com/auth/gmail.modify"]
'''
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "google.toml"
            config.write_text(legacy, encoding="utf-8")
            with (
                patch.object(install_instance, "GOOGLE_CONFIG", config),
                patch.object(install_instance, "_vault_operational", return_value=True),
                patch.object(
                    install_instance,
                    "_run_json",
                    return_value={"ok": True, "entry_exists": True},
                ),
                patch.object(install_instance, "_yes_no", return_value=True),
                patch("credential_vault.remove_entry") as remove_entry,
                patch("builtins.print"),
            ):
                result = install_instance.cleanup_legacy_google("instance-test")

            parsed = tomllib.loads(config.read_text(encoding="utf-8"))

        remove_entry.assert_called_once_with("APIs/Google/OAuthClient")
        self.assertTrue(result["entry_removed"])
        self.assertEqual(["gmail"], parsed["profiles"]["default"]["services"])

    def test_instance_contract_documents_runtime_and_git_path_bases(self) -> None:
        contract = (install_instance.PROJECT_ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("instance/data/work/", contract)
        self.assertIn("instructions_config.py replace", contract)

    def test_generated_telegram_config_starts_with_minimal_sandbox(self) -> None:
        content = install_instance._telegram_content("assistente-teste")

        self.assertIn('credential_ref = "APIs/Telegram/assistente-teste"', content)
        self.assertIn('access_mode = "restricted"', content)
        self.assertIn('approval_policy = "never"', content)
        self.assertIn("network_access = false", content)
        self.assertIn('writable_directories = ["data/work"]', content)
        parsed = tomllib.loads(content)
        self.assertTrue(parsed["codex"]["home_dir"])

    def test_legacy_absolute_workspace_path_is_made_portable(self) -> None:
        legacy = install_instance.PROJECT_ROOT.parent / "old-instance" / "data" / "work" / "scripts"
        content = install_instance._telegram_content(
            "assistente-teste", {"writable_directories": [str(legacy)]}
        )
        parsed = tomllib.loads(content)
        self.assertEqual(["data/work/scripts"], parsed["codex"]["writable_directories"])

    def test_invalid_workspace_path_falls_back_to_restricted_default(self) -> None:
        content = install_instance._telegram_content(
            "assistente-teste", {"writable_directories": ["C:/fora-da-instancia"]}
        )
        parsed = tomllib.loads(content)
        self.assertEqual(["data/work"], parsed["codex"]["writable_directories"])

    def test_directory_entries_accept_commas_quotes_and_legacy_semicolons(self) -> None:
        self.assertEqual(
            [".", "C:\\repo", "C:\\path,with,commas"],
            install_instance._parse_directory_entries(
                '., C:\\repo, "C:\\path,with,commas"'
            ),
        )
        self.assertEqual(
            ["data", "C:\\repo"],
            install_instance._parse_directory_entries("data;C:\\repo"),
        )

    def test_directory_manager_adds_and_removes_individually(self) -> None:
        with patch(
            "builtins.input", side_effect=["1", "alpha,beta", "2", "1", "0"]
        ):
            result = install_instance._manage_read_directories("Leitura", [])
        self.assertEqual(["beta"], result)

    def test_writable_directory_manager_can_restore_workspace_profile(self) -> None:
        with patch("builtins.input", side_effect=["3", "s", "0"]):
            result = install_instance._manage_writable_directories(
                "Escrita", ["data", "."]
            )
        self.assertEqual(["data/work"], result)

    def test_codex_update_preserves_unrelated_telegram_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "telegram.toml"
            content = install_instance._telegram_content("assistente-teste")
            content = content.replace('listen_port = 8787', 'listen_port = 9999')
            config.write_text(content, encoding="utf-8")
            values = install_instance._default_telegram_values("assistente-teste")
            values["sandbox"] = "read-only"
            with patch.object(install_instance, "TELEGRAM_CONFIG", config):
                install_instance._save_codex_values("assistente-teste", values)

            parsed = tomllib.loads(config.read_text(encoding="utf-8"))
            self.assertEqual("read-only", parsed["codex"]["sandbox"])
            self.assertEqual(9999, parsed["webhook"]["listen_port"])

    def test_generated_vault_config_uses_functional_filename_and_instance_target(self) -> None:
        content = install_instance._secrets_content("assistente-teste")

        self.assertIn("[executables]", content)
        self.assertIn("[vault]", content)
        self.assertIn('path = "data/secrets/vault.kdbx"', content)
        self.assertIn(
            'target = "Coworker/Instances/assistente-teste/KeePassXC/MasterPassword"',
            content,
        )

    def test_existing_identity_can_be_reviewed_and_changed_one_field_at_a_time(
        self,
    ) -> None:
        values = {
            key: f"valor-{key}" for key, _label in install_instance.IDENTITY_FIELDS
        }
        values["instance_id"] = "assistente-teste"
        values["grammatical_gender"] = "neutral"

        with (
            patch("builtins.input", side_effect=["2", "Novo Nome", ""]),
            patch("builtins.print"),
        ):
            updated = install_instance.edit_identity(values)

        self.assertEqual("Novo Nome", updated["display_name"])
        self.assertEqual("assistente-teste", updated["instance_id"])

    def test_missing_keepassxc_can_remain_pending_without_aborting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "secrets.toml"
            with (
                patch.object(install_instance, "SECRETS_CONFIG", config),
                patch.object(
                    install_instance, "_known_executable_paths", return_value=()
                ),
                patch("builtins.input", side_effect=["", ""]),
                patch("builtins.print"),
            ):
                created, ready = install_instance.configure_vault_executables(
                    "assistente-teste", non_interactive=False
                )

            self.assertTrue(created)
            self.assertFalse(ready)
            self.assertIn('gui = ""', config.read_text(encoding="utf-8"))
            self.assertIn('cli = ""', config.read_text(encoding="utf-8"))

    def test_detects_keepassxc_siblings_from_existing_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            gui = root / "KeePassXC.exe"
            cli = root / "keepassxc-cli.exe"
            gui.touch()
            cli.touch()
            config = root / "secrets.toml"
            config.write_text(
                install_instance._secrets_content(
                    "assistente-teste", gui=str(gui), cli=""
                ),
                encoding="utf-8",
            )
            with (
                patch.object(install_instance, "SECRETS_CONFIG", config),
                patch.object(
                    install_instance, "_known_executable_paths", return_value=()
                ),
            ):
                created, ready = install_instance.configure_vault_executables(
                    "assistente-teste", non_interactive=True
                )

            self.assertFalse(created)
            self.assertTrue(ready)
            content = config.read_text(encoding="utf-8")
            self.assertIn(str(gui).replace("\\", "\\\\"), content)
            self.assertIn(str(cli).replace("\\", "\\\\"), content)

    def test_write_new_never_overwrites_existing_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            self.assertTrue(install_instance._write_new(path, "primeiro\n"))
            self.assertFalse(install_instance._write_new(path, "segundo\n"))
            self.assertEqual("primeiro\n", path.read_text(encoding="utf-8"))

    def test_main_menu_opens_validation_directly(self) -> None:
        identity = {
            key: f"valor-{key}" for key, _label in install_instance.IDENTITY_FIELDS
        }
        identity["instance_id"] = "assistente-teste"
        identity["display_name"] = "Assistente Teste"
        identity["grammatical_gender"] = "neutral"
        complete = [
            {"status": "OK", "component": "Teste", "detail": "pronto", "cause": ""}
        ]
        with (
            patch("builtins.input", side_effect=["0", "9"]),
            patch.object(
                install_instance, "print_validation_report", return_value=complete
            ) as report,
            patch.object(install_instance, "validate_installation", return_value=complete),
            patch("builtins.print"),
        ):
            result = install_instance.run_configurator(
                Namespace(skip_telegram=False, no_start=True), identity
            )

        report.assert_called_once_with("assistente-teste")
        self.assertTrue(result["configuration_complete"])

    def test_codex_status_explains_when_required_cli_is_missing(self) -> None:
        values = install_instance._default_telegram_values("assistente-teste")
        values["executable"] = ""
        with (
            patch.object(install_instance, "_discover_codex", return_value=None),
            patch.object(
                install_instance,
                "_default_codex_home",
                return_value=Path("C:/isolado/codex"),
            ),
        ):
            status = install_instance._codex_status(values)

        self.assertIsNone(status["executable"])
        self.assertFalse(status["authenticated"])
        self.assertIn("não localizado", status["login_detail"])

    def test_managed_gateway_start_waits_for_runtime_registration(self) -> None:
        process = Mock(pid=321)
        process.poll.return_value = None
        with (
            patch.object(
                install_instance,
                "gateway_runtime_status",
                side_effect=[
                    {"running": False},
                    {"running": True, "pid": 321},
                ],
            ),
            patch.object(install_instance, "_gateway_process", return_value=process),
            patch.object(install_instance.time, "sleep"),
        ):
            result = install_instance.start_gateway("assistente-teste")

        self.assertTrue(result["started"])
        self.assertEqual(321, result["pid"])

    def test_managed_gateway_start_surfaces_last_logged_error(self) -> None:
        process = Mock(pid=321)
        process.poll.return_value = 1
        with (
            patch.object(
                install_instance,
                "gateway_runtime_status",
                return_value={"running": False},
            ),
            patch.object(install_instance, "_gateway_process", return_value=process),
            patch.object(
                install_instance,
                "_gateway_failure_detail",
                return_value="Último erro: Telegram recusou setMyName (429).",
            ),
        ):
            with self.assertRaisesRegex(
                install_instance.InstallError, "setMyName.*429"
            ):
                install_instance.start_gateway("assistente-teste")

    def test_gateway_service_start_uses_service_without_spawning_process(self) -> None:
        service = {"installed": True, "state_name": "stopped", "service_name": "assistente-teste"}
        with (
            patch.object(install_instance, "gateway_service_status", return_value=service),
            patch.object(install_instance, "gateway_runtime_status", return_value={"running": False}),
            patch.object(
                install_instance,
                "windows_service_action",
                return_value={"ok": True, "state_name": "running"},
            ) as action,
        ):
            result = install_instance.start_gateway("assistente-teste", mode="service")

        action.assert_called_once_with(
            "assistente-teste", "start", service_name="assistente-teste"
        )
        self.assertEqual("running", result["state_name"])

    def test_gateway_service_error_is_converted_to_install_error(self) -> None:
        from scripts.windows_service import WindowsServiceError

        with patch(
            "scripts.windows_service.control_service",
            side_effect=WindowsServiceError("erro 1053"),
        ):
            with self.assertRaisesRegex(install_instance.InstallError, "1053"):
                install_instance.windows_service_action(
                    "assistente-teste", "start", service_name="assistente-teste"
                )

    def test_gateway_menu_keeps_running_after_service_failure(self) -> None:
        output = StringIO()
        with (
            patch.object(install_instance, "gateway_runtime_status", return_value={"running": False, "pid": None}),
            patch.object(
                install_instance,
                "gateway_service_status",
                return_value={"installed": True, "state_name": "stopped", "service_name": "lavelinha"},
            ),
            patch.object(
                install_instance,
                "start_gateway",
                side_effect=install_instance.InstallError("erro 1053"),
            ),
            patch("builtins.input", side_effect=["2", "service", "0"]),
            redirect_stdout(output),
        ):
            install_instance.manage_gateway("lavelinha")

        self.assertIn("Não foi possível iniciar o gateway", output.getvalue())
        self.assertIn("erro 1053", output.getvalue())

    def test_process_start_is_blocked_when_service_is_running(self) -> None:
        service = {"installed": True, "state_name": "running", "service_name": "assistente-teste"}
        with (
            patch.object(install_instance, "gateway_service_status", return_value=service),
            patch.object(install_instance, "gateway_runtime_status", return_value={"running": False}),
            self.assertRaisesRegex(install_instance.InstallError, "serviço"),
        ):
            install_instance.start_gateway("assistente-teste", mode="process")

    def test_gateway_service_stop_uses_service_control(self) -> None:
        service = {"installed": True, "state_name": "running", "service_name": "assistente-teste"}
        with (
            patch.object(install_instance, "gateway_service_status", return_value=service),
            patch.object(
                install_instance,
                "windows_service_action",
                return_value={"ok": True, "state_name": "stopped"},
            ) as action,
        ):
            result = install_instance.stop_gateway("assistente-teste", mode="service")

        action.assert_called_once_with(
            "assistente-teste", "stop", service_name="assistente-teste"
        )
        self.assertEqual("stopped", result["state_name"])

    def test_managed_gateway_stop_uses_cooperative_request(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(
                install_instance,
                "_gateway_state_dir",
                return_value=Path(temporary),
            ),
            patch(
                "interfaces.telegram.runtime.request_stop",
                return_value={"running": True, "pid": 321, "requested": True},
            ),
            patch(
                "interfaces.telegram.runtime.runtime_status",
                return_value={"running": False, "pid": None},
            ),
        ):
            result = install_instance.stop_gateway(
                "assistente-teste", timeout_seconds=1
            )

        self.assertTrue(result["stopped"])

    def test_pairing_waits_for_request_and_requires_local_id_confirmation(self) -> None:
        process = Mock()
        process.poll.return_value = None
        process.wait.return_value = 0
        gateway_results = [
            {"owner": None, "pending": None},
            {"pin": "123456"},
            {
                "pending": {
                    "approval_code": "ABC123",
                    "display_name": "Pessoa Teste",
                    "username": "pessoa_teste",
                    "user_id": 10,
                    "chat_id": 10,
                }
            },
            {"ok": True},
        ]
        with (
            patch.object(install_instance, "_gateway", side_effect=gateway_results) as gateway,
            patch.object(install_instance, "_gateway_process", return_value=process),
            patch.object(install_instance, "_yes_no", return_value=True),
            patch("builtins.print"),
        ):
            self.assertTrue(install_instance.pair_owner_interactively())

        self.assertEqual(("pairing", "approve", "ABC123"), gateway.call_args_list[-1].args)
        process.terminate.assert_called_once()

    def test_telegram_token_is_masked_and_written_directly_to_vault(self) -> None:
        with (
            patch.object(install_instance, "_yes_no", return_value=True),
            patch.object(
                install_instance.getpass,
                "getpass",
                return_value="token-secreto",
            ),
            patch("scripts.credential_vault.write_entry_secret") as write,
            patch.object(install_instance, "_gateway", return_value={"ok": True}),
            patch.object(
                install_instance,
                "pair_owner_interactively",
                return_value=True,
            ),
            patch("builtins.print") as output,
        ):
            result = install_instance.configure_telegram(
                "assistente-teste",
                non_interactive=False,
                should_start_gateway=False,
            )

        write.assert_called_once_with(
            "APIs/Telegram/assistente-teste", "token-secreto"
        )
        self.assertTrue(result["configured"])
        self.assertNotIn("token-secreto", str(output.call_args_list))

    def test_telegram_configuration_starts_managed_gateway_when_requested(self) -> None:
        with (
            patch.object(install_instance, "_yes_no", return_value=False),
            patch.object(
                install_instance,
                "pair_owner_interactively",
                return_value=True,
            ),
            patch.object(
                install_instance,
                "start_gateway",
                return_value={
                    "pid": 321,
                    "already_running": False,
                },
            ) as start,
            patch("builtins.print"),
        ):
            result = install_instance.configure_telegram(
                "assistente-teste",
                non_interactive=False,
                should_start_gateway=True,
            )

        start.assert_called_once_with("assistente-teste")
        self.assertTrue(result["paired"])
        self.assertEqual(321, result["process_id"])


if __name__ == "__main__":
    unittest.main()
