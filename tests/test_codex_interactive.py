import importlib.util
import sys
import unittest
from pathlib import Path

from interfaces.telegram.codex import CodexAdapter, ProcessRegistry
from interfaces.telegram.config import CodexConfig


MODULE = Path(__file__).resolve().parents[1] / "instance" / "scripts" / "codex_interactive.py"
SPEC = importlib.util.spec_from_file_location("codex_interactive", MODULE)
assert SPEC and SPEC.loader
codex_interactive = importlib.util.module_from_spec(SPEC)
sys.modules["codex_interactive"] = codex_interactive
SPEC.loader.exec_module(codex_interactive)


class CodexInteractiveTests(unittest.TestCase):
    def test_command_reuses_gateway_policy_and_options(self):
        config = CodexConfig(
            executable=Path("C:/opt/codex.exe"),
            home_dir=Path("C:/instance/data/codex"),
            sandbox="workspace-write",
            network_access=False,
            approval_policy="never",
            timeout_seconds=60,
            additional_directories=(Path("C:/shared"),),
            writable_directories=(Path("C:/instance/data/work"),),
            model="gpt-test",
            reasoning_effort="high",
            speed="fast",
            verbosity="high",
        )
        adapter = CodexAdapter(config, Path("C:/instance"), ProcessRegistry())

        command = codex_interactive.build_command(adapter)

        self.assertEqual(command[:3], [str(Path("C:/opt/codex.exe")), "--cd", str(Path("C:/instance"))])
        self.assertIn("permissions.coworker_gateway.network.enabled=false", command)
        self.assertIn('approval_policy="never"', command)
        self.assertIn('model="gpt-test"', command)
        self.assertIn('model_reasoning_effort="high"', command)
        self.assertIn('model_verbosity="high"', command)
        self.assertIn("features.fast_mode=true", command)
        self.assertIn(str(Path("C:/shared")), command)

    def test_parser_does_not_expose_configuration_overrides(self):
        self.assertEqual(vars(codex_interactive.build_parser().parse_args([])), {})
        with self.assertRaises(SystemExit):
            codex_interactive.build_parser().parse_args(["--config", "other.toml"])


if __name__ == "__main__":
    unittest.main()
