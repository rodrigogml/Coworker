import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from interfaces.telegram.credential_broker import (
    CredentialBrokerError,
    create_request,
    parse_field_spec,
    validate_fields,
)


class CredentialBrokerTests(unittest.TestCase):
    def test_accepts_attachment_with_ssh_fields(self):
        fields = validate_fields([
            parse_field_spec("username:Usuário SSH"),
            parse_field_spec("password:Passphrase"),
            parse_field_spec("attachment:Chave privada"),
        ])
        self.assertEqual([item.name for item in fields], ["username", "password", "attachment"])

    def test_attachment_can_be_requested_without_text_credentials(self):
        self.assertEqual(validate_fields([parse_field_spec("attachment")])[0].name, "attachment")

    def test_username_and_attachment_do_not_require_password(self):
        fields = validate_fields([
            parse_field_spec("username"), parse_field_spec("attachment"),
        ])
        self.assertEqual([item.name for item in fields], ["username", "attachment"])

    def test_username_requires_password(self):
        with self.assertRaises(CredentialBrokerError):
            validate_fields([parse_field_spec("username")])

    def test_duplicate_attachment_is_rejected(self):
        with self.assertRaises(CredentialBrokerError):
            validate_fields([parse_field_spec("attachment"), parse_field_spec("attachment")])

    def test_serializes_normalized_attachment_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "jobs" / "42" / "output"
            output.mkdir(parents=True)
            with patch.dict(
                os.environ,
                {"COWORKER_JOB_OUTPUT": str(output), "COWORKER_CHAT_ID": "7"},
                clear=False,
            ):
                request = create_request(
                    "Infraestrutura/Turing/SSH",
                    "Cadastrar credencial SSH",
                    [parse_field_spec("username"), parse_field_spec("attachment")],
                    600,
                    "id_ed25519",
                )
            payload = json.loads(request.request_path.read_text(encoding="utf-8"))
            self.assertEqual("id_ed25519", request.attachment_name)
            self.assertEqual("id_ed25519", payload["attachment_name"])

    def test_attachment_name_requires_attachment_field(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "jobs" / "42" / "output"
            output.mkdir(parents=True)
            with patch.dict(
                os.environ,
                {"COWORKER_JOB_OUTPUT": str(output), "COWORKER_CHAT_ID": "7"},
                clear=False,
            ):
                with self.assertRaises(CredentialBrokerError):
                    create_request(
                        "APIs/Teste",
                        "Cadastrar",
                        [parse_field_spec("password")],
                        600,
                        "id_ed25519",
                    )


if __name__ == "__main__":
    unittest.main()
