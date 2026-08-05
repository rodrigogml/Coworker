"""Testes da ferramenta de bancos operacionais privados."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts import instance_db


class InstanceDbTest(unittest.TestCase):
    """Verifica isolamento, schema declarativo e CRUD parametrizado."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        config = self.root / "data" / "config"
        config.mkdir(parents=True)
        (config / "identity.toml").write_text(
            '[identity]\ninstance_id = "test-instance"\n', encoding="utf-8"
        )

    def test_database_table_crud_backup_and_recoverable_delete(self) -> None:
        created = instance_db.create_database(
            "tasks", purpose="Teste", project_root=self.root
        )
        self.assertTrue(created["created"])
        instance_db.create_table(
            "tasks",
            "jobs",
            [
                "id:integer:primary",
                "draft_key:text:unique",
                "status:text:required",
                "amount:decimal",
                "metadata:json",
            ],
            project_root=self.root,
        )
        inserted = instance_db.mutate_row(
            "tasks",
            "jobs",
            [
                "draft_key=job-1",
                "status=draft",
                "amount=12.50",
                'metadata={"source":"telegram"}',
            ],
            upsert_key=None,
            project_root=self.root,
        )
        self.assertEqual("insert", inserted["operation"])
        updated = instance_db.mutate_row(
            "tasks",
            "jobs",
            ["draft_key=job-1", "status=posted"],
            upsert_key="draft_key",
            project_root=self.root,
        )
        self.assertEqual("update", updated["operation"])
        instance_db.add_column(
            "tasks", "jobs", "notes:text", project_root=self.root
        )
        description = instance_db.describe_table(
            "tasks", "jobs", project_root=self.root
        )
        self.assertEqual("text", description["columns"]["notes"]["type"])
        listed = instance_db.list_rows(
            "tasks", "jobs", ["status=posted"], 50, project_root=self.root
        )
        self.assertEqual(1, listed["count"])
        self.assertEqual("12.5", listed["rows"][0]["amount"])
        backup = instance_db.backup_database("tasks", project_root=self.root)
        self.assertTrue(Path(backup["backup"]).is_file())
        deleted = instance_db.delete_database(
            "tasks", confirm=True, project_root=self.root
        )
        self.assertTrue(deleted["deleted"])
        self.assertTrue(Path(deleted["recoverable_path"]).is_file())

    def test_database_names_and_secret_columns_are_rejected(self) -> None:
        with self.assertRaises(instance_db.InstanceDbError):
            instance_db.database_path("../outside", project_root=self.root)
        instance_db.create_database("safe", purpose=None, project_root=self.root)
        with self.assertRaises(instance_db.InstanceDbError):
            instance_db.create_table(
                "safe", "jobs", ["token:text"], project_root=self.root
            )
        with self.assertRaises(instance_db.InstanceDbError):
            instance_db.create_table(
                "safe", "jobs", ["id:integer", "id:text"], project_root=self.root
            )

    def test_foreign_instance_cannot_operate_database(self) -> None:
        instance_db.create_database("owned", purpose=None, project_root=self.root)
        identity = self.root / "data" / "config" / "identity.toml"
        identity.write_text('[identity]\ninstance_id = "other-instance"\n', encoding="utf-8")
        with self.assertRaisesRegex(instance_db.InstanceDbError, "outra instância"):
            instance_db.list_rows("owned", "missing", [], 1, project_root=self.root)

    def test_outputs_are_json_serializable(self) -> None:
        value = {"ok": True, "metadata": {"a": 1}}
        json.dumps(value, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
