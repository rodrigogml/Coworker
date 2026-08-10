import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE = Path(__file__).resolve().parents[1] / "instance" / "skills" / "bis10" / "scripts" / "bis10.py"
SPEC = importlib.util.spec_from_file_location("bis10_tool", MODULE)
assert SPEC and SPEC.loader
bis10 = importlib.util.module_from_spec(SPEC)
sys.modules["bis10_tool"] = bis10
SPEC.loader.exec_module(bis10)


class Bis10ToolTests(unittest.TestCase):
    def test_account_create_maps_to_documented_command(self):
        args = bis10.build_parser().parse_args([
            "--profile", "local", "account-create", "--account-id", "1",
            "--category-id", "10", "--date", "2026-08-08", "--value", "125.30",
            "--display-line", "Despesa", "--confirm",
        ])
        command, mutating = bis10.build_arguments(args)
        self.assertTrue(mutating)
        self.assertEqual(command, [
            "-connect", "-accountStatement", "create", "accountId", "1",
            "categoryId", "10", "date", "2026-08-08", "value", "125.30",
            "displayLine", "Despesa", "audited", "false", "confirm",
        ])

    def test_transfer_update_is_mutating(self):
        args = bis10.build_parser().parse_args([
            "--profile", "local", "transfer-update", "--id", "200",
            "--value", "550.00", "--confirm",
        ])
        command, mutating = bis10.build_arguments(args)
        self.assertTrue(mutating)
        self.assertEqual(command, [
            "-connect", "-accountStatement", "updateTransfer", "id", "200",
            "value", "550.00", "confirm",
        ])

    def test_relative_client_paths_resolve_against_project(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "bis10.toml"
            config.write_text(
                'default_profile = "local"\n'
                '[profiles.local]\n'
                'host = "127.0.0.1"\nport = 8080\n'
                'jar_path = "client/BISCMD-10.0.jar"\n'
                'working_dir = "client"\nlocale = "pt-BR"\n'
                'jndi_credential_ref = "BIS10/Local/ApplicationRealm"\n'
                'bis_credential_ref = "BIS10/Local/BIS10"\n', encoding="utf-8"
            )
            profile = bis10.load_config(config).profiles["local"]
            self.assertTrue(profile.jar_path.is_absolute())

    def test_run_injects_two_credential_pairs_without_command_line_secrets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            jar = root / "BISCMD-10.0.jar"; jar.write_bytes(b"jar")
            profile = bis10.Bis10Profile(
                "local", "127.0.0.1", 8080, jar, root, "pt-BR",
                "BIS10/Local/ApplicationRealm", "BIS10/Local/BIS10",
            )
            config = bis10.Bis10Config("", 10, "local", {"local": profile})
            completed = type("Completed", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()
            captured = {}
            def fake_run(command, **kwargs):
                captured["command"] = command
                captured["environment"] = dict(kwargs["env"])
                return completed
            with patch.object(bis10, "_java", return_value="java"), \
                 patch.object(bis10, "read_entry_credentials", side_effect=[("jndi", "jndi-secret"), ("bis", "bis-secret")]), \
                 patch.object(bis10.subprocess, "run", side_effect=fake_run):
                result = bis10.run_bis10cmd(config, profile, ["-connect", "-ping"])
            command = captured["command"]
            environment = captured["environment"]
            self.assertNotIn("jndi-secret", command)
            self.assertNotIn("bis-secret", command)
            self.assertEqual(environment["BISCMD_JNDI_PASSWORD"], "jndi-secret")
            self.assertEqual(environment["BISCMD_BIS_PASSWORD"], "bis-secret")
            self.assertTrue(result["ok"])


if __name__ == "__main__":
    unittest.main()
