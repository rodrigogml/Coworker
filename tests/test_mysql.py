from __future__ import annotations

import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[1] / "instance"
SPEC = importlib.util.spec_from_file_location("mysql_config_tested", ROOT / "scripts" / "integration_config.py")
module = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(module)


class MySQLConfigTests(unittest.TestCase):
    def root(self, temporary: str) -> Path:
        root = Path(temporary)
        (root / "config").mkdir()
        (root / "config" / "mysql.example.toml").write_text(
            'enabled = false\nmysql_executable = ""\ndefault_profile = "example"\n\n'
            '[profiles.example]\nhost = "127.0.0.1"\nport = 3306\ncredential_ref = "DB/Example"\n',
            encoding="utf-8",
        )
        module.initialize_integration("mysql", project_root=root)
        return root

    def test_profiles_require_enabled_skill_and_executable(self):
        with TemporaryDirectory() as temporary:
            root = self.root(temporary)
            with self.assertRaises(module.IntegrationConfigError):
                module.add_profile("mysql", "local", "localhost", 3306, "DB/Local", project_root=root)
            module.configure_mysql(enabled=True, executable="mysql.exe", project_root=root)
            result = module.add_profile(
                "mysql", "local", "localhost", 3306, "DB/Local",
                database="app", credential_mode="password", project_root=root,
            )
            self.assertTrue(result["created"])
            profile = next(item for item in module.list_profiles("mysql", project_root=root)["profiles"] if item["name"] == "local")
            self.assertEqual("app", profile["database"])

    def test_enabled_requires_executable(self):
        with TemporaryDirectory() as temporary:
            root = self.root(temporary)
            with self.assertRaises(module.IntegrationConfigError):
                module.configure_mysql(enabled=True, executable="", project_root=root)


if __name__ == "__main__":
    unittest.main()
