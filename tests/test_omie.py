from __future__ import annotations

import argparse
import importlib.util
import io
import json
import sys
import unittest
import urllib.error
from contextlib import redirect_stderr
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


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


class FakeOperationClient:
    def __init__(self, handlers=None) -> None:
        self.handlers = handlers or {}
        self.calls = []
        self.config = omie.OmieConfig(
            "https://app.omie.com.br/api/v1",
            "APIs/Omie",
            30,
            100,
            20,
        )

    def call(self, service, method, params):
        self.calls.append((service.resource, method, dict(params)))
        handler = self.handlers.get(method)
        if handler is None:
            return {"codigo_status": "0", "descricao_status": "OK"}
        if isinstance(handler, Exception):
            raise handler
        if callable(handler):
            return handler(dict(params))
        return dict(handler)

    def list_page(self, service, *, page, params):
        del params
        handler = self.handlers.get(service.list_call)
        items = handler if isinstance(handler, list) else []
        return list(items), {
            "page": page,
            "total_pages": 1,
            "records": len(items),
            "total_records": len(items),
        }


class OmieTests(unittest.TestCase):
    def test_missing_config_points_to_sandbox_safe_initializer(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "omie.toml"

            with self.assertRaises(omie.OmieToolError) as raised:
                omie.load_config(path)

        self.assertIn(
            "python scripts/integration_config.py init omie",
            str(raised.exception),
        )

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

    def test_mutation_response_exposes_only_operational_status(self):
        self.assertEqual(
            omie.summarize_mutation_response(
                {
                    "codigo_lancamento_omie": 10,
                    "codigo_status": "0",
                    "descricao_status": "OK",
                    "payload_echo": {"cnpj_cpf": "sensitive"},
                }
            ),
            {
                "codigo_lancamento_omie": 10,
                "codigo_status": "0",
                "descricao_status": "OK",
            },
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
        client = omie.OmieClient(
            config,
            "key",
            "secret",
            opener=lambda *_args, **_kwargs: self.fail("network call not expected"),
        )
        with self.assertRaises(omie.OmieToolError):
            client.call(
                omie.SERVICE_SPECS["payables"],
                "UpsertContaPagar",
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

    def test_write_allowlist_contains_only_planned_methods(self):
        self.assertEqual(
            dict(omie.SERVICE_SPECS["projects"].mutation_calls),
            {
                "create": "IncluirProjeto",
                "update": "AlterarProjeto",
                "deactivate": "AlterarProjeto",
                "delete": "ExcluirProjeto",
            },
        )
        self.assertNotIn(
            "UpsertContaReceber",
            {
                method
                for service in omie.SERVICE_SPECS.values()
                for _, method in service.mutation_calls
            },
        )

    def test_every_allowed_write_method_uses_mocked_http_transport(self):
        captured = []

        def opener(request, *, timeout):
            del timeout
            captured.append(json.loads(request.data.decode("utf-8"))["call"])
            return FakeResponse({"codigo_status": "0", "descricao_status": "OK"})

        config = omie.OmieConfig(
            "https://app.omie.com.br/api/v1",
            "APIs/Omie",
            30,
            100,
            20,
        )
        client = omie.OmieClient(config, "key", "secret", opener=opener)
        expected = []
        for service in omie.SERVICE_SPECS.values():
            for _, method in service.mutation_calls:
                expected.append(method)
                response = client.call(service, method, {"validated": True})
                self.assertEqual(response["codigo_status"], "0")

        self.assertCountEqual(captured, expected)

    def test_input_envelope_rejects_unknown_fields_and_imprecise_money(self):
        with self.assertRaises(omie.OmieToolError):
            omie.parse_input_document(
                json.dumps(
                    {
                        "schema_version": 1,
                        "request_id": "req-1",
                        "data": {},
                        "unexpected": True,
                    }
                )
            )
        with self.assertRaises(omie.OmieToolError):
            omie.decimal_value("10.001", "amount")

    def test_project_create_is_idempotent_by_derived_integration_id(self):
        def project_show(params):
            self.assertIn("codInt", params)
            return {
                "codigo": 44,
                "codInt": params["codInt"],
                "nome": "Projeto Alfa",
                "inativo": "N",
            }

        client = FakeOperationClient({"ConsultarProjeto": project_show})
        calls = omie.prepare_project_call(
            client,
            "create",
            {"data": {"name": "Projeto Alfa"}},
            "project-request-1",
        )

        self.assertEqual(len(calls), 1)
        self.assertIsNotNone(calls[0].already_applied)
        self.assertEqual(calls[0].method, "IncluirProjeto")
        self.assertLessEqual(len(calls[0].params["codInt"]), 20)

    def test_customer_delete_requires_inactive_record_and_confirmation(self):
        active = FakeOperationClient(
            {
                "ConsultarCliente": {
                    "codigo_cliente_omie": 10,
                    "razao_social": "Fornecedor",
                    "nome_fantasia": "Fornecedor",
                    "inativo": "N",
                }
            }
        )
        with self.assertRaises(omie.OmieToolError):
            omie.prepare_customer_call(
                active,
                "delete",
                {"selector": {"id": 10}, "confirm_delete": True},
                "delete-1",
            )

        inactive = FakeOperationClient(
            {
                "ConsultarCliente": {
                    "codigo_cliente_omie": 10,
                    "razao_social": "Fornecedor",
                    "nome_fantasia": "Fornecedor",
                    "inativo": "S",
                }
            }
        )
        call = omie.prepare_customer_call(
            inactive,
            "delete",
            {"selector": {"id": 10}, "confirm_delete": True},
            "delete-1",
        )[0]
        self.assertEqual(call.method, "ExcluirCliente")

    def test_exact_name_resolution_rejects_ambiguity_and_inactive_reference(self):
        ambiguous = FakeOperationClient(
            {
                "ListarProjetos": [
                    {"codigo": 1, "nome": "Obra A", "inativo": "N"},
                    {"codigo": 2, "nome": " obra   a ", "inativo": "N"},
                ]
            }
        )
        with self.assertRaises(omie.OmieToolError):
            omie.resolve_reference(ambiguous, "project", {"name": "OBRA A"})

        inactive = FakeOperationClient(
            {
                "ConsultarDepartamento": {
                    "codigo": "D1",
                    "descricao": "Administrativo",
                    "inativo": "S",
                }
            }
        )
        with self.assertRaises(omie.OmieToolError):
            omie.resolve_reference(inactive, "department", "D1")

    def test_financial_installments_resolve_project_and_allocations(self):
        def payable_show(_params):
            raise omie.OmieApiError(None, "registro não encontrado")

        client = FakeOperationClient(
            {
                "ConsultarContaPagar": payable_show,
                "ConsultarCliente": {
                    "codigo_cliente_omie": 7,
                    "razao_social": "Fornecedor X",
                    "inativo": "N",
                },
                "ConsultarContaCorrente": {
                    "nCodCC": 8,
                    "descricao": "Banco",
                    "inativo": "N",
                },
                "ConsultarCategoria": {
                    "codigo": "2.01.01",
                    "descricao": "Serviços",
                    "conta_inativa": "N",
                },
                "ConsultarDepartamento": {
                    "codigo": "D1",
                    "descricao": "Operação",
                    "inativo": "N",
                },
                "ConsultarProjeto": {
                    "codigo": 9,
                    "nome": "Projeto",
                    "inativo": "N",
                },
            }
        )
        data = {
            "counterparty": {"id": 7},
            "amount": "300.00",
            "category": {"code": "2.01.01"},
            "departments": [{"code": "D1", "percentage": "100.00"}],
            "current_account": {"id": 8},
            "project": {"id": 9},
            "installments": [
                {"due_date": "10/08/2026", "amount": "100.00"},
                {"due_date": "10/09/2026", "amount": "200.00"},
            ],
        }

        calls = omie.prepare_financial_call(
            client, "payables", "create", {"data": data}, "finance-1"
        )

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].params["numero_parcela"], "001/002")
        self.assertEqual(calls[1].params["valor_documento"], 200)
        self.assertEqual(calls[0].params["codigo_projeto"], 9)
        self.assertEqual(calls[0].params["distribuicao"][0]["nPerDep"], 100)
        self.assertNotEqual(
            calls[0].params["codigo_lancamento_integracao"],
            calls[1].params["codigo_lancamento_integracao"],
        )

    def test_installments_must_sum_to_principal(self):
        with self.assertRaises(omie.OmieToolError):
            omie.financial_installment_data(
                {
                    "amount": "100.00",
                    "installments": [
                        {"due_date": "10/08/2026", "amount": "99.99"}
                    ],
                }
            )

    def test_financial_delete_detects_settled_status_without_paid_value(self):
        self.assertGreater(
            omie.title_paid_amount({"status_titulo": "LIQUIDADO"}),
            0,
        )

    def test_transfer_uses_single_tra_lancamento_and_distinct_accounts(self):
        account_by_id = {
            1: {"nCodCC": 1, "descricao": "Origem", "inativo": "N"},
            2: {"nCodCC": 2, "descricao": "Destino", "inativo": "N"},
        }

        def account_show(params):
            return account_by_id[params["nCodCC"]]

        def transfer_show(_params):
            raise omie.OmieApiError(None, "não encontrado")

        client = FakeOperationClient(
            {
                "ConsultarContaCorrente": account_show,
                "ConsultarCategoria": {
                    "codigo": "2.99.01",
                    "descricao": "Transferência",
                    "conta_inativa": "N",
                },
                "ConsultaLancCC": transfer_show,
            }
        )
        call = omie.prepare_transfer_call(
            client,
            "create",
            {
                "data": {
                    "source_account": {"id": 1},
                    "destination_account": {"id": 2},
                    "date": "02/08/2026",
                    "amount": "50.00",
                    "category": {"code": "2.99.01"},
                }
            },
            "transfer-1",
        )[0]

        self.assertEqual(call.method, "IncluirLancCC")
        self.assertEqual(call.params["detalhes"]["cTipo"], "TRA")
        self.assertEqual(call.params["cabecalho"]["nCodCC"], 1)
        self.assertEqual(call.params["transferencia"]["nCodCCDestino"], 2)

        with self.assertRaises(omie.OmieToolError):
            omie.transfer_payload(
                client,
                {},
                {
                    "source_account": {"id": 1},
                    "destination_account": {"id": 1},
                    "date": "02/08/2026",
                    "amount": "50.00",
                    "category": {"code": "2.99.01"},
                },
                integration_id="cw-test",
            )

    def test_batch_is_fully_validated_before_first_write(self):
        def project_show(_params):
            raise omie.OmieApiError(None, "não encontrado")

        client = FakeOperationClient({"ConsultarProjeto": project_show})
        document = {
            "schema_version": 1,
            "request_id": "batch-1",
            "items": [
                {"data": {"name": "Projeto válido"}},
                {"data": {}},
            ],
        }
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "input.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            args = argparse.Namespace(
                profile="empresa",
                input_stdin=False,
                input_file=str(path),
                resource="projects",
                operation="create",
                dry_run=False,
            )
            with self.assertRaises(omie.OmieToolError):
                omie.execute_mutation(client, args)

        self.assertFalse(
            any(method == "IncluirProjeto" for _, method, _ in client.calls)
        )

    def test_dry_run_never_performs_write(self):
        def project_show(_params):
            raise omie.OmieApiError(None, "não encontrado")

        client = FakeOperationClient({"ConsultarProjeto": project_show})
        document = {
            "schema_version": 1,
            "request_id": "dry-run-1",
            "data": {"name": "Projeto"},
        }
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "input.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            args = argparse.Namespace(
                profile="empresa",
                input_stdin=False,
                input_file=str(path),
                resource="projects",
                operation="create",
                dry_run=True,
            )
            result = omie.execute_mutation(client, args)

        self.assertTrue(result["dry_run"])
        self.assertEqual(result["calls"][0]["method"], "IncluirProjeto")
        self.assertFalse(
            any(method == "IncluirProjeto" for _, method, _ in client.calls)
        )

    def test_every_mutation_requires_explicit_profile(self):
        args = argparse.Namespace(
            profile=None,
            input_stdin=True,
            input_file=None,
            resource="projects",
            operation="create",
            dry_run=True,
        )
        with self.assertRaises(omie.OmieToolError):
            omie.execute_mutation(FakeOperationClient(), args)

    def test_main_rejects_missing_write_profile_before_loading_config(self):
        stderr = io.StringIO()
        argv = [
            "omie.py",
            "projects",
            "create",
            "--input-stdin",
            "--dry-run",
        ]
        with (
            patch.object(sys, "argv", argv),
            patch.object(
                omie,
                "load_config",
                side_effect=AssertionError("config must not be loaded"),
            ),
            redirect_stderr(stderr),
        ):
            result = omie.main()

        self.assertEqual(result, 2)
        self.assertIn("--profile", stderr.getvalue())

    def test_create_recovers_after_ambiguous_timeout(self):
        class RecoveryClient(FakeOperationClient):
            def __init__(self):
                super().__init__()
                self.show_count = 0

            def call(self, service, method, params):
                self.calls.append((service.resource, method, dict(params)))
                if method == "ConsultarProjeto":
                    self.show_count += 1
                    if self.show_count == 1:
                        raise omie.OmieApiError(None, "não encontrado")
                    return {
                        "codigo": 99,
                        "codInt": params["codInt"],
                        "nome": "Projeto recuperado",
                        "inativo": "N",
                    }
                if method == "IncluirProjeto":
                    raise omie.OmieUnknownStateError("timeout")
                return super().call(service, method, params)

        client = RecoveryClient()
        document = {
            "schema_version": 1,
            "request_id": "recover-1",
            "data": {"name": "Projeto recuperado"},
        }
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "input.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            args = argparse.Namespace(
                profile="empresa",
                input_stdin=False,
                input_file=str(path),
                resource="projects",
                operation="create",
                dry_run=False,
            )
            result = omie.execute_mutation(client, args)

        self.assertEqual(
            result["results"][0]["status"], "recovered_after_timeout"
        )

    def test_settlement_and_reconciliation_methods_use_documented_keys(self):
        title = {
            "codigo_lancamento_omie": 20,
            "codigo_lancamento_integracao": "TITLE-20",
            "valor_documento": 100,
            "valor_pago": 0,
        }
        client = FakeOperationClient(
            {
                "ConsultarContaReceber": title,
                "ConsultarContaCorrente": {
                    "nCodCC": 5,
                    "descricao": "Banco",
                    "inativo": "N",
                },
            }
        )
        receive = omie.prepare_financial_call(
            client,
            "receivables",
            "receive",
            {
                "selector": {"id": 20},
                "data": {
                    "current_account": {"id": 5},
                    "amount": "40.00",
                    "date": "02/08/2026",
                },
            },
            "receipt-1",
        )[0]
        self.assertEqual(receive.method, "LancarRecebimento")
        self.assertEqual(receive.params["codigo_lancamento"], 20)
        self.assertIn("codigo_baixa_integracao", receive.params)

        reconcile = omie.prepare_financial_call(
            client,
            "receivables",
            "reconcile",
            {"selector": {"integration_id": "SETTLE-1"}},
            "reconcile-1",
        )[0]
        self.assertEqual(reconcile.method, "ConciliarRecebimento")
        self.assertEqual(
            reconcile.params, {"codigo_baixa_integracao": "SETTLE-1"}
        )

    def test_doctor_reports_write_contracts_as_mock_validated(self):
        client = FakeOperationClient(
            {
                "ListarEmpresas": [],
            }
        )
        result = omie.execute_doctor(client, argparse.Namespace())
        self.assertIn("projects", result["write_capabilities"])
        self.assertEqual(result["write_validation"], "mocked_only")
        self.assertFalse(result["real_write_tested"])

    def test_parser_never_accepts_credentials_or_arbitrary_calls(self):
        parser = omie.build_parser()
        help_text = parser.format_help()
        self.assertNotIn("--app-key", help_text)
        self.assertNotIn("--app-secret", help_text)
        self.assertNotIn("--call", help_text)


if __name__ == "__main__":
    unittest.main()
