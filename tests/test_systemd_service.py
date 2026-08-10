import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.systemd_service import (
    SystemdServiceError,
    build_definition,
    render_unit,
    validate_service_name,
)


class SystemdServiceTests(unittest.TestCase):
    def test_service_name_is_restricted(self):
        self.assertEqual("coworker-demo", validate_service_name("coworker-demo"))
        with self.assertRaises(SystemdServiceError):
            validate_service_name("../unsafe")

    def test_unit_contains_isolated_runtime_and_encrypted_credential(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gateway = root / "interfaces" / "telegram" / "gateway.py"
            gateway.parent.mkdir(parents=True)
            gateway.write_text("", encoding="utf-8")
            definition = build_definition(
                root,
                instance_id="demo",
                service_name="coworker-demo",
                unit_dir=root / "units",
                credential_path=root / "data" / "secrets" / "master-password.cred",
                user="coworker",
            )
            unit = render_unit(definition)

        self.assertIn("User=coworker", unit)
        self.assertIn("LoadCredentialEncrypted=coworker-master-password:", unit)
        self.assertIn("NoNewPrivileges=true", unit)
        self.assertIn("ProtectHome=true", unit)
        self.assertIn("ReadWritePaths=", unit)
        self.assertNotIn("SetCredential=", unit)

    def test_install_rejects_missing_encrypted_credential(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gateway = root / "interfaces" / "telegram" / "gateway.py"
            gateway.parent.mkdir(parents=True)
            gateway.write_text("", encoding="utf-8")
            definition = build_definition(root, instance_id="demo")
            with (
                patch("scripts.systemd_service.os.name", "posix"),
                self.assertRaises(SystemdServiceError),
            ):
                from scripts.systemd_service import install_service

                install_service(definition)


if __name__ == "__main__":
    unittest.main()
