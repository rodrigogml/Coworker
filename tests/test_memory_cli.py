"""Testes de contrato da interface de memória da Coworker."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1] / "instance"
SCRIPT = PROJECT_ROOT / "scripts" / "memory.py"


class MemoryCliTest(unittest.TestCase):
    """Valida os fluxos públicos e as proteções da memória SQLite."""

    def setUp(self) -> None:
        """Cria um banco isolado para cada teste."""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database = Path(self.temporary_directory.name) / "memory.sqlite3"

    def run_cli(
        self, *arguments: str, expected_return_code: int = 0
    ) -> dict[str, object]:
        """Executa o utilitário e retorna sua resposta JSON."""
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--database",
                str(self.database),
                *arguments,
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(
            expected_return_code,
            completed.returncode,
            msg=f"stdout={completed.stdout}\nstderr={completed.stderr}",
        )
        output = completed.stdout if completed.returncode == 0 else completed.stderr
        return json.loads(output)

    def initialize(self) -> None:
        """Inicializa o banco de teste."""
        response = self.run_cli("init")
        self.assertTrue(response["ok"])

    def test_memory_lifecycle(self) -> None:
        """Cria, pesquisa, substitui e esquece uma memória."""
        self.initialize()
        created = self.run_cli(
            "remember",
            "--kind",
            "preference",
            "--subject",
            "comunicação",
            "--content",
            "Prefere respostas em português.",
            "--source",
            "usuário",
            "--tag",
            "idioma",
        )
        memory = created["memory"]
        memory_id = memory["id"]
        self.assertEqual(["idioma"], memory["tags"])

        searched = self.run_cli("search", "português")
        self.assertEqual(1, searched["count"])
        self.assertEqual(memory_id, searched["memories"][0]["id"])

        replacement = self.run_cli(
            "supersede",
            memory_id,
            "--source",
            "usuário",
            "--content",
            "Prefere respostas objetivas em português.",
        )
        replacement_id = replacement["memory"]["id"]
        self.assertEqual(memory_id, replacement["memory"]["supersedes_id"])

        old_memory = self.run_cli("show", memory_id)
        self.assertEqual("superseded", old_memory["memory"]["status"])

        forgotten = self.run_cli("forget", replacement_id, "--confirm")
        self.assertFalse(forgotten["recoverable_from_active_database"])
        missing = self.run_cli(
            "show", replacement_id, expected_return_code=1
        )
        self.assertFalse(missing["ok"])

    def test_rejects_probable_secret(self) -> None:
        """Recusa conteúdo que aparente conter uma credencial."""
        self.initialize()
        response = self.run_cli(
            "remember",
            "--kind",
            "reference",
            "--subject",
            "API",
            "--content",
            "api_key=valor-secreto",
            "--source",
            "usuário",
            expected_return_code=1,
        )
        self.assertFalse(response["ok"])
        self.assertIn("segredo", response["error"])

    def test_accepts_credential_reference_without_secret(self) -> None:
        """Permite guardar apenas o nome de uma credencial externa."""
        self.initialize()
        response = self.run_cli(
            "remember",
            "--kind",
            "reference",
            "--subject",
            "Gmail pessoal",
            "--content",
            "Usar a credencial armazenada no cofre do sistema.",
            "--source",
            "usuário",
            "--credential-ref",
            "gmail-pessoal",
            "--sensitivity",
            "confidential",
        )
        self.assertEqual("gmail-pessoal", response["memory"]["credential_ref"])

    def test_backup_creates_readable_copy(self) -> None:
        """Cria um backup consistente e inicializado."""
        self.initialize()
        backup_path = Path(self.temporary_directory.name) / "backup.sqlite3"
        response = self.run_cli("backup", "--output", str(backup_path))
        self.assertTrue(backup_path.exists())
        self.assertEqual(str(backup_path.resolve()), response["backup"])


if __name__ == "__main__":
    unittest.main()
