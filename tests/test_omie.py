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

    def test_financial_update_changes_reconciliation_and_preserves_partial_values(self):
        current = {
            "codigo_lancamento_omie": 20,
            "codigo_lancamento_integracao": "PAY-20",
            "codigo_cliente_fornecedor": 7,
            "data_vencimento": "10/08/2026",
            "valor_documento": 100,
            "codigo_categoria": "2.01.01",
            "data_previsao": "10/08/2026",
            "id_conta_corrente": 5,
            "numero_documento": "DOC",
            "observacao": "old",
        }
        client = FakeOperationClient({"ConsultarContaPagar": current})
        call = omie.prepare_financial_call(
            client,
            "payables",
            "update",
            {"selector": {"id": 20}, "data": {"amount": "125.50", "reconcile": True}},
            "pay-update-1",
        )[0]
        self.assertEqual(call.method, "AlterarContaPagar")
        self.assertEqual(call.params["valor_documento"], 125.5)
        self.assertEqual(call.params["conciliar_documento"], "S")
        self.assertEqual(call.params["codigo_lancamento_omie"], 20)
        self.assertEqual(call.params["observacao"], "old")

        call = omie.prepare_financial_call(
            FakeOperationClient({"ConsultarContaPagar": current}),
            "payables",
            "update",
            {"selector": {"id": 20}, "data": {"reconcile": False}},
            "pay-update-2",
        )[0]
        self.assertEqual(call.params["conciliar_documento"], "N")

        with self.assertRaisesRegex(omie.OmieToolError, "booleano"):
            omie.prepare_financial_call(
                FakeOperationClient({"ConsultarContaPagar": current}),
                "payables",
                "update",
                {"selector": {"id": 20}, "data": {"reconcile": "true"}},
                "pay-update-3",
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

    def test_account_entries_parser_requires_nature_and_maps_origin(self):
        parser = omie.build_parser()
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["account-entries", "list"])

        args = parser.parse_args(
            ["--profile", "empresa", "account-entries", "list", "--nature", "expense"]
        )
        self.assertEqual(omie.list_params("account-entries", args), {"cOrigem": "EXTP"})

        args = parser.parse_args(
            ["--profile", "empresa", "account-entries", "list", "--nature", "revenue"]
        )
        self.assertEqual(omie.list_params("account-entries", args), {"cOrigem": "EXTR"})

    def test_account_entry_create_builds_direct_expense_payload(self):
        def entry_show(_params):
            raise omie.OmieApiError(None, "não encontrado")

        client = FakeOperationClient(
            {
                "ConsultarContaCorrente": {
                    "nCodCC": 5,
                    "descricao": "Banco",
                    "inativo": "N",
                },
                "ConsultarCategoria": {
                    "codigo": "2.01.01",
                    "descricao": "Tarifas",
                    "conta_inativa": "N",
                    "conta_despesa": "S",
                    "conta_receita": "N",
                    "transferencia": "N",
                    "totalizadora": "N",
                    "nao_exibir": "N",
                },
                "ConsultarCliente": {
                    "codigo_cliente_omie": 8,
                    "razao_social": "Banco",
                    "inativo": "N",
                },
                "ConsultarProjeto": {
                    "codigo": 9,
                    "nome": "Operação",
                    "inativo": "N",
                },
                "ConsultarDepartamento": {
                    "codigo": "ADM",
                    "descricao": "Administrativo",
                    "inativo": "N",
                },
                "ConsultaLancCC": entry_show,
            }
        )
        call = omie.prepare_account_entry_call(
            client,
            "create",
            {
                "data": {
                    "nature": "expense",
                    "account": {"id": 5},
                    "date": "04/08/2026",
                    "amount": "150.00",
                    "document_type": "DEB",
                    "category": {"code": "2.01.01"},
                    "counterparty": {"id": 8},
                    "project": {"id": 9},
                    "departments": [{"code": "ADM", "percentage": "100.00"}],
                    "document_number": "TARIFA-08",
                    "observation": "Tarifa bancária",
                }
            },
            "entry-1",
        )[0]

        self.assertEqual(call.method, "IncluirLancCC")
        self.assertEqual(call.params["cabecalho"]["nValorLanc"], 150)
        self.assertEqual(call.params["detalhes"]["cTipo"], "DEB")
        self.assertEqual(call.params["detalhes"]["cCodCateg"], "2.01.01")
        self.assertEqual(call.params["detalhes"]["nCodCliente"], 8)
        self.assertEqual(call.params["detalhes"]["nCodProjeto"], 9)
        self.assertEqual(call.params["departamentos"], [{"cCodDep": "ADM", "nPerDep": 100}])
        self.assertNotIn("transferencia", call.params)
        self.assertLessEqual(len(call.params["cCodIntLanc"]), 20)

    def test_account_entry_missing_is_not_found_but_auth_error_propagates(self):
        service = omie.SERVICE_SPECS["account-entries"]
        selector = {"cCodIntLanc": "cw-fa8aa68284fe12f1b"}
        missing = omie.OmieApiError(
            500,
            "ERROR: Lançamento de Conta Corrente não cadastrado para o "
            "Código de Integração [cw-fa8aa68284fe12f1b] !",
            fault_code="SOAP-ENV:Client-103",
        )
        auth_error = omie.OmieApiError(
            500,
            "ERROR: App Key não cadastrado para esta conta.",
            fault_code="SOAP-ENV:Client-103",
        )
        missing_without_accents = omie.OmieApiError(
            500,
            "Lancamento de Conta Corrente nao cadastrado para o codigo de integracao.",
        )

        self.assertTrue(omie.is_not_found_error(missing_without_accents))
        self.assertIsNone(
            omie.maybe_show(
                FakeOperationClient({"ConsultaLancCC": missing}),
                service,
                selector,
            )
        )
        with self.assertRaises(omie.OmieApiError) as raised:
            omie.maybe_show(
                FakeOperationClient({"ConsultaLancCC": auth_error}),
                service,
                selector,
            )

        self.assertIs(auth_error, raised.exception)

    def test_account_entry_create_accepts_revenue_category(self):
        def entry_show(_params):
            raise omie.OmieApiError(None, "não encontrado")

        client = FakeOperationClient(
            {
                "ConsultarContaCorrente": {"nCodCC": 5, "inativo": "N"},
                "ConsultarCategoria": {
                    "codigo": "1.01.01",
                    "conta_inativa": "N",
                    "conta_despesa": "N",
                    "conta_receita": "S",
                    "transferencia": "N",
                    "totalizadora": "N",
                    "nao_exibir": "N",
                },
                "ConsultaLancCC": entry_show,
            }
        )
        call = omie.prepare_account_entry_call(
            client,
            "create",
            {
                "data": {
                    "nature": "revenue",
                    "account": {"id": 5},
                    "date": "04/08/2026",
                    "amount": "25.00",
                    "document_type": "DIN",
                    "category": {"code": "1.01.01"},
                }
            },
            "entry-revenue-1",
        )[0]
        self.assertEqual(call.params["cabecalho"]["nValorLanc"], 25)
        self.assertEqual(call.params["detalhes"]["cCodCateg"], "1.01.01")

    def test_account_entry_rejects_invalid_categories_and_document_type(self):
        base_category = {
            "codigo": "2.01.01",
            "conta_inativa": "N",
            "conta_despesa": "S",
            "conta_receita": "N",
            "transferencia": "N",
            "totalizadora": "N",
            "nao_exibir": "N",
        }
        invalid_variants = (
            {"conta_inativa": "S"},
            {"totalizadora": "S"},
            {"nao_exibir": "S"},
            {"transferencia": "S"},
            {"conta_despesa": "N", "conta_receita": "S"},
        )
        for changes in invalid_variants:
            with self.subTest(changes=changes):
                category = {**base_category, **changes}
                client = FakeOperationClient(
                    {
                        "ConsultarContaCorrente": {"nCodCC": 5, "inativo": "N"},
                        "ConsultarCategoria": category,
                    }
                )
                with self.assertRaises(omie.OmieToolError):
                    omie.account_entry_payload(
                        client,
                        {},
                        {
                            "nature": "expense",
                            "account": {"id": 5},
                            "date": "04/08/2026",
                            "amount": "10.00",
                            "document_type": "DIN",
                            "category": {"code": "2.01.01"},
                        },
                        integration_id="cw-entry",
                    )

        client = FakeOperationClient(
            {
                "ConsultarContaCorrente": {"nCodCC": 5, "inativo": "N"},
                "ConsultarCategoria": base_category,
            }
        )
        for document_type in ("TRA", "din", "INVALID"):
            with self.subTest(document_type=document_type), self.assertRaises(omie.OmieToolError):
                omie.account_entry_payload(
                    client,
                    {},
                    {
                        "nature": "expense",
                        "account": {"id": 5},
                        "date": "04/08/2026",
                        "amount": "10.00",
                        "document_type": document_type,
                        "category": {"code": "2.01.01"},
                    },
                    integration_id="cw-entry",
                )

    def test_account_entry_rejects_mixed_category_natures(self):
        def category_show(params):
            code = params["codigo"]
            return {
                "codigo": code,
                "conta_inativa": "N",
                "conta_despesa": "S" if code.startswith("2") else "N",
                "conta_receita": "S" if code.startswith("1") else "N",
                "transferencia": "N",
                "totalizadora": "N",
                "nao_exibir": "N",
            }

        client = FakeOperationClient(
            {
                "ConsultarContaCorrente": {"nCodCC": 5, "inativo": "N"},
                "ConsultarCategoria": category_show,
            }
        )
        with self.assertRaises(omie.OmieToolError):
            omie.account_entry_payload(
                client,
                {},
                {
                    "nature": "expense",
                    "account": {"id": 5},
                    "date": "04/08/2026",
                    "amount": "100.00",
                    "document_type": "DIN",
                    "categories": [
                        {"code": "2.01", "percentage": "50.00"},
                        {"code": "1.01", "percentage": "50.00"},
                    ],
                },
                integration_id="cw-entry",
            )

    def test_account_entry_update_preserves_manual_id_and_clears_optional_fields(self):
        current = {
            "nCodLanc": 44,
            "cCodIntLanc": "",
            "cabecalho": {"nCodCC": 5, "dDtLanc": "04/08/2026", "nValorLanc": 100},
            "detalhes": {
                "cCodCateg": "2.01.01",
                "cTipo": "DIN",
                "cNumDoc": "DOC-1",
                "cObs": "Anterior",
            },
            "departamentos": [{"cCodDep": "ADM", "nPerc": 100}],
            "diversos": {"cOrigem": "EXTP", "cNatureza": "P"},
        }
        client = FakeOperationClient(
            {
                "ConsultaLancCC": current,
                "ConsultarCategoria": {
                    "codigo": "2.01.01",
                    "conta_inativa": "N",
                    "conta_despesa": "S",
                    "conta_receita": "N",
                    "transferencia": "N",
                    "totalizadora": "N",
                    "nao_exibir": "N",
                },
            }
        )
        call = omie.prepare_account_entry_call(
            client,
            "update",
            {
                "selector": {"id": 44},
                "data": {
                    "nature": "expense",
                    "amount": "120.00",
                    "date": "05/08/2026",
                    "document_number": None,
                    "observation": None,
                    "departments": [],
                },
            },
            "entry-update-1",
        )[0]

        self.assertEqual(call.params["nCodLanc"], 44)
        self.assertEqual(call.params["cabecalho"]["nValorLanc"], 120)
        self.assertEqual(call.params["cabecalho"]["dDtLanc"], "05/08/2026")
        self.assertNotIn("cCodIntLanc", call.params)
        self.assertNotIn("cNumDoc", call.params["detalhes"])
        self.assertNotIn("cObs", call.params["detalhes"])
        self.assertNotIn("departamentos", call.params)

        with self.assertRaisesRegex(omie.OmieToolError, "reconcile"):
            omie.prepare_account_entry_call(
                client,
                "update",
                {
                    "selector": {"id": 44},
                    "data": {"nature": "expense", "reconcile": True},
                },
                "entry-update-reconcile",
            )

    def test_account_entry_blocks_nature_change_and_non_manual_origins(self):
        expense = {
            "nCodLanc": 44,
            "cabecalho": {"nCodCC": 5, "dDtLanc": "04/08/2026", "nValorLanc": 100},
            "detalhes": {"cCodCateg": "2.01.01", "cTipo": "DIN"},
            "diversos": {"cOrigem": "EXTP", "cNatureza": "P"},
        }
        client = FakeOperationClient({"ConsultaLancCC": expense})
        with self.assertRaises(omie.OmieToolError):
            omie.prepare_account_entry_call(
                client,
                "update",
                {
                    "selector": {"id": 44},
                    "data": {"nature": "revenue", "observation": "Conversão"},
                },
                "entry-update-2",
            )

        transfer = {**expense, "diversos": {"cOrigem": "TRAP", "cNatureza": "P"}}
        client = FakeOperationClient({"ConsultaLancCC": transfer})
        with self.assertRaises(omie.OmieToolError):
            omie.prepare_account_entry_call(
                client,
                "delete",
                {"selector": {"id": 44}, "confirm_delete": True},
                "entry-delete-1",
            )

    def test_account_entry_delete_requires_confirmation_and_accepts_manual_entry(self):
        current = {
            "nCodLanc": 44,
            "diversos": {"cOrigem": "EXTR", "cNatureza": "R"},
        }
        client = FakeOperationClient({"ConsultaLancCC": current})
        with self.assertRaises(omie.OmieToolError):
            omie.prepare_account_entry_call(
                client,
                "delete",
                {"selector": {"id": 44}, "confirm_delete": False},
                "entry-delete-2",
            )
        call = omie.prepare_account_entry_call(
            client,
            "delete",
            {"selector": {"id": 44}, "confirm_delete": True},
            "entry-delete-3",
        )[0]
        self.assertEqual(call.method, "ExcluirLancCC")
        self.assertEqual(call.params, {"nCodLanc": 44})

    def test_account_entry_rejects_non_positive_amount_and_stale_allocations(self):
        category = {
            "codigo": "2.01.01",
            "conta_inativa": "N",
            "conta_despesa": "S",
            "conta_receita": "N",
            "transferencia": "N",
            "totalizadora": "N",
            "nao_exibir": "N",
        }
        client = FakeOperationClient(
            {
                "ConsultarContaCorrente": {"nCodCC": 5, "inativo": "N"},
                "ConsultarCategoria": category,
            }
        )
        with self.assertRaises(omie.OmieToolError):
            omie.account_entry_payload(
                client,
                {},
                {
                    "nature": "expense",
                    "account": {"id": 5},
                    "date": "04/08/2026",
                    "amount": "0.00",
                    "document_type": "DIN",
                    "category": {"code": "2.01.01"},
                },
                integration_id="cw-entry",
            )

        current = {
            "nCodLanc": 44,
            "cabecalho": {"nCodCC": 5, "dDtLanc": "04/08/2026", "nValorLanc": 100},
            "detalhes": {
                "aCodCateg": [
                    {"cCodCateg": "2.01.01", "nValor": 60, "nPerc": 60},
                    {"cCodCateg": "2.01.02", "nValor": 40, "nPerc": 40},
                ],
                "cTipo": "DIN",
            },
            "diversos": {"cOrigem": "EXTP", "cNatureza": "P"},
        }
        def category_by_code(params):
            return {**category, "codigo": params["codigo"]}
        client = FakeOperationClient(
            {"ConsultaLancCC": current, "ConsultarCategoria": category_by_code}
        )
        with self.assertRaises(omie.OmieToolError):
            omie.prepare_account_entry_call(
                client,
                "update",
                {
                    "selector": {"id": 44},
                    "data": {"nature": "expense", "amount": "120.00"},
                },
                "entry-update-rateio",
            )

    def test_account_entry_delete_recovers_unknown_transport_state(self):
        class DeleteRecoveryClient(FakeOperationClient):
            def __init__(self):
                super().__init__()
                self.consultations = 0

            def call(self, service, method, params):
                if method == "ConsultaLancCC":
                    self.consultations += 1
                    if self.consultations == 1:
                        return {
                            "nCodLanc": 44,
                            "diversos": {"cOrigem": "EXTP", "cNatureza": "P"},
                        }
                    raise omie.OmieApiError(None, "não encontrado")
                if method == "ExcluirLancCC":
                    raise omie.OmieUnknownStateError("timeout")
                return super().call(service, method, params)

        document = {
            "schema_version": 1,
            "request_id": "entry-delete-recovery",
            "selector": {"id": 44},
            "confirm_delete": True,
        }
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "input.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            args = argparse.Namespace(
                profile="empresa",
                input_stdin=False,
                input_file=str(path),
                resource="account-entries",
                operation="delete",
                dry_run=False,
            )
            result = omie.execute_mutation(DeleteRecoveryClient(), args)

        self.assertEqual(result["results"][0]["status"], "recovered_after_timeout")
        self.assertEqual(result["results"][0]["item"], {"deleted": True})

    def test_account_entry_dry_run_never_performs_write(self):
        def entry_show(_params):
            raise omie.OmieApiError(
                500,
                "ERROR: Lançamento de Conta Corrente não cadastrado para o "
                "Código de Integração [cw-fa8aa68284fe12f1b] !",
                fault_code="SOAP-ENV:Client-103",
            )

        client = FakeOperationClient(
            {
                "ConsultarContaCorrente": {"nCodCC": 5, "inativo": "N"},
                "ConsultarCategoria": {
                    "codigo": "2.01.01",
                    "conta_inativa": "N",
                    "conta_despesa": "S",
                    "conta_receita": "N",
                    "transferencia": "N",
                    "totalizadora": "N",
                    "nao_exibir": "N",
                },
                "ConsultaLancCC": entry_show,
            }
        )
        document = {
            "schema_version": 1,
            "request_id": "entry-dry-run",
            "data": {
                "nature": "expense",
                "account": {"id": 5},
                "date": "04/08/2026",
                "amount": "10.00",
                "document_type": "DIN",
                "category": {"code": "2.01.01"},
            },
        }
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "input.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            args = argparse.Namespace(
                profile="empresa",
                input_stdin=False,
                input_file=str(path),
                resource="account-entries",
                operation="create",
                dry_run=True,
            )
            result = omie.execute_mutation(client, args)

        self.assertTrue(result["dry_run"])
        self.assertEqual(result["calls"][0]["method"], "IncluirLancCC")
        self.assertFalse(any(method == "IncluirLancCC" for _, method, _ in client.calls))

    def test_account_entry_prepare_writes_closed_job_envelope_idempotently(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            derived = root / "data" / "telegram" / "jobs" / "7" / "derived"
            derived.mkdir(parents=True)
            args = argparse.Namespace(
                request_id="telegram:omie:entry-7",
                nature="expense",
                account_id=5,
                date="04/08/2026",
                amount="450.00",
                document_type="99999",
                category_code="2.01.01",
                project_id=9,
                counterparty_id=None,
                department=["ADM:100"],
                document_number=None,
                observation="Transporte autorizado",
            )
            environment = {"COWORKER_JOB_DERIVED": str(derived)}

            created = omie.prepare_account_entry_envelope(
                args, project_root=root, environment=environment
            )
            repeated = omie.prepare_account_entry_envelope(
                args, project_root=root, environment=environment
            )
            document = omie.parse_input_document(
                Path(created["path"]).read_text(encoding="utf-8")
            )
            def entry_show(_params):
                raise omie.OmieApiError(None, "não encontrado")

            client = FakeOperationClient(
                {
                    "ConsultarContaCorrente": {"nCodCC": 5, "inativo": "N"},
                    "ConsultarCategoria": {
                        "codigo": "2.01.01",
                        "conta_inativa": "N",
                        "conta_despesa": "S",
                        "conta_receita": "N",
                        "transferencia": "N",
                        "totalizadora": "N",
                        "nao_exibir": "N",
                    },
                    "ConsultarProjeto": {
                        "codigo": 9,
                        "nome": "Operação",
                        "inativo": "N",
                    },
                    "ConsultarDepartamento": {
                        "codigo": "ADM",
                        "descricao": "Administrativo",
                        "inativo": "N",
                    },
                    "ConsultaLancCC": entry_show,
                }
            )
            dry_run = omie.execute_mutation(
                client,
                argparse.Namespace(
                    profile="empresa",
                    input_stdin=False,
                    input_file=created["path"],
                    resource="account-entries",
                    operation="create",
                    dry_run=True,
                ),
            )

        self.assertTrue(created["created"])
        self.assertFalse(repeated["created"])
        self.assertEqual(created["path"], repeated["path"])
        self.assertEqual(document["request_id"], "telegram:omie:entry-7")
        self.assertEqual(document["data"]["account"], {"id": 5})
        self.assertEqual(document["data"]["project"], {"id": 9})
        self.assertNotIn("counterparty", document["data"])
        self.assertEqual(
            document["data"]["departments"],
            [{"code": "ADM", "percentage": 100}],
        )
        self.assertTrue(dry_run["dry_run"])
        self.assertEqual(dry_run["calls"][0]["method"], "IncluirLancCC")
        self.assertEqual(
            dry_run["calls"][0]["params"]["departamentos"],
            [{"cCodDep": "ADM", "nPerDep": 100}],
        )
        self.assertEqual(
            dry_run["calls"][0]["params"]["detalhes"]["cCodCateg"],
            "2.01.01",
        )
        self.assertNotIn("aCodCateg", dry_run["calls"][0]["params"]["detalhes"])

    def test_account_entry_prepare_refuses_overwrite_and_external_directory(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            derived = root / "data" / "telegram" / "jobs" / "7" / "derived"
            derived.mkdir(parents=True)
            args = argparse.Namespace(
                request_id="telegram:omie:entry-7",
                nature="expense",
                account_id=5,
                date="04/08/2026",
                amount="450.00",
                document_type="99999",
                category_code="2.01.01",
                project_id=None,
                counterparty_id=None,
                department=[],
                document_number=None,
                observation=None,
            )
            environment = {"COWORKER_JOB_DERIVED": str(derived)}
            omie.prepare_account_entry_envelope(
                args, project_root=root, environment=environment
            )
            args.amount = "451.00"
            with self.assertRaises(omie.OmieToolError):
                omie.prepare_account_entry_envelope(
                    args, project_root=root, environment=environment
                )

            external = Path(temporary) / "external" / "derived"
            external.mkdir(parents=True)
            with self.assertRaises(omie.OmieToolError):
                omie.prepare_account_entry_envelope(
                    args,
                    project_root=root,
                    environment={"COWORKER_JOB_DERIVED": str(external)},
                )

    def test_account_entry_prepare_parser_uses_typed_fields(self):
        args = omie.build_parser().parse_args(
            [
                "account-entries",
                "prepare",
                "--request-id",
                "telegram:omie:entry-7",
                "--nature",
                "expense",
                "--account-id",
                "5",
                "--date",
                "04/08/2026",
                "--amount",
                "450.00",
                "--document-type",
                "99999",
                "--category-code",
                "2.01.01",
                "--department",
                "INFRA:60",
                "--department",
                "OPERACOES:40",
            ]
        )

        self.assertIs(args.handler, omie.execute_prepare_account_entry)
        self.assertEqual(args.account_id, 5)
        self.assertEqual(args.category_code, "2.01.01")
        self.assertEqual(args.department, ["INFRA:60", "OPERACOES:40"])

    def test_account_entry_prepare_validates_department_allocations(self):
        self.assertEqual(
            omie.prepare_department_allocations(["INFRA:60", "OPERACOES:40"]),
            [
                {"code": "INFRA", "percentage": 60},
                {"code": "OPERACOES", "percentage": 40},
            ],
        )
        for values in (
            ["INFRA:99"],
            ["INFRA:0", "OPERACOES:100"],
            ["INFRA:101"],
            [":100"],
            ["INFRA"],
            ["INFRA:50", "INFRA:50"],
        ):
            with self.subTest(values=values), self.assertRaises(omie.OmieToolError):
                omie.prepare_department_allocations(values)

    def test_account_entry_prepare_invalid_departments_do_not_create_envelope(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            derived = root / "data" / "telegram" / "jobs" / "8" / "derived"
            derived.mkdir(parents=True)
            args = argparse.Namespace(
                request_id="telegram:omie:entry-8",
                nature="expense",
                account_id=5,
                date="04/08/2026",
                amount="450.00",
                document_type="99999",
                category_code="2.01.01",
                project_id=None,
                counterparty_id=None,
                department=["INFRA:70", "OPERACOES:20"],
                document_number=None,
                observation=None,
            )

            with self.assertRaises(omie.OmieToolError):
                omie.prepare_account_entry_envelope(
                    args,
                    project_root=root,
                    environment={"COWORKER_JOB_DERIVED": str(derived)},
                )

            self.assertEqual(list(derived.iterdir()), [])

    def test_account_entry_prepare_main_does_not_load_credentials(self):
        argv = [
            "omie.py",
            "account-entries",
            "prepare",
            "--request-id",
            "telegram:omie:entry-7",
            "--nature",
            "expense",
            "--account-id",
            "5",
            "--date",
            "04/08/2026",
            "--amount",
            "450.00",
            "--document-type",
            "99999",
            "--category-code",
            "2.01.01",
        ]
        with (
            patch.object(sys, "argv", argv),
            patch.object(
                omie,
                "prepare_account_entry_envelope",
                return_value={"ok": True, "created": True, "path": "input.json"},
            ),
            patch.object(
                omie,
                "load_config",
                side_effect=AssertionError("config must not be loaded"),
            ),
            patch.object(omie, "print_json"),
        ):
            result = omie.main()

        self.assertEqual(result, 0)

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
