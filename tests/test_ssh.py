from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "instance" / "skills" / "ssh" / "scripts" / "ssh.py"
SPEC = importlib.util.spec_from_file_location("ssh_skill_tested", SCRIPT)
ssh = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(ssh)


class SSHSkillTests(unittest.TestCase):
    def test_profile_rejects_empty_host_before_vault_access(self) -> None:
        with self.assertRaisesRegex(ssh.SSHSkillError, "Host ou porta"):
            ssh._profile({"default_profile": "turing", "profiles": {"turing": {"host": "", "port": 22}}}, "turing")

    def test_private_key_validation_rejects_public_key(self) -> None:
        with self.assertRaises(ssh.SSHSkillError):
            ssh._validate_key("id_ed25519.pub", b"-----BEGIN PUBLIC KEY-----")

    def test_private_key_validation_accepts_supported_header(self) -> None:
        ssh._validate_key("id_ed25519", b"-----BEGIN OPENSSH PRIVATE KEY-----")

    def test_material_falls_back_to_the_only_attachment_when_name_differs(self) -> None:
        key = b"-----BEGIN OPENSSH PRIVATE KEY-----"
        with patch.object(
            ssh.credential_vault,
            "read_entry_attachment",
            side_effect=[
                ssh.credential_vault.VaultToolError("nome divergente"),
                ("telegram-original-name", key),
            ],
        ) as read_attachment:
            with patch.object(ssh.credential_vault, "read_entry_username", return_value="user"):
                with patch.object(ssh.credential_vault, "read_entry_secret", return_value=""):
                    username, _passphrase, returned_key = ssh._read_material(
                        {
                            "credential_ref": "Infraestrutura/Turing/SSH",
                            "attachment_name": "id_ed25519",
                        }
                    )
        self.assertEqual("user", username)
        self.assertEqual(key, returned_key)
        self.assertEqual(2, read_attachment.call_count)

    def test_check_removes_temporary_key_after_failure(self) -> None:
        created: list[Path] = []
        original_mkdtemp = ssh.tempfile.mkdtemp

        def track(*args, **kwargs):
            path = Path(original_mkdtemp(*args, **kwargs))
            created.append(path)
            return str(path)

        class Vault:
            def read_entry_username(self, _ref): return "user"
            def read_entry_secret(self, _ref): return "passphrase"
            def read_entry_attachment(self, _ref, _name): return "id_key", b"-----BEGIN OPENSSH PRIVATE KEY-----"

        with (
            patch.object(ssh.tempfile, "mkdtemp", side_effect=track),
            patch.object(ssh, "credential_vault", Vault()),
            patch.object(ssh.subprocess, "run", side_effect=FileNotFoundError),
        ):
            with self.assertRaisesRegex(ssh.SSHSkillError, "OpenSSH"):
                ssh._check({"host": "127.0.0.1", "port": 22, "credential_ref": "SSH"})

        self.assertTrue(created)
        self.assertFalse(created[0].exists())


if __name__ == "__main__":
    unittest.main()
