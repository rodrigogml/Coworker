from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


PROJECT_ROOT = Path(__file__).resolve().parents[1] / "instance"
MODULE_PATH = PROJECT_ROOT / "scripts" / "integration_config.py"
SPEC = importlib.util.spec_from_file_location("integration_config_tested", MODULE_PATH)
integration_config = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = integration_config
SPEC.loader.exec_module(integration_config)


class IntegrationConfigTests(unittest.TestCase):
    def test_cpfl_is_a_direct_skill_not_a_configured_integration(self) -> None:
        self.assertNotIn("cpfl", integration_config.INTEGRATIONS)
        with self.assertRaises(integration_config.IntegrationConfigError):
            integration_config.initialization_command("cpfl")

    def root(self, temporary: str, integration: str = "omie") -> Path:
        root = Path(temporary)
        (root / "config").mkdir()
        (root / "config" / f"{integration}.example.toml").write_text(
            'credential_ref = "APIs/Teste"\n',
            encoding="utf-8",
        )
        return root

    def test_init_creates_private_config_from_public_model(self):
        with TemporaryDirectory() as temporary:
            root = self.root(temporary)

            result = integration_config.initialize_integration(
                "omie", project_root=root
            )
            destination = root / "data" / "config" / "omie.toml"

            self.assertEqual("created", result["status"])
            self.assertEqual(
                'credential_ref = "APIs/Teste"\n',
                destination.read_text(encoding="utf-8"),
            )

    def test_repeated_init_never_overwrites_existing_config(self):
        with TemporaryDirectory() as temporary:
            root = self.root(temporary)
            integration_config.initialize_integration("omie", project_root=root)
            destination = root / "data" / "config" / "omie.toml"
            destination.write_text("personalizado\n", encoding="utf-8")

            result = integration_config.initialize_integration(
                "omie", project_root=root
            )

            self.assertEqual("already_exists", result["status"])
            self.assertEqual("personalizado\n", destination.read_text(encoding="utf-8"))

    def test_unknown_integration_is_rejected(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.mkdir(exist_ok=True)

            with self.assertRaises(integration_config.IntegrationConfigError):
                integration_config.initialize_integration(
                    "../../fora", project_root=root
                )

    def test_private_directory_cannot_escape_project(self):
        with TemporaryDirectory() as temporary, TemporaryDirectory() as outside:
            root = self.root(temporary)
            try:
                (root / "data").symlink_to(Path(outside), target_is_directory=True)
            except OSError:
                self.skipTest("Criação de symlink indisponível nesta instalação.")

            with self.assertRaises(integration_config.IntegrationConfigError):
                integration_config.initialize_integration("omie", project_root=root)

    def bis_root(self, temporary: str) -> Path:
        root = Path(temporary)
        (root / "config").mkdir()
        (root / "config" / "bis2.example.toml").write_text(
            'jar_path = "C:/BISCMD.jar"\n'
            'default_profile = "example"\n\n'
            '[profiles.example]\n'
            'host = "127.0.0.1"\n'
            'port = 8080\n'
            'credential_ref = "BIS2/Example/BISCMD"\n',
            encoding="utf-8",
        )
        integration_config.initialize_integration("bis2", project_root=root)
        return root

    def test_add_profile_is_typed_and_preserves_existing_profiles(self):
        with TemporaryDirectory() as temporary:
            root = self.bis_root(temporary)
            result = integration_config.add_profile(
                "bis2", "local", "127.0.0.1", 8080, "BIS2/Local/BISCMD", project_root=root
            )
            self.assertTrue(result["created"])
            profiles = integration_config.list_profiles("bis2", project_root=root)["profiles"]
            self.assertEqual({"example", "local"}, {item["name"] for item in profiles})
            text = (root / "data" / "config" / "bis2.toml").read_text(encoding="utf-8")
            self.assertIn('[profiles.local]', text)
            self.assertIn('credential_ref = "BIS2/Local/BISCMD"', text)

    def test_add_profile_rejects_duplicate_and_invalid_values(self):
        with TemporaryDirectory() as temporary:
            root = self.bis_root(temporary)
            with self.assertRaises(integration_config.IntegrationConfigError):
                integration_config.add_profile(
                    "bis2", "example", "127.0.0.1", 8080, "BIS2/Local/BISCMD", project_root=root
                )
            with self.assertRaises(integration_config.IntegrationConfigError):
                integration_config.add_profile(
                    "bis2", "bad name", "127.0.0.1", 8080, "BIS2/Local/BISCMD", project_root=root
                )
            with self.assertRaises(integration_config.IntegrationConfigError):
                integration_config.add_profile(
                    "bis2", "local", "127.0.0.1", 0, "BIS2/Local/BISCMD", project_root=root
                )

    def test_catalog_reports_commands_without_creating_files(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)

            result = integration_config.list_integrations(project_root=root)

            self.assertTrue(result["ok"])
            self.assertIn(
                "python scripts/integration_config.py init omie",
                [item["command"] for item in result["integrations"]],
            )
            self.assertFalse((root / "data").exists())

    def test_every_catalog_entry_has_a_public_model(self):
        for filename in integration_config.INTEGRATIONS.values():
            with self.subTest(integration=filename):
                self.assertTrue(
                    (PROJECT_ROOT / "config" / f"{filename}.example.toml").is_file()
                )

    def test_ssh_profile_set_updates_existing_typed_profile(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config").mkdir()
            (root / "config" / "ssh.example.toml").write_text(
                'default_profile = "turing"\n\n[profiles.turing]\n'
                'host = ""\nport = 22\ncredential_ref = ""\nattachment_name = ""\n',
                encoding="utf-8",
            )
            integration_config.initialize_integration("ssh", project_root=root)
            result = integration_config.set_profile(
                "ssh", "turing", "192.168.3.64", 22,
                "Infraestrutura/Turing/SSH", "id_ed25519", project_root=root,
            )
            self.assertFalse(result["created"])
            values = integration_config.list_profiles("ssh", project_root=root)
            self.assertEqual("192.168.3.64", values["profiles"][0]["host"])
            self.assertEqual("Infraestrutura/Turing/SSH", values["profiles"][0]["credential_ref"])
            self.assertEqual("id_ed25519", values["profiles"][0]["attachment_name"])

    def test_ssh_profile_rejects_path_in_attachment_name(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config").mkdir()
            (root / "config" / "ssh.example.toml").write_text(
                'default_profile = "turing"\n', encoding="utf-8"
            )
            integration_config.initialize_integration("ssh", project_root=root)
            with self.assertRaises(integration_config.IntegrationConfigError):
                integration_config.set_profile(
                    "ssh", "turing", "127.0.0.1", 22, "APIs/SSH", "..\\key",
                    project_root=root,
                )


if __name__ == "__main__":
    unittest.main()
