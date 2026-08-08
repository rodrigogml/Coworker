from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


PROJECT_ROOT = Path(__file__).resolve().parents[1] / "instance"
MODULE_PATH = PROJECT_ROOT / "skills" / "drive" / "scripts" / "drive.py"
SPEC = importlib.util.spec_from_file_location("drive_skill", MODULE_PATH)
drive = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = drive
SPEC.loader.exec_module(drive)


class DriveTests(unittest.TestCase):
    def config(self):
        common = drive.GoogleServiceConfig(
            "https://www.googleapis.com/drive/v3",
            PROJECT_ROOT / "data" / "config" / "google.toml",
            30,
            100,
            20,
            5_242_880,
            {
                "upload_base": "https://www.googleapis.com/upload/drive/v3",
                "max_download_bytes": 104_857_600,
            },
        )
        return drive.DriveConfig(
            common,
            "https://www.googleapis.com/upload/drive/v3",
            104_857_600,
            104_857_600,
        )

    def test_load_config(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "drive.toml"
            path.write_text(
                'api_base = "https://www.googleapis.com/drive/v3"\n'
                'upload_base = "https://www.googleapis.com/upload/drive/v3"\n'
                'google_config = "data/config/google.toml"\n'
                "timeout_seconds = 20\npage_size = 100\nmax_pages = 5\n"
                "max_response_bytes = 1000000\nmax_download_bytes = 2000000\n",
                encoding="utf-8",
            )
            config = drive.load_config(path)
        self.assertEqual(2_000_000, config.max_download_bytes)
        self.assertEqual(104_857_600, config.max_upload_bytes)

    def test_upload_dry_run_hides_content(self):
        with TemporaryDirectory() as temporary:
            source = Path(temporary) / "private.txt"
            source.write_text("conteúdo confidencial", encoding="utf-8")
            args = argparse.Namespace(
                source=str(source),
                name=None,
                mime_type=None,
                parent_id=None,
                dry_run=True,
            )
            result = drive.file_upload(None, self.config(), args)
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("conteúdo confidencial", serialized)
        self.assertEqual("private.txt", result["upload"]["name"])

    def test_permission_create_disables_notification_by_default(self):
        args = argparse.Namespace(
            file_id="file123",
            type="user",
            role="reader",
            email="destino@example.com",
            domain=None,
            notify=False,
            dry_run=True,
        )
        result = drive.permission_create(None, self.config(), args)
        self.assertEqual("false", result["request"]["query"]["sendNotificationEmail"])

    def test_parser_has_no_permanent_file_delete(self):
        parser = drive.build_parser()
        help_text = parser.format_help()
        self.assertNotIn("--token", help_text)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(["files", "delete", "--id", "abc"])

    def test_copy_dry_run_uses_closed_endpoint(self):
        args = argparse.Namespace(
            id="file123",
            name="Cópia",
            parent_id="folder123",
            dry_run=True,
        )
        result = drive.file_copy(None, self.config(), args)
        self.assertEqual("/files/file123/copy", result["request"]["path"])
        self.assertEqual(["folder123"], result["request"]["payload"]["parents"])

    def test_replace_dry_run_hides_file_content(self):
        with TemporaryDirectory() as temporary:
            source = Path(temporary) / "replacement.txt"
            source.write_text("conteúdo que não deve sair", encoding="utf-8")
            args = argparse.Namespace(
                id="file123",
                source=str(source),
                name=None,
                mime_type=None,
                dry_run=True,
            )
            result = drive.file_replace(None, self.config(), args)
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("conteúdo que não deve sair", serialized)
        self.assertEqual("file123", result["replace"]["id"])

    def test_permission_update_dry_run_is_closed_operation(self):
        args = argparse.Namespace(
            file_id="file123",
            permission_id="permission123",
            role="writer",
            dry_run=True,
        )
        result = drive.permission_update(None, self.config(), args)
        self.assertEqual("PATCH", result["request"]["method"])
        self.assertEqual(
            "/files/file123/permissions/permission123",
            result["request"]["path"],
        )


if __name__ == "__main__":
    unittest.main()
