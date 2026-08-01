from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PROJECT_ROOT / "scripts" / "integration_config.py"
SPEC = importlib.util.spec_from_file_location("integration_config_tested", MODULE_PATH)
integration_config = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = integration_config
SPEC.loader.exec_module(integration_config)


class IntegrationConfigTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
