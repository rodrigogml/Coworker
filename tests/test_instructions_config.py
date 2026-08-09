import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE = Path(__file__).resolve().parents[1] / "instance" / "scripts" / "instructions_config.py"
SPEC = importlib.util.spec_from_file_location("instructions_config", MODULE)
assert SPEC and SPEC.loader
instructions_config = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(instructions_config)


class InstructionsConfigTests(unittest.TestCase):
    def test_replaces_only_fixed_private_file(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "data" / "config" / "INSTRUCTIONS.md"
            with patch.object(instructions_config, "TARGET", target):
                with patch("sys.stdin") as stdin:
                    stdin.buffer.read.return_value = b"# Operacao\n"
                    self.assertEqual(instructions_config.replace_from_stdin(), 11)
            self.assertEqual(target.read_text(encoding="utf-8"), "# Operacao\n")

    def test_rejects_empty_content(self):
        with patch("sys.stdin") as stdin:
            stdin.buffer.read.return_value = b" \n"
            with self.assertRaises(ValueError):
                instructions_config.replace_from_stdin()


if __name__ == "__main__":
    unittest.main()
