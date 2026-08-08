import tempfile
import unittest
from pathlib import Path

from scripts.windows_service import (
    WindowsServiceError,
    build_definition,
    validate_service_name,
)


class WindowsServiceContractTests(unittest.TestCase):
    def test_service_name_contract(self):
        self.assertEqual(validate_service_name("RodriClone"), "RodriClone")
        with self.assertRaises(WindowsServiceError):
            validate_service_name("nome com espaço")
        with self.assertRaises(WindowsServiceError):
            validate_service_name("!")

    def test_definition_uses_instance_name_and_configured_codex_home(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gateway = root / "interfaces" / "telegram"
            gateway.mkdir(parents=True)
            (gateway / "gateway.py").write_text("", encoding="utf-8")
            config = root / "data" / "config"
            config.mkdir(parents=True)
            (config / "telegram.toml").write_text(
                '[codex]\nhome_dir = "C:/isolated/RodriClone/codex"\n',
                encoding="utf-8",
            )
            definition = build_definition(
                root, instance_id="rodriclone", display_name="RodriClone"
            )
            self.assertEqual(definition.name, "rodriclone")
            self.assertEqual(definition.display_name, "RodriClone")
            self.assertEqual(str(definition.codex_home), "C:\\isolated\\RodriClone\\codex")
            self.assertIn("rodriclone", str(definition.gateway_state_dir).casefold())

    def test_invalid_startup_and_timeout_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gateway = root / "interfaces" / "telegram"
            gateway.mkdir(parents=True)
            (gateway / "gateway.py").write_text("", encoding="utf-8")
            with self.assertRaises(WindowsServiceError):
                build_definition(root, instance_id="abc", display_name="ABC", startup="bad")
            with self.assertRaises(WindowsServiceError):
                build_definition(root, instance_id="abc", display_name="ABC", stop_timeout_seconds=1)


if __name__ == "__main__":
    unittest.main()
