import tempfile
import unittest
import io
from pathlib import Path
from unittest.mock import patch

from scripts import instructions_config, workspace


class WorkspaceWriterTests(unittest.TestCase):
    def test_write_accepts_utf16_stdin_from_windows_powershell(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stdin = io.TextIOWrapper(io.BytesIO('{"ação": "ok"}'.encode("utf-16le")))
            with patch.object(workspace, "PROJECT_ROOT", root), patch.object(
                workspace, "WORK_ROOT", root / "data" / "work"
            ), patch.object(workspace.sys, "stdin", stdin):
                self.assertEqual(
                    0, workspace.main(["write", "--path", "data/work/input.json", "--content-stdin"])
                )
            self.assertEqual(
                '{"ação": "ok"}',
                (root / "data" / "work" / "input.json").read_text(encoding="utf-8"),
            )
    def test_write_creates_nested_utf8_file_inside_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(workspace, "PROJECT_ROOT", root), patch.object(
                workspace, "WORK_ROOT", root / "data" / "work"
            ):
                result = workspace.write_text(
                    "data/work/documents/formularios/README.md", "Catálogo\n"
                )
            self.assertTrue(result["ok"])
            self.assertEqual(
                "Catálogo\n",
                (root / "data" / "work" / "documents" / "formularios" / "README.md").read_text(
                    encoding="utf-8"
                ),
            )

    def test_write_rejects_protected_and_absolute_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(workspace, "PROJECT_ROOT", root), patch.object(
                workspace, "WORK_ROOT", root / "data" / "work"
            ):
                with self.assertRaises(workspace.WorkspaceError):
                    workspace.write_text("data/config/INSTRUCTIONS.md", "x")
                with self.assertRaises(workspace.WorkspaceError):
                    workspace.write_text(str(root / "data" / "work" / "x.md"), "x")

    def test_instructions_replace_file_accepts_only_workspace_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            work = root / "data" / "work"
            work.mkdir(parents=True)
            source = work / "instructions-update.md"
            source.write_text("# Atualização\n", encoding="utf-8")
            target = root / "data" / "config" / "INSTRUCTIONS.md"
            with patch.object(instructions_config, "PROJECT_ROOT", root), patch.object(
                instructions_config, "WORK_ROOT", work
            ), patch.object(instructions_config, "TARGET", target):
                self.assertEqual(
                    17,
                    instructions_config.replace_from_workspace(
                        "data/work/instructions-update.md"
                    ),
                )
            self.assertEqual("# Atualização\n", target.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
