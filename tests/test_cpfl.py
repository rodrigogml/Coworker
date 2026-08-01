"""Testes da consulta direta e segura de contas da CPFL."""

from __future__ import annotations

import importlib.util
import io
import sys
import tempfile
import unittest
import urllib.parse
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


class _Headers:
    @staticmethod
    def get_content_charset() -> str:
        return "utf-8"


class _Response:
    def __init__(self, url: str, body: str):
        self._url = url
        self._body = body.encode("utf-8")
        self.headers = _Headers()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def geturl(self) -> str:
        return self._url

    def read(self, limit: int) -> bytes:
        return self._body[:limit]


class _Opener:
    def __init__(self, responses: list[_Response]):
        self.responses = responses
        self.requests = []

    def open(self, request, *, timeout):
        self.requests.append((request, timeout))
        return self.responses.pop(0)


class CpflTests(unittest.TestCase):
    link = "https://contadigital.cpfl.com.br/Boleto/boletolink.aspx?token=falso"
    entity_ref = "Pessoas/Fisicas/Pessoa Teste"

    @staticmethod
    def pix_payload() -> str:
        prefix = "00020153039865802BR6304"
        return prefix + CPFL._crc16(prefix.encode("utf-8"))

    def test_builds_request_without_configuration_or_email(self) -> None:
        request = CPFL.build_request(self.link, self.entity_ref)

        self.assertEqual(self.entity_ref, request.entity_ref)
        self.assertEqual(16, len(request.request_id))
        self.assertNotIn("token=falso", str(request.request_id))

    def test_rejects_non_official_or_incomplete_links(self) -> None:
        invalid = (
            "https://example.com/Boleto/boletolink.aspx?token=x",
            "http://contadigital.cpfl.com.br/Boleto/boletolink.aspx?token=x",
            "https://contadigital.cpfl.com.br/Boleto/boletolink.aspx",
            "https://contadigital.cpfl.com.br/Boleto/boletolink.aspx?token=x#fragment",
        )
        for link in invalid:
            with self.subTest(link=link), self.assertRaises(CPFL.CpflError):
                CPFL._validate_portal_url(link)

    def test_reads_link_from_file_or_stdin_but_not_argument_value(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "link.txt"
            source.write_text(self.link + "\n", encoding="utf-8")
            from_file = CPFL.read_link(link_file=source, link_stdin=False)
        from_stdin = CPFL.read_link(
            link_file=None,
            link_stdin=True,
            stdin=io.StringIO(self.link + "\n"),
        )

        self.assertEqual(self.link, from_file)
        self.assertEqual(self.link, from_stdin)
        parser_help = CPFL.build_parser().format_help()
        self.assertNotIn("--link LINK", parser_help)

    def test_portal_uses_only_first_four_cpf_digits(self) -> None:
        request = CPFL.build_request(self.link, self.entity_ref)
        first_page = (
            '<form action="/Boleto/boletolink.aspx?token=falso">'
            '<input name="txtNumDoc" value="">'
            '<input name="__VIEWSTATE" value="state">'
            "</form>"
        )
        result_page = '<input name="resultado" value="ok">'
        opener = _Opener([
            _Response(self.link, first_page),
            _Response(self.link, result_page),
        ])
        with (
            patch.object(CPFL.urllib.request, "build_opener", return_value=opener),
            patch.object(CPFL, "read_entry_attribute", return_value="12345678901"),
        ):
            CPFL._open_portal(request, 30)

        posted = opener.requests[1][0].data.decode("utf-8")
        fields = urllib.parse.parse_qs(posted)
        self.assertEqual(["1234"], fields["txtNumDoc"])
        self.assertNotIn("12345678901", posted)

    def test_payment_data_validates_without_returning_codes_in_summary(self) -> None:
        barcode = "8" + "1" * 47
        pix = self.pix_payload()
        page = (
            f'<input type="hidden" name="hdCodigoBarras" value="{barcode}">'
            f'<input type="hidden" name="hdPIX" value="{pix}">'
        )
        request = CPFL.build_request(self.link, self.entity_ref)
        with patch.object(CPFL, "_open_portal", return_value=(self.link, page)):
            payment = CPFL.retrieve_payment_data(request)
        summary = {
            "barcode": CPFL.validate_barcode(payment["payment"]["barcode"]),
            "pix": CPFL.validate_pix(payment["payment"]["pix"]),
        }

        self.assertTrue(summary["barcode"]["febraban_collection_line"])
        self.assertTrue(summary["pix"]["crc16_valid"])
        self.assertNotIn(barcode, str(summary))
        self.assertNotIn(pix, str(summary))

    def test_private_output_is_idempotent_and_confined_to_data(self) -> None:
        target = PROJECT_ROOT / "data" / "test-cpfl-output.json"
        payload = {"payment": {"barcode": "fake", "pix": "fake"}}
        try:
            self.assertTrue(CPFL.write_private_json(target, payload, overwrite=False))
            self.assertFalse(CPFL.write_private_json(target, payload, overwrite=False))
        finally:
            target.unlink(missing_ok=True)
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(CPFL.CpflError, "dentro de 'data/'"):
                CPFL._safe_output_path(Path(temporary) / "outside.json", "id")

    def test_doctor_requires_protected_cpf_without_gmail(self) -> None:
        inspection = {
            "attributes": [{"name": "CPF", "present": True, "protected": True}]
        }
        with patch.object(CPFL, "inspect_entry", return_value=inspection):
            result = CPFL.command_doctor(self.entity_ref)

        self.assertTrue(result["cpf_protected"])
        self.assertFalse(result["configuration_required"])
        self.assertFalse(result["secrets_exposed"])


if __name__ == "__main__":
    unittest.main()
