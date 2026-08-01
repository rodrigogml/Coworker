"""Testes do contrato de identidade e da instalação de instâncias."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from interfaces.telegram.identity import IdentityConfigError, load_identity
from scripts import install_instance


class IdentityTests(unittest.TestCase):
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
    def test_generated_telegram_config_starts_with_minimal_sandbox(self) -> None:
        content = install_instance._telegram_content("assistente-teste")

        self.assertIn('credential_ref = "APIs/Telegram/assistente-teste"', content)
        self.assertIn('approval_policy = "never"', content)
        self.assertIn("network_access = false", content)
        self.assertIn('writable_directories = ["data"]', content)

    def test_generated_vault_config_uses_functional_filename_and_instance_target(self) -> None:
        content = install_instance._secrets_content("assistente-teste")

        self.assertIn("[executables]", content)
        self.assertIn("[vault]", content)
        self.assertIn('path = "data/secrets/vault.kdbx"', content)
        self.assertIn(
            'target = "Coworker/Instances/assistente-teste/KeePassXC/MasterPassword"',
            content,
        )

    def test_write_new_never_overwrites_existing_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            self.assertTrue(install_instance._write_new(path, "primeiro\n"))
            self.assertFalse(install_instance._write_new(path, "segundo\n"))
            self.assertEqual("primeiro\n", path.read_text(encoding="utf-8"))

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


if __name__ == "__main__":
    unittest.main()
