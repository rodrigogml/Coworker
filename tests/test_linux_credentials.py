import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import credential_vault


class LinuxCredentialTests(unittest.TestCase):
    def test_reads_only_credential_delivered_by_runtime_environment(self):
        if os.name == "nt":
            self.skipTest("Permissões POSIX não são reproduzíveis no Windows.")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / credential_vault.LINUX_CREDENTIAL_NAME
            path.write_text("secret-value\n", encoding="utf-8")
            os.chmod(path, 0o600)
            with patch.dict(
                credential_vault.os.environ,
                {"CREDENTIALS_DIRECTORY": directory},
                clear=True,
            ):
                self.assertEqual("secret-value", credential_vault.read_linux_credential())

    def test_rejects_world_readable_credential(self):
        if os.name == "nt":
            self.skipTest("Permissões POSIX não são reproduzíveis no Windows.")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / credential_vault.LINUX_CREDENTIAL_NAME
            path.write_text("secret-value\n", encoding="utf-8")
            os.chmod(path, 0o644)
            with (
                patch.dict(
                    credential_vault.os.environ,
                    {"CREDENTIALS_DIRECTORY": directory},
                    clear=True,
                ),
                self.assertRaises(credential_vault.VaultToolError),
            ):
                credential_vault.read_linux_credential()

    def test_missing_runtime_credential_is_actionable(self):
        with (
            patch.dict(credential_vault.os.environ, {}, clear=True),
            self.assertRaisesRegex(credential_vault.VaultToolError, "systemd"),
        ):
            credential_vault.read_linux_credential()


if __name__ == "__main__":
    unittest.main()
