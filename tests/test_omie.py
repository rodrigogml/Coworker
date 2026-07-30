from __future__ import annotations

import argparse
import importlib.util
import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from tempfile import TemporaryDirectory


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PROJECT_ROOT / "skills" / "omie" / "scripts" / "omie.py"
SPEC = importlib.util.spec_from_file_location("omie_skill", MODULE_PATH)
omie = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = omie
SPEC.loader.exec_module(omie)


class FakeResponse:
    status = 200

    def __init__(self, payload) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class FakePagingClient:
    def __init__(self, pages) -> None:
        self.pages = pages
        self.config = omie.OmieConfig(
            "https://app.omie.com.br/api/v1",
            "APIs/Omie",
            30,
            100,
            2,
        )

    def list_page(self, service, *, page, params):
        del service, params
        return self.pages[page]


class OmieTests(unittest.TestCase):
    def test_load_config(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "omie.toml"
            path.write_text(
                'api_base = "https://app.omie.com.br/api/v1/"\n'
                'credential_ref = "APIs/Omie"\n'
                "timeout_seconds = 25\n"
                "page_size = 80\n"
                "max_pages = 10\n",
                encoding="utf-8",
            )
            config = omie.load_config(path)

        self.assertEqual(config.api_base, "https://app.omie.com.br/api/v1")
        self.assertEqual(config.page_size, 80)
        self.assertEqual(config.max_pages, 10)

    def test_config_rejects_credential_exfiltration_host(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "omie.toml"
            path.write_text(
                'api_base = "https://example.com/api/v1"\n'
                'credential_ref = "APIs/Omie"\n',
                encoding="utf-8",
            )
            with self.assertRaises(omie.OmieToolError):
                omie.load_config(path)

    def test_config_rejects_unexpected_path_on_allowed_host(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "omie.toml"
            path.write_text(
                'api_base = "https://app.omie.com.br/redirect"\n'
                'credential_ref = "APIs/Omie"\n',
                encoding="utf-8",
            )
            with self.assertRaises(omie.OmieToolError):
                omie.load_config(path)

    def test_config_requires_credential_entry(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "omie.toml"
            path.write_text(
                'api_base = "https://app.omie.com.br/api/v1"\n'
                'credential_ref = ""\n',
                encoding="utf-8",
            )
            with self.assertRaises(omie.OmieToolError):
                omie.load_config(path)

    def test_sanitizes_nested_secrets_and_certificates(self):
        payload = {
            "app_key": "hidden",
            "nested": {
                "senha": "hidden",
                "smtp_password": "hidden",
                "certificado_digital": "hidden",
                "value": "visible",
            },
        }
        self.assertEqual(
            omie.sanitize_payload(payload),
            {"nested": {"value": "visible"}},
        )

    def test_client_posts_credentials_only_inside_request(self):
        captured = {}

        def opener(request, *, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse(
                {
                    "pagina": 1,
                    "total_de_paginas": 1,
                    "registros": 0,
                    "total_de_registros": 0,
                    "empresas_cadastro": [],
                }
            )

        config = omie.OmieConfig(
            "https://app.omie.com.br/api/v1",
            "APIs/Omie",
            30,
            100,
            20,
        )
        client = omie.OmieClient(
            config,
            "key-value",
            "secret-value",
            opener=opener,
        )
        service = omie.SERVICE_SPECS["companies"]
        result = client.call(service, service.list_call, {"pagina": 1})

        request_payload = json.loads(captured["request"].data.decode("utf-8"))
        self.assertEqual(captured["request"].method, "POST")
        self.assertEqual(
            captured["request"].full_url,
            "https://app.omie.com.br/api/v1/geral/empresas/",
        )
        self.assertEqual(request_payload["app_key"], "key-value")
        self.assertEqual(request_payload["app_secret"], "secret-value")
        self.assertNotIn("key-value", json.dumps(result))
        self.assertNotIn("secret-value", json.dumps(result))
        client.close()
        self.assertEqual(client._app_key, "")
        self.assertEqual(client._app_secret, "")

    def test_client_redacts_credentials_from_api_fault(self):
        def opener(request, *, timeout):
            del request, timeout
            payload = json.dumps(
                {
                    "faultcode": "SOAP-ENV:Client-1",
                    "faultstring": "invalid key-value and secret-value",
                }
            ).encode("utf-8")
            raise urllib.error.HTTPError(
                "https://app.omie.com.br/api/v1/geral/empresas/",
                500,
                "error",
                {},
                io.BytesIO(payload),
            )

        config = omie.OmieConfig(
            "https://app.omie.com.br/api/v1",
            "APIs/Omie",
            30,
            100,
            20,
        )
        client = omie.OmieClient(
            config,
            "key-value",
            "secret-value",
            opener=opener,
        )
        service = omie.SERVICE_SPECS["companies"]

        with self.assertRaises(omie.OmieApiError) as raised:
            client.call(service, service.list_call, {})

        self.assertNotIn("key-value", str(raised.exception))
        self.assertNotIn("secret-value", str(raised.exception))
        self.assertIn("[REDACTED]", str(raised.exception))

    def test_client_rejects_method_outside_allowlist(self):
        config = omie.OmieConfig(
            "https://app.omie.com.br/api/v1",
            "APIs/Omie",
            30,
            100,
            20,
        )
        client = omie.OmieClient(config, "key", "secret")
        with self.assertRaises(omie.OmieToolError):
            client.call(
                omie.SERVICE_SPECS["payables"],
                "ExcluirContaPagar",
                {"codigo_lancamento_omie": 1},
            )

    def test_company_summary_excludes_certificate_material(self):
        result = omie.summarize(
            "companies",
            {
                "codigo_empresa": 123,
                "razao_social": "Example Ltda",
                "ct_eskey": "certificate",
                "csc_producao": "sensitive",
            },
        )
        self.assertEqual(
            result,
            {"codigo_empresa": 123, "razao_social": "Example Ltda"},
        )

    def test_all_pages_stops_at_configured_limit(self):
        pages = {
            1: (
                [{"codigo_cliente_omie": 1}],
                {
                    "page": 1,
                    "total_pages": 5,
                    "records": 1,
                    "total_records": 5,
                },
            ),
            2: (
                [{"codigo_cliente_omie": 2}],
                {
                    "page": 2,
                    "total_pages": 5,
                    "records": 1,
                    "total_records": 5,
                },
            ),
        }
        args = argparse.Namespace(
            resource="customers",
            page=1,
            all_pages=True,
            only_api=False,
            changed_from=None,
            changed_to=None,
            only_created=False,
            only_changed=False,
        )
        result = omie.execute_list(FakePagingClient(pages), args)

        self.assertEqual(result["count"], 2)
        self.assertTrue(result["pagination"]["truncated"])
        self.assertEqual(result["pagination"]["next_page"], 3)

    def test_financial_filters_use_documented_parameter_names(self):
        args = argparse.Namespace(
            only_api=False,
            changed_from="01/07/2026",
            changed_to="31/07/2026",
            only_created=False,
            only_changed=True,
            issued_from="01/07/2026",
            issued_to="31/07/2026",
            customer_id=123,
            status="ATRASADO",
        )
        params = omie.list_params("receivables", args)

        self.assertEqual(params["filtrar_por_data_de"], "01/07/2026")
        self.assertEqual(params["filtrar_apenas_alteracao"], "S")
        self.assertEqual(params["filtrar_cliente"], 123)
        self.assertEqual(params["filtrar_por_status"], "ATRASADO")

    def test_rejects_inverted_date_range_before_api_call(self):
        with self.assertRaises(omie.OmieToolError):
            omie.validate_date_range(
                "31/07/2026",
                "01/07/2026",
                "alteração",
            )

    def test_parser_never_accepts_credentials_or_arbitrary_calls(self):
        parser = omie.build_parser()
        help_text = parser.format_help()
        self.assertNotIn("--app-key", help_text)
        self.assertNotIn("--app-secret", help_text)
        self.assertNotIn("--call", help_text)


if __name__ == "__main__":
    unittest.main()
