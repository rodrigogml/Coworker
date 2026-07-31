"""Testes da extração segura de contas digitais da CPFL."""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PROJECT_ROOT / "skills" / "cpfl" / "scripts" / "cpfl.py"
SPEC = importlib.util.spec_from_file_location("cpfl_skill", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Não foi possível carregar cpfl.py.")
CPFL = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = CPFL
SPEC.loader.exec_module(CPFL)


class CpflTests(unittest.TestCase):
    """Valida autenticidade, formatos e ausência de exposição financeira."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        root = Path(self.temporary_directory.name)
        self.config_path = root / "cpfl.toml"
        self.config_path.write_text(
            'default_profile = "pessoal"\n\n'
            "[portal]\n"
            'allowed_host = "contadigital.cpfl.com.br"\n'
            'allowed_path = "/Boleto/boletolink.aspx"\n'
            "timeout_seconds = 30\n\n"
            "[mail]\n"
            'sender = "contadigital@cpfl.com.br"\n'
            "search_days = 120\n"
            "search_limit = 20\n\n"
            "[profiles.pessoal]\n"
            'gmail_profile = "pessoal"\n'
            'entity_ref = "Pessoas/Fisicas/Pessoa Teste"\n'
            'consumer_unit = ""\n',
            encoding="utf-8",
        )
        self.config = CPFL.load_config(self.config_path)

    @staticmethod
    def pix_payload() -> str:
        prefix = "00020153039865802BR6304"
        return prefix + CPFL._crc16(prefix.encode("utf-8"))

    def message(self, *, authenticated: bool = True) -> dict[str, object]:
        authentication = (
            "mx.google.com; dkim=pass header.i=@cpfl.com.br; spf=pass "
            "smtp.mailfrom=contadigital@cpfl.com.br; dmarc=pass "
            "header.from=cpfl.com.br"
            if authenticated
            else "mx.google.com; dkim=fail; spf=fail; dmarc=fail"
        )
        body = (
            '<a href="https://contadigital.cpfl.com.br/Boleto/boletolink.aspx?token=falso">Conta</a>'
            " Número da UC: 123.456.789-01 Mês de referência: 07/2026 "
            "Data de vencimento: 24/08/2026 Valor: R$ 123,45"
        )
        encoded = base64.urlsafe_b64encode(body.encode("utf-8")).decode("ascii")
        return {
            "id": "abcdef1234",
            "payload": {
                "headers": [
                    {"name": "From", "value": "<contadigital@cpfl.com.br>"},
                    {"name": "Subject", "value": "Conta por e-mail CPFL"},
                    {"name": "Date", "value": "Tue, 28 Jul 2026 04:31:14 -0300"},
                    {"name": "Authentication-Results", "value": authentication},
                ],
                "body": {"data": encoded},
            },
        }

    def test_loads_profile_without_personal_values_in_public_model(self) -> None:
        self.assertEqual(self.config.profile.name, "pessoal")
        self.assertEqual(self.config.portal.allowed_host, "contadigital.cpfl.com.br")

    def test_parses_only_authenticated_official_message(self) -> None:
        bill = CPFL.parse_bill_message(self.message(), self.config)
        self.assertEqual(bill.reference_month, "07/2026")
        self.assertEqual(bill.due_date, "24/08/2026")
        self.assertEqual(bill.amount, "R$ 123,45")
        self.assertNotIn("token=falso", str(CPFL.bill_summary(bill)))
        with self.assertRaisesRegex(CPFL.CpflError, "autenticação"):
            CPFL.parse_bill_message(self.message(authenticated=False), self.config)

    def test_rejects_link_outside_cpfl_allowlist(self) -> None:
        with self.assertRaisesRegex(CPFL.CpflError, "fora do portal"):
            CPFL._validate_portal_url("https://example.com/Boleto/boletolink.aspx", self.config)

    def test_payment_data_validates_without_returning_codes_in_summary(self) -> None:
        barcode = "8" + "1" * 47
        pix = self.pix_payload()
        page = (
            f'<input type="hidden" name="hdCodigoBarras" value="{barcode}">'
            f'<input type="hidden" name="hdPIX" value="{pix}">'
        )
        bill = CPFL.parse_bill_message(self.message(), self.config)
        with patch.object(CPFL, "_open_portal", return_value=(bill.portal_url, page)):
            payment = CPFL.retrieve_payment_data(bill, self.config)
        summary = {
            "barcode": CPFL.validate_barcode(payment["payment"]["barcode"]),
            "pix": CPFL.validate_pix(payment["payment"]["pix"]),
        }
        self.assertTrue(summary["barcode"]["febraban_collection_line"])
        self.assertTrue(summary["pix"]["crc16_valid"])
        self.assertNotIn(barcode, str(summary))
        self.assertNotIn(pix, str(summary))

    def test_private_output_is_idempotent_and_confined_to_data(self) -> None:
        data_root = PROJECT_ROOT / "data"
        target = data_root / "test-cpfl-output.json"
        payload = {"payment": {"barcode": "fake", "pix": "fake"}}
        try:
            self.assertTrue(CPFL.write_private_json(target, payload, overwrite=False))
            self.assertFalse(CPFL.write_private_json(target, payload, overwrite=False))
        finally:
            target.unlink(missing_ok=True)
        with self.assertRaisesRegex(CPFL.CpflError, "dentro de 'data/'"):
            CPFL._safe_output_path(Path(self.temporary_directory.name) / "outside.json", "id")

    def test_doctor_requires_protected_cpf(self) -> None:
        inspection = {
            "attributes": [
                {"name": "CPF", "present": True, "protected": True}
            ]
        }
        with (
            patch.object(CPFL, "_run_gmail", return_value={"ok": True}),
            patch.object(CPFL, "inspect_entry", return_value=inspection),
        ):
            result = CPFL.command_doctor(self.config)
        self.assertTrue(result["cpf_protected"])
        self.assertFalse(result["secrets_exposed"])


if __name__ == "__main__":
    unittest.main()
