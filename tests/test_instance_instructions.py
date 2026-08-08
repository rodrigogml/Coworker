import tempfile
import unittest
from pathlib import Path

from interfaces.telegram.instructions import (
    InstanceInstructionsError,
    instruction_block,
    load_instance_instructions,
)


class InstanceInstructionsTests(unittest.TestCase):
    def test_missing_file_is_optional(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual("", load_instance_instructions(root))
            self.assertEqual("", instruction_block(root))

    def test_loads_utf8_from_private_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "data" / "config" / "INSTRUCTIONS.md"
            path.parent.mkdir(parents=True)
            path.write_text("Preferir respostas objetivas.", encoding="utf-8")
            self.assertIn("Preferir respostas objetivas.", instruction_block(root))

    def test_rejects_oversized_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "data" / "config" / "INSTRUCTIONS.md"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"x" * (32 * 1024 + 1))
            with self.assertRaises(InstanceInstructionsError):
                load_instance_instructions(root)


if __name__ == "__main__":
    unittest.main()
