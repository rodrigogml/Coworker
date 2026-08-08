from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from interfaces.telegram.scripts.clone_credential import clone


class TelegramCredentialCloneTests(unittest.TestCase):
    def test_clone_requires_gateway_job_context(self) -> None:
        from types import SimpleNamespace

        arguments = SimpleNamespace(
            source="APIs/Origem", target="APIs/Destino", field=["Password"], confirm=True
        )
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(Exception):
                clone(arguments)

    def test_clone_uses_context_and_returns_metadata_only(self) -> None:
        from types import SimpleNamespace

        arguments = SimpleNamespace(
            source="APIs/Origem", target="APIs/Destino", field=["Username", "Password"], confirm=True
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "jobs" / "42" / "output"
            output.mkdir(parents=True)
            with (
                patch.dict(
                    os.environ,
                    {"COWORKER_JOB_OUTPUT": str(output), "COWORKER_CHAT_ID": "7"},
                    clear=False,
                ),
                patch(
                    "interfaces.telegram.scripts.clone_credential.clone_entry_fields"
                ) as clone_entry,
            ):
                result = clone(arguments)

        clone_entry.assert_called_once_with("APIs/Origem", "APIs/Destino", ("Username", "Password"))
        self.assertEqual(42, result["job_id"])
        self.assertTrue(result["source_preserved"])
        self.assertFalse(result["secret_exposed"])


if __name__ == "__main__":
    unittest.main()
