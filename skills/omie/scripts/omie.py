#!/usr/bin/env python3
"""Consulta e altera contratos permitidos da API Omie sem expor credenciais."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = PROJECT_ROOT / "data" / "config" / "omie.toml"
EXAMPLE_CONFIG = PROJECT_ROOT / "config" / "omie.example.toml"
PROJECT_SCRIPTS = PROJECT_ROOT / "scripts"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(PROJECT_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(PROJECT_SCRIPTS))

from credential_vault import VaultToolError, read_entry_credentials  # noqa: E402
from interfaces.telegram.job_context import (  # noqa: E402
    JobContextError,
    write_job_json,
)
from integration_profiles import (  # noqa: E402
    IntegrationProfileError,
    resolve_credential_ref,
)
from integration_config import missing_config_message  # noqa: E402


ALLOWED_API_HOST = "app.omie.com.br"
SENSITIVE_KEYS = {
    "app_key",
    "app_secret",
    "api_key",
    "api_secret",
    "password",
    "senha",
    "secret",
    "token",
    "csc_homologacao",
    "csc_producao",
    "ct_eskey",
    "ct_espass",
}


class OmieToolError(Exception):
    """Erro seguro para apresentação ao agente ou à pessoa usuária."""


class OmieApiError(OmieToolError):
    """Erro sanitizado devolvido pela API Omie."""

    def __init__(
        self,
        status: int | None,
        message: str,
        *,
        fault_code: str | None = None,
    ) -> None:
        self.status = status
        self.message = message
        self.fault_code = fault_code
        prefix = f"Omie HTTP {status}" if status is not None else "Omie"
        code = f" ({fault_code})" if fault_code else ""
        super().__init__(f"{prefix}{code}: {message}")


class OmieUnknownStateError(OmieToolError):
    """Indica que uma escrita pode ter sido aplicada apesar da falha de transporte."""


@dataclass(frozen=True)
class OmieConfig:
    """Configuração local não confidencial."""

    api_base: str
    credential_ref: str
    timeout_seconds: int
    page_size: int
    max_pages: int


@dataclass(frozen=True)
class ServiceSpec:
    """Contrato fechado de um serviço permitido."""

    resource: str
    path: str
    list_call: str
    list_key: str
    show_call: str
    selectors: tuple[tuple[str, str, str], ...]
    mutation_calls: tuple[tuple[str, str], ...] = ()
    page_field: str = "pagina"
    page_size_field: str = "registros_por_pagina"
    response_page_field: str = "pagina"
    response_pages_field: str = "total_de_paginas"
    response_records_field: str = "registros"
    response_total_field: str = "total_de_registros"

    def mutation_call(self, operation: str) -> str:
        """Resolve uma operação de escrita sem aceitar chamadas arbitrárias."""
        for name, method in self.mutation_calls:
            if name == operation:
                return method
        raise OmieToolError(
            f"Operação '{operation}' não permitida para '{self.resource}'."
        )

    def allows(self, method: str) -> bool:
        """Informa se um método pertence à allowlist imutável do serviço."""
        return method in {
            self.list_call,
            self.show_call,
            *(call for _, call in self.mutation_calls),
        }


SERVICE_SPECS = {
    "companies": ServiceSpec(
        "companies",
        "geral/empresas/",
        "ListarEmpresas",
        "empresas_cadastro",
        "ConsultarEmpresa",
        (("id", "codigo_empresa", "Código interno da empresa."),),
    ),
    "customers": ServiceSpec(
        "customers",
        "geral/clientes/",
        "ListarClientes",
        "clientes_cadastro",
        "ConsultarCliente",
        (
            ("id", "codigo_cliente_omie", "Código interno do cliente."),
            (
                "integration-id",
                "codigo_cliente_integracao",
                "Código de integração do cliente.",
            ),
        ),
        (
            ("create", "IncluirCliente"),
            ("update", "AlterarCliente"),
            ("deactivate", "AlterarCliente"),
            ("delete", "ExcluirCliente"),
        ),
    ),
    "projects": ServiceSpec(
        "projects",
        "geral/projetos/",
        "ListarProjetos",
        "cadastro",
        "ConsultarProjeto",
        (
            ("id", "codigo", "Código interno do projeto."),
            ("integration-id", "codInt", "Código de integração do projeto."),
        ),
        (
            ("create", "IncluirProjeto"),
            ("update", "AlterarProjeto"),
            ("deactivate", "AlterarProjeto"),
            ("delete", "ExcluirProjeto"),
        ),
    ),
    "categories": ServiceSpec(
        "categories",
        "geral/categorias/",
        "ListarCategorias",
        "categoria_cadastro",
        "ConsultarCategoria",
        (("code", "codigo", "Código da categoria."),),
    ),
    "departments": ServiceSpec(
        "departments",
        "geral/departamentos/",
        "ListarDepartamentos",
        "departamentos",
        "ConsultarDepartamento",
        (("code", "codigo", "Código do departamento."),),
    ),
    "current-accounts": ServiceSpec(
        "current-accounts",
        "geral/contacorrente/",
        "ListarContasCorrentes",
        "ListarContasCorrentes",
        "ConsultarContaCorrente",
        (
            ("id", "nCodCC", "Código interno da conta corrente."),
            ("integration-id", "cCodCCInt", "Código de integração da conta."),
        ),
    ),
    "products": ServiceSpec(
        "products",
        "geral/produtos/",
        "ListarProdutos",
        "produto_servico_cadastro",
        "ConsultarProduto",
        (
            ("id", "codigo_produto", "Código interno do produto."),
            (
                "integration-id",
                "codigo_produto_integracao",
                "Código de integração do produto.",
            ),
            ("code", "codigo", "Código visível ou SKU do produto."),
        ),
    ),
    "payables": ServiceSpec(
        "payables",
        "financas/contapagar/",
        "ListarContasPagar",
        "conta_pagar_cadastro",
        "ConsultarContaPagar",
        (
            ("id", "codigo_lancamento_omie", "Código interno do lançamento."),
            (
                "integration-id",
                "codigo_lancamento_integracao",
                "Código de integração do lançamento.",
            ),
        ),
        (
            ("create", "IncluirContaPagar"),
            ("update", "AlterarContaPagar"),
            ("delete", "ExcluirContaPagar"),
            ("pay", "LancarPagamento"),
            ("cancel-payment", "CancelarPagamento"),
        ),
    ),
    "receivables": ServiceSpec(
        "receivables",
        "financas/contareceber/",
        "ListarContasReceber",
        "conta_receber_cadastro",
        "ConsultarContaReceber",
        (
            ("id", "codigo_lancamento_omie", "Código interno do lançamento."),
            (
                "integration-id",
                "codigo_lancamento_integracao",
                "Código de integração do lançamento.",
            ),
        ),
        (
            ("create", "IncluirContaReceber"),
            ("update", "AlterarContaReceber"),
            ("delete", "ExcluirContaReceber"),
            ("receive", "LancarRecebimento"),
            ("cancel-receipt", "CancelarRecebimento"),
            ("reconcile", "ConciliarRecebimento"),
            ("unreconcile", "DesconciliarRecebimento"),
        ),
    ),
    "transfers": ServiceSpec(
        "transfers",
        "financas/contacorrentelancamentos/",
        "ListarLancCC",
        "listaLancamentos",
        "ConsultaLancCC",
        (
            ("id", "nCodLanc", "Código interno do lançamento."),
            ("integration-id", "cCodIntLanc", "Código de integração do lançamento."),
        ),
        (
            ("create", "IncluirLancCC"),
            ("update", "AlterarLancCC"),
            ("delete", "ExcluirLancCC"),
        ),
        "nPagina",
        "nRegPorPagina",
        "nPagina",
        "nTotPaginas",
        "nRegistros",
        "nTotRegistros",
    ),
    "account-entries": ServiceSpec(
        "account-entries",
        "financas/contacorrentelancamentos/",
        "ListarLancCC",
        "listaLancamentos",
        "ConsultaLancCC",
        (
            ("id", "nCodLanc", "Código interno do lançamento."),
            ("integration-id", "cCodIntLanc", "Código de integração do lançamento."),
        ),
        (
            ("create", "IncluirLancCC"),
            ("update", "AlterarLancCC"),
            ("delete", "ExcluirLancCC"),
        ),
        "nPagina",
        "nRegPorPagina",
        "nPagina",
        "nTotPaginas",
        "nRegistros",
        "nTotRegistros",
    ),
    "sales-orders": ServiceSpec(
        "sales-orders",
        "produtos/pedido/",
        "ListarPedidos",
        "pedido_venda_produto",
        "ConsultarPedido",
        (
            ("id", "codigo_pedido", "Código interno do pedido."),
            (
                "integration-id",
                "codigo_pedido_integracao",
                "Código de integração do pedido.",
            ),
        ),
    ),
    "service-orders": ServiceSpec(
        "service-orders",
        "servicos/os/",
        "ListarOS",
        "osCadastro",
        "ConsultarOS",
        (
            ("id", "nCodOS", "Código interno da ordem de serviço."),
            (
                "integration-id",
                "cCodIntOS",
                "Código de integração da ordem de serviço.",
            ),
            ("number", "cNumOS", "Número visível da ordem de serviço."),
        ),
    ),
}


def load_config(path: Path, profile: str | None = None) -> OmieConfig:
    """Carrega e valida a configuração TOML."""
    try:
        values = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise OmieToolError(
            missing_config_message("omie", path)
        ) from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise OmieToolError(
            f"Não foi possível carregar a configuração '{path}'."
        ) from exc

    api_base = str(values.get("api_base", "")).rstrip("/")
    parsed = urllib.parse.urlparse(api_base)
    try:
        port = parsed.port
    except ValueError as exc:
        raise OmieToolError("'api_base' contém uma porta inválida.") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != ALLOWED_API_HOST
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path.rstrip("/") != "/api/v1"
        or parsed.query
        or parsed.fragment
    ):
        raise OmieToolError(
            f"'api_base' deve ser 'https://{ALLOWED_API_HOST}/api/v1'."
        )
    try:
        _, credential_ref = resolve_credential_ref(values, profile)
    except IntegrationProfileError as exc:
        raise OmieToolError(str(exc)) from exc

    timeout_seconds = values.get("timeout_seconds", 30)
    page_size = values.get("page_size", 100)
    max_pages = values.get("max_pages", 20)
    if not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= 120:
        raise OmieToolError("'timeout_seconds' deve estar entre 1 e 120.")
    if not isinstance(page_size, int) or not 1 <= page_size <= 100:
        raise OmieToolError("'page_size' deve estar entre 1 e 100.")
    if not isinstance(max_pages, int) or not 1 <= max_pages <= 100:
        raise OmieToolError("'max_pages' deve estar entre 1 e 100.")
    return OmieConfig(
        api_base,
        credential_ref,
        timeout_seconds,
        page_size,
        max_pages,
    )


def normalized_key(key: Any) -> str:
    """Normaliza um nome de campo para classificação de segurança."""
    return re.sub(r"[^a-z0-9]+", "_", str(key).lower()).strip("_")


def sensitive_key(key: Any) -> bool:
    """Identifica campos que não podem ser devolvidos."""
    normalized = normalized_key(key)
    return (
        normalized in SENSITIVE_KEYS
        or normalized.endswith(("_password", "_senha", "_secret", "_token"))
        or "certificado" in normalized
    )


def sanitize_payload(value: Any) -> Any:
    """Remove campos sensíveis de objetos antes da saída."""
    if isinstance(value, dict):
        return {
            str(key): sanitize_payload(child)
            for key, child in value.items()
            if not sensitive_key(key)
        }
    if isinstance(value, list):
        return [sanitize_payload(item) for item in value]
    return value


def first_message(payload: Any) -> tuple[str, str | None]:
    """Extrai mensagem e código sem devolver o corpo integral."""
    if isinstance(payload, dict):
        code = payload.get("faultcode") or payload.get("code")
        for key in (
            "faultstring",
            "message",
            "description",
            "descricao_status",
            "cDesStatus",
        ):
            message = payload.get(key)
            if isinstance(message, str) and message.strip():
                return message.strip()[:500], str(code) if code is not None else None
    return "Erro sem detalhes.", None


class OmieClient:
    """Cliente mínimo e restrito aos serviços permitidos da Omie."""

    def __init__(
        self,
        config: OmieConfig,
        app_key: str,
        app_secret: str,
        *,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        if not app_key or not app_secret:
            raise OmieToolError("As credenciais da Omie estão vazias.")
        self.config = config
        self._app_key = app_key
        self._app_secret = app_secret
        self._opener = opener

    def close(self) -> None:
        """Descarta referências às credenciais mantidas pelo processo."""
        self._app_key = ""
        self._app_secret = ""

    def redact(self, message: str) -> str:
        """Remove credenciais de uma mensagem defensivamente."""
        redacted = message
        for secret in (self._app_key, self._app_secret):
            if secret:
                redacted = redacted.replace(secret, "[REDACTED]")
        return redacted[:500]

    def call(
        self,
        service: ServiceSpec,
        method: str,
        params: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Executa uma chamada JSON POST em um serviço permitido."""
        if service not in SERVICE_SPECS.values():
            raise OmieToolError("Serviço não permitido.")
        if not service.allows(method):
            raise OmieToolError("Método não permitido para este serviço.")

        url = f"{self.config.api_base}/{service.path}"
        payload = {
            "call": method,
            "app_key": self._app_key,
            "app_secret": self._app_secret,
            "param": [dict(params)],
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "Coworker-Omie/1",
            },
        )
        try:
            with self._opener(
                request,
                timeout=self.config.timeout_seconds,
            ) as response:
                raw_payload = response.read()
                status = getattr(response, "status", 200)
        except urllib.error.HTTPError as exc:
            raw_payload = exc.read()
            try:
                response_payload = json.loads(raw_payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                response_payload = None
            message, fault_code = first_message(response_payload)
            if exc.code == 425:
                message = (
                    "A API bloqueou temporariamente esta combinação de IP, App Key "
                    "e método após chamadas inválidas. Aguarde 30 minutos."
                )
            elif exc.code == 429:
                message = (
                    "Limite de requisições atingido. Aguarde antes de tentar novamente."
                )
            raise OmieApiError(
                exc.code,
                self.redact(message),
                fault_code=fault_code,
            ) from None
        except TimeoutError:
            if method in {call for _, call in service.mutation_calls}:
                raise OmieUnknownStateError(
                    "A conexão expirou após o envio. O estado remoto é desconhecido; "
                    "consulte pelo identificador de integração antes de repetir."
                ) from None
            raise OmieToolError("A conexão com a Omie expirou.") from None
        except urllib.error.URLError as exc:
            if (
                method in {call for _, call in service.mutation_calls}
                and isinstance(exc.reason, TimeoutError)
            ):
                raise OmieUnknownStateError(
                    "A conexão expirou após o envio. O estado remoto é desconhecido; "
                    "consulte pelo identificador de integração antes de repetir."
                ) from None
            raise OmieToolError(
                f"Não foi possível conectar à Omie: {self.redact(str(exc.reason))}"
            ) from None

        if not raw_payload:
            raise OmieToolError(f"A Omie devolveu uma resposta vazia (HTTP {status}).")
        try:
            response_payload = json.loads(raw_payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OmieToolError(
                f"A Omie devolveu JSON inválido (HTTP {status})."
            ) from exc
        if not isinstance(response_payload, dict):
            raise OmieToolError("A Omie não devolveu um objeto JSON válido.")
        if "faultstring" in response_payload or "faultcode" in response_payload:
            message, fault_code = first_message(response_payload)
            raise OmieApiError(
                status,
                self.redact(message),
                fault_code=fault_code,
            )
        return response_payload

    def list_page(
        self,
        service: ServiceSpec,
        *,
        page: int,
        params: Mapping[str, Any],
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        """Consulta uma página e valida a forma da resposta."""
        request_params = dict(params)
        request_params.update(
            {
                service.page_field: page,
                service.page_size_field: self.config.page_size,
            }
        )
        payload = self.call(service, service.list_call, request_params)
        raw_items = payload.get(service.list_key, [])
        if not isinstance(raw_items, list):
            raise OmieToolError(
                f"A resposta não contém a lista '{service.list_key}'."
            )
        items = [item for item in raw_items if isinstance(item, dict)]
        metadata = {
            "page": integer_or(payload.get(service.response_page_field), page),
            "total_pages": integer_or(
                payload.get(service.response_pages_field), page
            ),
            "records": integer_or(
                payload.get(service.response_records_field), len(items)
            ),
            "total_records": integer_or(
                payload.get(service.response_total_field),
                len(items),
            ),
        }
        return items, metadata


def integer_or(value: Any, default: int) -> int:
    """Converte inteiros devolvidos como número ou texto."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def validate_date(value: str) -> str:
    """Valida datas no formato exigido pela Omie."""
    try:
        datetime.strptime(value, "%d/%m/%Y")
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Use data no formato DD/MM/AAAA.") from exc
    return value


def validate_date_range(
    start: str | None,
    end: str | None,
    label: str,
) -> None:
    """Recusa intervalos invertidos antes de consumir a API."""
    if start and end:
        start_date = datetime.strptime(start, "%d/%m/%Y")
        end_date = datetime.strptime(end, "%d/%m/%Y")
        if start_date > end_date:
            raise OmieToolError(
                f"A data inicial de {label} não pode ser posterior à final."
            )


def pick(source: Mapping[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    """Seleciona somente campos conhecidos como apropriados para saída."""
    return {key: source.get(key) for key in keys if key in source}


def summarize(resource: str, item: dict[str, Any]) -> dict[str, Any]:
    """Produz uma visão operacional sem campos secretos ou corpos excessivos."""
    if resource == "companies":
        return pick(
            item,
            (
                "codigo_empresa",
                "codigo_empresa_integracao",
                "cnpj",
                "razao_social",
                "nome_fantasia",
                "cidade",
                "estado",
                "email",
                "inativa",
                "gera_nfe",
                "gera_nfse",
            ),
        )
    if resource == "customers":
        return pick(
            item,
            (
                "codigo_cliente_omie",
                "codigo_cliente_integracao",
                "cnpj_cpf",
                "razao_social",
                "nome_fantasia",
                "email",
                "telefone1_ddd",
                "telefone1_numero",
                "cidade",
                "estado",
                "inativo",
                "bloquear_faturamento",
            ),
        )
    if resource == "projects":
        return pick(item, ("codigo", "codInt", "nome", "inativo"))
    if resource == "categories":
        return pick(
            item,
            (
                "codigo",
                "descricao",
                "descricao_padrao",
                "tipo_categoria",
                "conta_inativa",
                "conta_despesa",
                "conta_receita",
                "transferencia",
                "totalizadora",
                "nao_exibir",
            ),
        )
    if resource == "departments":
        return pick(
            item,
            ("codigo", "descricao", "estrutura", "inativo", "nivel_totalizador"),
        )
    if resource == "current-accounts":
        return pick(
            item,
            (
                "nCodCC",
                "cCodCCInt",
                "descricao",
                "tipo_conta_corrente",
                "codigo_banco",
                "inativo",
            ),
        )
    if resource == "products":
        return pick(
            item,
            (
                "codigo_produto",
                "codigo_produto_integracao",
                "codigo",
                "descricao",
                "unidade",
                "ncm",
                "ean",
                "valor_unitario",
                "quantidade_estoque",
                "inativo",
                "tipoItem",
            ),
        )
    if resource in ("payables", "receivables"):
        return pick(
            item,
            (
                "codigo_lancamento_omie",
                "codigo_lancamento_integracao",
                "codigo_cliente_fornecedor",
                "data_emissao",
                "data_vencimento",
                "data_previsao",
                "valor_documento",
                "valor_pago",
                "numero_documento",
                "numero_parcela",
                "codigo_categoria",
                "id_conta_corrente",
                "codigo_projeto",
                "distribuicao",
                "status_titulo",
            ),
        )
    if resource in ("transfers", "account-entries"):
        summary = {
            **pick(item, ("nCodLanc", "cCodIntLanc", "nCodAgrup")),
            "cabecalho": pick(
                item.get("cabecalho", {}) if isinstance(item.get("cabecalho"), dict) else {},
                ("nCodCC", "dDtLanc", "nValorLanc"),
            ),
            "detalhes": pick(
                item.get("detalhes", {}) if isinstance(item.get("detalhes"), dict) else {},
                (
                    "cCodCateg",
                    "aCodCateg",
                    "cTipo",
                    "cNumDoc",
                    "nCodCliente",
                    "nCodProjeto",
                    "cObs",
                ),
            ),
            "departamentos": item.get("departamentos", []),
            "diversos": pick(
                item.get("diversos", {})
                if isinstance(item.get("diversos"), dict)
                else {},
                ("cOrigem", "cNatureza", "dDtConc"),
            ),
            "info": pick(
                item.get("info", {}) if isinstance(item.get("info"), dict) else {},
                ("cImpAPI",),
            ),
        }
        if resource == "transfers":
            summary["transferencia"] = pick(
                item.get("transferencia", {})
                if isinstance(item.get("transferencia"), dict)
                else {},
                ("nCodCCDestino",),
            )
        return summary
    if resource == "sales-orders":
        header = item.get("cabecalho")
        total = item.get("total_pedido")
        summary = pick(
            header if isinstance(header, dict) else {},
            (
                "codigo_pedido",
                "codigo_pedido_integracao",
                "numero_pedido",
                "codigo_cliente",
                "data_previsao",
                "etapa",
                "codigo_vendedor",
                "quantidade_itens",
            ),
        )
        if isinstance(total, dict):
            summary["total"] = pick(
                total,
                ("valor_mercadorias", "valor_total_pedido"),
            )
        return summary
    if resource == "service-orders":
        header = item.get("Cabecalho")
        return pick(
            header if isinstance(header, dict) else {},
            (
                "nCodOS",
                "cCodIntOS",
                "cNumOS",
                "nCodCli",
                "cCodIntCli",
                "dDtPrevisao",
                "cEtapa",
                "nCodVend",
                "nValorTotal",
            ),
        )
    raise OmieToolError(f"Resumo não definido para '{resource}'.")


def list_params(resource: str, args: argparse.Namespace) -> dict[str, Any]:
    """Monta apenas filtros documentados e explicitamente informados."""
    if resource == "transfers":
        return {}
    if resource == "account-entries":
        return {"cOrigem": ACCOUNT_ENTRY_NATURES[args.nature][1]}
    params: dict[str, Any] = {
        "apenas_importado_api": "S" if getattr(args, "only_api", False) else "N"
    }
    if getattr(args, "changed_from", None):
        params["filtrar_por_data_de"] = args.changed_from
    if getattr(args, "changed_to", None):
        params["filtrar_por_data_ate"] = args.changed_to
    if getattr(args, "only_created", False):
        params["filtrar_apenas_inclusao"] = "S"
    if getattr(args, "only_changed", False):
        params["filtrar_apenas_alteracao"] = "S"
    if resource == "products":
        params["filtrar_apenas_omiepdv"] = "N"
        if args.description:
            params["filtrar_apenas_descricao"] = f"%{args.description}%"
    if resource in ("payables", "receivables"):
        if args.issued_from:
            params["filtrar_por_emissao_de"] = args.issued_from
        if args.issued_to:
            params["filtrar_por_emissao_ate"] = args.issued_to
        if args.customer_id is not None:
            params["filtrar_cliente"] = args.customer_id
        if args.status:
            params["filtrar_por_status"] = args.status
    if resource == "sales-orders":
        if args.customer_id is not None:
            params["filtrar_por_cliente"] = args.customer_id
        if args.status:
            params["status_pedido"] = args.status
        params["apenas_resumo"] = "S"
    if resource == "service-orders":
        if args.customer_id is not None:
            params["filtrar_por_cliente"] = args.customer_id
        if args.status:
            params["filtrar_por_status"] = args.status
    return params


SCHEMA_VERSION = 1
ENVELOPE_FIELDS = {
    "schema_version",
    "request_id",
    "selector",
    "data",
    "confirm_delete",
    "items",
}
ITEM_FIELDS = {"selector", "data", "confirm_delete"}
CUSTOMER_FIELDS = {
    "razao_social",
    "nome_fantasia",
    "cnpj_cpf",
    "telefone1_ddd",
    "telefone1_numero",
    "telefone2_ddd",
    "telefone2_numero",
    "contato",
    "email",
    "homepage",
    "endereco",
    "endereco_numero",
    "bairro",
    "complemento",
    "estado",
    "cidade",
    "cidade_ibge",
    "cep",
    "codigo_pais",
    "inscricao_estadual",
    "inscricao_municipal",
    "inscricao_suframa",
    "optante_simples_nacional",
    "produtor_rural",
    "contribuinte",
    "observacao",
    "obs_detalhadas",
    "valor_limite_credito",
    "bloquear_faturamento",
    "tags",
    "inativo",
}
FINANCIAL_FIELDS = {
    "counterparty",
    "due_date",
    "amount",
    "category",
    "categories",
    "forecast_date",
    "current_account",
    "document_number",
    "installment_number",
    "document_type",
    "fiscal_document_number",
    "issue_date",
    "observation",
    "project",
    "departments",
    "installments",
}
TRANSFER_FIELDS = {
    "source_account",
    "destination_account",
    "date",
    "amount",
    "category",
    "categories",
    "counterparty",
    "project",
    "departments",
    "document_number",
    "observation",
}
ACCOUNT_ENTRY_FIELDS = {
    "nature",
    "account",
    "date",
    "amount",
    "document_type",
    "category",
    "categories",
    "counterparty",
    "project",
    "departments",
    "document_number",
    "observation",
}
ACCOUNT_ENTRY_DOCUMENT_TYPES = {
    "ADI",
    "BOL",
    "CRT",
    "CHQ",
    "CON",
    "CRE",
    "DRF",
    "DAS",
    "DEB",
    "DIN",
    "DOC",
    "GUIA",
    "PROT",
    "REC",
    "RPA",
    "TED",
    "99999",
}
ACCOUNT_ENTRY_NATURES = {
    "expense": ("P", "EXTP", "conta_despesa"),
    "revenue": ("R", "EXTR", "conta_receita"),
}
SETTLEMENT_FIELDS = {
    "current_account",
    "amount",
    "discount",
    "interest",
    "fine",
    "date",
    "observation",
    "reconcile",
}


@dataclass(frozen=True)
class PreparedCall:
    """Chamada validada que ainda não produziu efeito remoto."""

    service: ServiceSpec
    method: str
    params: dict[str, Any]
    resource: str
    operation: str
    request_id: str
    recovery_selector: dict[str, Any] | None = None
    already_applied: dict[str, Any] | None = None


def require_object(value: Any, label: str) -> dict[str, Any]:
    """Exige objeto JSON e devolve uma cópia mutável."""
    if not isinstance(value, dict):
        raise OmieToolError(f"'{label}' deve ser um objeto JSON.")
    return dict(value)


def reject_unknown_fields(
    value: Mapping[str, Any],
    allowed: set[str],
    label: str,
) -> None:
    """Recusa campos fora do contrato fechado."""
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise OmieToolError(
            f"Campos desconhecidos em '{label}': {', '.join(unknown)}."
        )


def require_nonempty_string(value: Any, label: str, max_length: int) -> str:
    """Valida texto obrigatório com limite documentado."""
    if not isinstance(value, str) or not value.strip():
        raise OmieToolError(f"'{label}' deve ser um texto não vazio.")
    normalized = value.strip()
    if len(normalized) > max_length:
        raise OmieToolError(f"'{label}' deve ter no máximo {max_length} caracteres.")
    return normalized


def request_identifier(value: Any) -> str:
    """Valida o identificador estável usado na idempotência."""
    identifier = require_nonempty_string(value, "request_id", 120)
    if not re.fullmatch(r"[A-Za-z0-9._:@/-]+", identifier):
        raise OmieToolError(
            "'request_id' aceita somente letras, números e . _ : @ / -."
        )
    return identifier


def decimal_value(
    value: Any,
    label: str,
    *,
    allow_zero: bool = False,
) -> Decimal:
    """Converte valor monetário sem aceitar mais de duas casas decimais."""
    if isinstance(value, bool) or value is None:
        raise OmieToolError(f"'{label}' deve ser um valor monetário.")
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise OmieToolError(f"'{label}' deve ser um valor monetário.") from exc
    if not decimal.is_finite() or decimal.as_tuple().exponent < -2:
        raise OmieToolError(f"'{label}' deve ter no máximo duas casas decimais.")
    if decimal < 0 or (decimal == 0 and not allow_zero):
        condition = "maior ou igual a zero" if allow_zero else "maior que zero"
        raise OmieToolError(f"'{label}' deve ser {condition}.")
    return decimal.quantize(Decimal("0.01"))


def decimal_number(value: Decimal) -> int | float:
    """Converte Decimal validado para número JSON estável."""
    if value == value.to_integral():
        return int(value)
    return float(format(value, ".2f"))


def date_value(value: Any, label: str) -> str:
    """Valida uma data de entrada no formato da API Omie."""
    if not isinstance(value, str):
        raise OmieToolError(f"'{label}' deve usar DD/MM/AAAA.")
    try:
        return validate_date(value)
    except argparse.ArgumentTypeError as exc:
        raise OmieToolError(f"'{label}' deve usar DD/MM/AAAA.") from exc


def yn_value(value: Any, label: str) -> str:
    """Valida campos S/N."""
    if value not in ("S", "N"):
        raise OmieToolError(f"'{label}' deve ser 'S' ou 'N'.")
    return str(value)


def derived_integration_id(
    request_id: str,
    namespace: str,
    *,
    max_length: int,
    sequence: int | None = None,
) -> str:
    """Deriva um código curto e determinístico sem revelar o pedido original."""
    seed = f"{namespace}:{request_id}"
    if sequence is not None:
        seed = f"{seed}:{sequence}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    prefix = "cw-"
    return f"{prefix}{digest[: max_length - len(prefix)]}"


def parse_input_document(raw: str) -> dict[str, Any]:
    """Carrega o envelope JSON preservando precisão decimal."""
    try:
        value = json.loads(raw, parse_float=Decimal)
    except json.JSONDecodeError as exc:
        raise OmieToolError(
            f"JSON inválido na linha {exc.lineno}, coluna {exc.colno}."
        ) from exc
    envelope = require_object(value, "entrada")
    reject_unknown_fields(envelope, ENVELOPE_FIELDS, "entrada")
    if envelope.get("schema_version") != SCHEMA_VERSION:
        raise OmieToolError(f"'schema_version' deve ser {SCHEMA_VERSION}.")
    envelope["request_id"] = request_identifier(envelope.get("request_id"))
    if "items" in envelope and any(
        key in envelope for key in ("selector", "data", "confirm_delete")
    ):
        raise OmieToolError(
            "Use 'items' ou os campos de uma operação única, nunca ambos."
        )
    if "items" in envelope:
        if not isinstance(envelope["items"], list) or not envelope["items"]:
            raise OmieToolError("'items' deve ser uma lista não vazia.")
        for index, item in enumerate(envelope["items"], 1):
            item_object = require_object(item, f"items[{index}]")
            reject_unknown_fields(item_object, ITEM_FIELDS, f"items[{index}]")
    return envelope


def load_input_document(args: argparse.Namespace) -> dict[str, Any]:
    """Lê uma única fonte de entrada aprovada pela CLI."""
    if args.input_stdin:
        raw = sys.stdin.read()
    else:
        try:
            raw = Path(args.input_file).expanduser().resolve().read_text(
                encoding="utf-8"
            )
        except OSError as exc:
            raise OmieToolError(
                f"Não foi possível ler o arquivo de entrada: {exc}."
            ) from exc
    return parse_input_document(raw)


def envelope_items(envelope: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Normaliza operação única e lote para o mesmo fluxo."""
    if "items" in envelope:
        return [dict(item) for item in envelope["items"]]
    return [
        {
            key: envelope[key]
            for key in ("selector", "data", "confirm_delete")
            if key in envelope
        }
    ]


def normalize_name(value: Any) -> str:
    """Normaliza somente caixa e espaços para busca exata."""
    if not isinstance(value, str):
        return ""
    return " ".join(value.split()).casefold()


def normalize_tax_id(value: Any) -> str:
    """Mantém somente dígitos para comparação de CPF/CNPJ."""
    return re.sub(r"\D", "", str(value))


def is_inactive(item: Mapping[str, Any]) -> bool:
    """Reconhece as variações oficiais do marcador de inatividade."""
    return any(
        item.get(key) == "S" for key in ("inativo", "inativa", "conta_inativa")
    )


def exact_selector(
    value: Any,
    allowed: set[str],
    label: str,
) -> dict[str, Any]:
    """Exige exatamente um identificador de referência."""
    selector = require_object(value, label)
    reject_unknown_fields(selector, allowed, label)
    present = [key for key in allowed if selector.get(key) not in (None, "")]
    if len(present) != 1:
        raise OmieToolError(f"'{label}' deve informar exatamente um seletor.")
    return {present[0]: selector[present[0]]}


def list_all_for_resolution(
    client: OmieClient,
    resource: str,
) -> list[dict[str, Any]]:
    """Percorre uma lista limitada para resolver nomes sem correspondência difusa."""
    service = SERVICE_SPECS[resource]
    items: list[dict[str, Any]] = []
    for page in range(1, client.config.max_pages + 1):
        page_items, metadata = client.list_page(service, page=page, params={})
        items.extend(page_items)
        if page >= metadata["total_pages"]:
            return items
    raise OmieToolError(
        f"A busca exata em '{resource}' excedeu o limite configurado de páginas. "
        "Use um código inequívoco."
    )


def resolve_reference(
    client: OmieClient,
    kind: str,
    value: Any,
    *,
    require_active: bool = True,
) -> tuple[Any, dict[str, Any]]:
    """Resolve uma referência por ID, integração ou nome exato."""
    definitions = {
        "customer": (
            "customers",
            {"id", "integration_id", "tax_id", "name"},
            {"id": "codigo_cliente_omie", "integration_id": "codigo_cliente_integracao"},
            "codigo_cliente_omie",
            ("razao_social", "nome_fantasia"),
        ),
        "project": (
            "projects",
            {"id", "integration_id", "name"},
            {"id": "codigo", "integration_id": "codInt"},
            "codigo",
            ("nome",),
        ),
        "category": (
            "categories",
            {"code", "name"},
            {"code": "codigo"},
            "codigo",
            ("descricao", "descricao_padrao"),
        ),
        "department": (
            "departments",
            {"code", "name"},
            {"code": "codigo"},
            "codigo",
            ("descricao",),
        ),
        "current_account": (
            "current-accounts",
            {"id", "integration_id", "name"},
            {"id": "nCodCC", "integration_id": "cCodCCInt"},
            "nCodCC",
            ("descricao",),
        ),
    }
    if kind not in definitions:
        raise OmieToolError(f"Tipo de referência desconhecido: '{kind}'.")
    resource, allowed, direct_fields, result_field, name_fields = definitions[kind]
    if kind in ("category", "department") and isinstance(value, str):
        value = {"code": value}
    selector = exact_selector(value, allowed, kind)
    key, expected = next(iter(selector.items()))
    service = SERVICE_SPECS[resource]
    if key in direct_fields:
        if key == "id" and (not isinstance(expected, int) or isinstance(expected, bool)):
            raise OmieToolError(f"'{kind}.id' deve ser inteiro.")
        item = client.call(service, service.show_call, {direct_fields[key]: expected})
        matches = [item]
    else:
        candidates = list_all_for_resolution(client, resource)
        if key == "tax_id":
            target = normalize_tax_id(expected)
            if len(target) not in (11, 14):
                raise OmieToolError("'customer.tax_id' deve conter CPF ou CNPJ válido.")
            matches = [
                item
                for item in candidates
                if normalize_tax_id(item.get("cnpj_cpf")) == target
            ]
        else:
            target_name = normalize_name(expected)
            if not target_name:
                raise OmieToolError(f"'{kind}.name' deve ser um texto não vazio.")
            matches = [
                item
                for item in candidates
                if any(normalize_name(item.get(field)) == target_name for field in name_fields)
            ]
    if len(matches) != 1:
        raise OmieToolError(
            f"A referência '{kind}' encontrou {len(matches)} correspondências; "
            "informe um código inequívoco."
        )
    item = matches[0]
    if require_active and is_inactive(item):
        raise OmieToolError(f"A referência '{kind}' está inativa.")
    identifier = item.get(result_field)
    if identifier in (None, ""):
        raise OmieToolError(f"A Omie não devolveu o código de '{kind}'.")
    return identifier, item


def allocation_payload(
    client: OmieClient,
    values: Any,
    total: Decimal,
    kind: str,
) -> list[dict[str, Any]]:
    """Valida e converte rateio de categoria ou departamento."""
    if not isinstance(values, list) or not values:
        raise OmieToolError(f"'{kind}s' deve ser uma lista não vazia.")
    entries: list[tuple[Any, str, Decimal]] = []
    modes: set[str] = set()
    for index, raw in enumerate(values, 1):
        item = require_object(raw, f"{kind}s[{index}]")
        reject_unknown_fields(
            item,
            {"code", "name", "amount", "percentage"},
            f"{kind}s[{index}]",
        )
        selector_keys = [key for key in ("code", "name") if item.get(key) not in (None, "")]
        if len(selector_keys) != 1:
            raise OmieToolError(
                f"'{kind}s[{index}]' deve informar exatamente 'code' ou 'name'."
            )
        allocation_keys = [
            key for key in ("amount", "percentage") if item.get(key) is not None
        ]
        if len(allocation_keys) != 1:
            raise OmieToolError(
                f"'{kind}s[{index}]' deve informar exatamente 'amount' ou 'percentage'."
            )
        mode = allocation_keys[0]
        modes.add(mode)
        identifier, _ = resolve_reference(
            client,
            kind,
            {selector_keys[0]: item[selector_keys[0]]},
        )
        value = decimal_value(item[mode], f"{kind}s[{index}].{mode}")
        entries.append((identifier, mode, value))
    if len(modes) != 1:
        raise OmieToolError(f"Não misture valores e percentuais em '{kind}s'.")
    mode = next(iter(modes))
    expected = total if mode == "amount" else Decimal("100.00")
    if sum((entry[2] for entry in entries), Decimal("0")) != expected:
        label = "valor do lançamento" if mode == "amount" else "100%"
        raise OmieToolError(f"O rateio de '{kind}s' deve fechar exatamente em {label}.")
    result: list[dict[str, Any]] = []
    for identifier, _, value in entries:
        if kind == "category":
            payload = {"codigo_categoria": identifier}
            payload["valor" if mode == "amount" else "percentual"] = decimal_number(value)
        else:
            payload = {"cCodDep": identifier}
            payload["nValDep" if mode == "amount" else "nPerDep"] = decimal_number(value)
        result.append(payload)
    return result


def resource_selector(resource: str, value: Any) -> dict[str, Any]:
    """Converte o seletor público para a chave oficial do recurso."""
    definitions = {
        "customers": (
            {"id", "integration_id"},
            {"id": "codigo_cliente_omie", "integration_id": "codigo_cliente_integracao"},
        ),
        "projects": (
            {"id", "integration_id"},
            {"id": "codigo", "integration_id": "codInt"},
        ),
        "payables": (
            {"id", "integration_id"},
            {"id": "codigo_lancamento_omie", "integration_id": "codigo_lancamento_integracao"},
        ),
        "receivables": (
            {"id", "integration_id"},
            {"id": "codigo_lancamento_omie", "integration_id": "codigo_lancamento_integracao"},
        ),
        "transfers": (
            {"id", "integration_id"},
            {"id": "nCodLanc", "integration_id": "cCodIntLanc"},
        ),
        "account-entries": (
            {"id", "integration_id"},
            {"id": "nCodLanc", "integration_id": "cCodIntLanc"},
        ),
    }
    allowed, mapping = definitions[resource]
    selector = exact_selector(value, allowed, "selector")
    key, selected = next(iter(selector.items()))
    if key == "id" and (not isinstance(selected, int) or isinstance(selected, bool)):
        raise OmieToolError("'selector.id' deve ser inteiro.")
    if key == "integration_id":
        max_length = 20 if resource == "account-entries" else 60
        selected = require_nonempty_string(
            selected, "selector.integration_id", max_length
        )
    return {mapping[key]: selected}


def settlement_selector(value: Any) -> dict[str, Any]:
    """Converte um seletor de baixa financeira."""
    selector = exact_selector(value, {"id", "integration_id"}, "selector")
    key, selected = next(iter(selector.items()))
    if key == "id":
        if not isinstance(selected, int) or isinstance(selected, bool):
            raise OmieToolError("'selector.id' deve ser inteiro.")
        return {"codigo_baixa": selected}
    return {
        "codigo_baixa_integracao": require_nonempty_string(
            selected, "selector.integration_id", 20
        )
    }


def is_not_found_error(error: OmieApiError) -> bool:
    """Reconhece somente respostas inequívocas de registro ausente."""
    normalized = normalize_name(error.message)
    return any(
        marker in normalized
        for marker in (
            "nao encontrado",
            "não encontrado",
            "inexistente",
            "nao localizado",
            "não localizado",
            "nao cadastrado para o codigo",
            "não cadastrado para o código",
        )
    )


def maybe_show(
    client: OmieClient,
    service: ServiceSpec,
    selector: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Consulta um registro e distingue ausência de outras falhas."""
    try:
        return client.call(service, service.show_call, selector)
    except OmieApiError as exc:
        if is_not_found_error(exc):
            return None
        raise


def require_data(item: Mapping[str, Any]) -> dict[str, Any]:
    """Obtém o objeto de dados de uma mutação."""
    return require_object(item.get("data"), "data")


def prepare_customer_call(
    client: OmieClient,
    operation: str,
    item: Mapping[str, Any],
    request_id: str,
) -> list[PreparedCall]:
    """Prepara inclusão, alteração, inativação ou exclusão cadastral."""
    service = SERVICE_SPECS["customers"]
    if operation == "delete" and item.get("confirm_delete") is not True:
        raise OmieToolError("A exclusão exige 'confirm_delete': true.")
    if operation == "create":
        data = require_data(item)
        reject_unknown_fields(data, CUSTOMER_FIELDS, "data")
        if data.get("inativo") not in (None, "N"):
            raise OmieToolError("Um novo cliente/fornecedor deve ser criado ativo.")
        payload = validate_customer_payload(data, require_names=True)
        integration_id = derived_integration_id(
            request_id, "customer", max_length=60
        )
        payload["codigo_cliente_integracao"] = integration_id
        selector = {"codigo_cliente_integracao": integration_id}
        existing = maybe_show(client, service, selector)
        return [
            PreparedCall(
                service,
                service.mutation_call(operation),
                payload,
                "customers",
                operation,
                request_id,
                selector,
                summarize("customers", existing) if existing else None,
            )
        ]

    selector = resource_selector("customers", item.get("selector"))
    current = maybe_show(client, service, selector)
    if current is None:
        if operation == "delete":
            return [
                PreparedCall(
                    service,
                    service.mutation_call(operation),
                    selector,
                    "customers",
                    operation,
                    request_id,
                    already_applied={"deleted": True},
                )
            ]
        raise OmieToolError("Cliente/fornecedor não encontrado.")
    if operation == "delete":
        if item.get("confirm_delete") is not True:
            raise OmieToolError("A exclusão exige 'confirm_delete': true.")
        if not is_inactive(current):
            raise OmieToolError("Inative o cliente/fornecedor antes da exclusão física.")
        return [
            PreparedCall(
                service,
                service.mutation_call(operation),
                selector,
                "customers",
                operation,
                request_id,
            )
        ]

    if operation == "deactivate":
        if is_inactive(current):
            return [
                PreparedCall(
                    service,
                    service.mutation_call(operation),
                    selector,
                    "customers",
                    operation,
                    request_id,
                    already_applied=summarize("customers", current),
                )
            ]
        patch: dict[str, Any] = {"inativo": "S"}
    else:
        patch = require_data(item)
        reject_unknown_fields(patch, CUSTOMER_FIELDS, "data")
        if not patch:
            raise OmieToolError("'data' deve conter ao menos uma alteração.")
    merged = {
        key: current[key]
        for key in CUSTOMER_FIELDS
        if key in current
    }
    merged.update(patch)
    payload = validate_customer_payload(merged, require_names=True)
    payload.update(selector)
    return [
        PreparedCall(
            service,
            service.mutation_call(operation),
            payload,
            "customers",
            operation,
            request_id,
        )
    ]


def validate_customer_payload(
    data: Mapping[str, Any],
    *,
    require_names: bool,
) -> dict[str, Any]:
    """Valida os campos cadastrais expostos pela skill."""
    payload = dict(data)
    if require_names:
        payload["razao_social"] = require_nonempty_string(
            payload.get("razao_social"), "data.razao_social", 60
        )
        payload["nome_fantasia"] = require_nonempty_string(
            payload.get("nome_fantasia"), "data.nome_fantasia", 100
        )
    text_limits = {
        "cnpj_cpf": 20,
        "telefone1_ddd": 5,
        "telefone1_numero": 15,
        "telefone2_ddd": 5,
        "telefone2_numero": 15,
        "contato": 100,
        "email": 500,
        "homepage": 100,
        "endereco": 60,
        "endereco_numero": 60,
        "bairro": 60,
        "complemento": 60,
        "estado": 2,
        "cidade": 40,
        "cidade_ibge": 7,
        "cep": 10,
        "codigo_pais": 4,
        "inscricao_estadual": 20,
        "inscricao_municipal": 20,
        "inscricao_suframa": 20,
        "observacao": 5000,
        "obs_detalhadas": 5000,
    }
    for key, maximum in text_limits.items():
        if key in payload:
            payload[key] = require_nonempty_string(
                payload[key], f"data.{key}", maximum
            )
    for key in (
        "inativo",
        "optante_simples_nacional",
        "produtor_rural",
        "contribuinte",
        "bloquear_faturamento",
    ):
        if key in payload:
            payload[key] = yn_value(payload[key], f"data.{key}")
    if "valor_limite_credito" in payload:
        payload["valor_limite_credito"] = decimal_number(
            decimal_value(
                payload["valor_limite_credito"],
                "data.valor_limite_credito",
                allow_zero=True,
            )
        )
    if "tags" in payload:
        if not isinstance(payload["tags"], list):
            raise OmieToolError("'data.tags' deve ser uma lista.")
        validated_tags = []
        for index, raw in enumerate(payload["tags"], 1):
            tag = require_object(raw, f"data.tags[{index}]")
            reject_unknown_fields(tag, {"tag"}, f"data.tags[{index}]")
            validated_tags.append(
                {"tag": require_nonempty_string(tag.get("tag"), f"data.tags[{index}].tag", 60)}
            )
        payload["tags"] = validated_tags
    return payload


def prepare_project_call(
    client: OmieClient,
    operation: str,
    item: Mapping[str, Any],
    request_id: str,
) -> list[PreparedCall]:
    """Prepara operações do cadastro de projetos."""
    service = SERVICE_SPECS["projects"]
    if operation == "delete" and item.get("confirm_delete") is not True:
        raise OmieToolError("A exclusão exige 'confirm_delete': true.")
    if operation == "create":
        data = require_data(item)
        reject_unknown_fields(data, {"name", "inactive"}, "data")
        name = require_nonempty_string(data.get("name"), "data.name", 70)
        if data.get("inactive") not in (None, False):
            raise OmieToolError("Um novo projeto deve ser criado ativo.")
        integration_id = derived_integration_id(
            request_id, "project", max_length=20
        )
        payload = {"codInt": integration_id, "nome": name, "inativo": "N"}
        selector = {"codInt": integration_id}
        existing = maybe_show(client, service, selector)
        return [
            PreparedCall(
                service,
                service.mutation_call(operation),
                payload,
                "projects",
                operation,
                request_id,
                selector,
                summarize("projects", existing) if existing else None,
            )
        ]
    selector = resource_selector("projects", item.get("selector"))
    current = maybe_show(client, service, selector)
    if current is None:
        if operation == "delete":
            return [
                PreparedCall(
                    service,
                    service.mutation_call(operation),
                    selector,
                    "projects",
                    operation,
                    request_id,
                    already_applied={"deleted": True},
                )
            ]
        raise OmieToolError("Projeto não encontrado.")
    if operation == "delete":
        if item.get("confirm_delete") is not True:
            raise OmieToolError("A exclusão exige 'confirm_delete': true.")
        if not is_inactive(current):
            raise OmieToolError("Inative o projeto antes da exclusão física.")
        return [
            PreparedCall(
                service,
                service.mutation_call(operation),
                selector,
                "projects",
                operation,
                request_id,
            )
        ]
    if operation == "deactivate":
        if is_inactive(current):
            return [
                PreparedCall(
                    service,
                    service.mutation_call(operation),
                    selector,
                    "projects",
                    operation,
                    request_id,
                    already_applied=summarize("projects", current),
                )
            ]
        patch = {"inactive": True}
    else:
        patch = require_data(item)
        reject_unknown_fields(patch, {"name", "inactive"}, "data")
        if not patch:
            raise OmieToolError("'data' deve conter ao menos uma alteração.")
    payload = {
        "codigo": current.get("codigo"),
        "codInt": current.get("codInt", ""),
        "nome": current.get("nome", ""),
        "inativo": current.get("inativo", "N"),
    }
    if "name" in patch:
        payload["nome"] = require_nonempty_string(patch["name"], "data.name", 70)
    if "inactive" in patch:
        if not isinstance(patch["inactive"], bool):
            raise OmieToolError("'data.inactive' deve ser booleano.")
        payload["inativo"] = "S" if patch["inactive"] else "N"
    return [
        PreparedCall(
            service,
            service.mutation_call(operation),
            payload,
            "projects",
            operation,
            request_id,
        )
    ]


FINANCIAL_NATIVE_FIELDS = {
    "codigo_lancamento_omie",
    "codigo_lancamento_integracao",
    "codigo_cliente_fornecedor",
    "data_vencimento",
    "valor_documento",
    "codigo_categoria",
    "categorias",
    "data_previsao",
    "id_conta_corrente",
    "numero_documento",
    "numero_parcela",
    "codigo_tipo_documento",
    "numero_documento_fiscal",
    "data_emissao",
    "observacao",
    "codigo_projeto",
    "distribuicao",
}


def apply_financial_patch(
    client: OmieClient,
    current: Mapping[str, Any],
    data: Mapping[str, Any],
    *,
    require_complete: bool,
) -> dict[str, Any]:
    """Aplica campos semânticos a um contrato nativo de título financeiro."""
    reject_unknown_fields(data, FINANCIAL_FIELDS, "data")
    if "installments" in data:
        raise OmieToolError("'installments' é permitido somente na inclusão.")
    payload = {key: current[key] for key in FINANCIAL_NATIVE_FIELDS if key in current}
    if "counterparty" in data:
        payload["codigo_cliente_fornecedor"] = resolve_reference(
            client, "customer", data["counterparty"]
        )[0]
    if "due_date" in data:
        payload["data_vencimento"] = date_value(data["due_date"], "data.due_date")
    if "amount" in data:
        payload["valor_documento"] = decimal_number(
            decimal_value(data["amount"], "data.amount")
        )
    if "forecast_date" in data:
        payload["data_previsao"] = date_value(
            data["forecast_date"], "data.forecast_date"
        )
    elif require_complete and "data_vencimento" in payload:
        payload["data_previsao"] = payload["data_vencimento"]
    if "current_account" in data:
        payload["id_conta_corrente"] = resolve_reference(
            client, "current_account", data["current_account"]
        )[0]
    if "project" in data:
        if data["project"] is None:
            payload.pop("codigo_projeto", None)
        else:
            payload["codigo_projeto"] = resolve_reference(
                client, "project", data["project"]
            )[0]
    simple_fields = {
        "document_number": ("numero_documento", 20),
        "installment_number": ("numero_parcela", 7),
        "document_type": ("codigo_tipo_documento", 5),
        "fiscal_document_number": ("numero_documento_fiscal", 20),
        "observation": ("observacao", 5000),
    }
    for public, (native, maximum) in simple_fields.items():
        if public in data:
            payload[native] = require_nonempty_string(
                data[public], f"data.{public}", maximum
            )
    if "issue_date" in data:
        payload["data_emissao"] = date_value(data["issue_date"], "data.issue_date")
    if "category" in data and "categories" in data:
        raise OmieToolError("Use 'category' ou 'categories', nunca ambos.")
    total = decimal_value(payload.get("valor_documento"), "data.amount")
    if "category" in data:
        payload["codigo_categoria"] = resolve_reference(
            client, "category", data["category"]
        )[0]
        payload.pop("categorias", None)
    if "categories" in data:
        payload["categorias"] = allocation_payload(
            client, data["categories"], total, "category"
        )
        payload.pop("codigo_categoria", None)
    if "departments" in data:
        payload["distribuicao"] = allocation_payload(
            client, data["departments"], total, "department"
        )
    required = {
        "codigo_cliente_fornecedor",
        "data_vencimento",
        "valor_documento",
        "data_previsao",
        "id_conta_corrente",
    }
    missing = sorted(key for key in required if payload.get(key) in (None, ""))
    if require_complete and missing:
        raise OmieToolError(
            f"Campos financeiros obrigatórios ausentes: {', '.join(missing)}."
        )
    if require_complete and not (
        payload.get("codigo_categoria") or payload.get("categorias")
    ):
        raise OmieToolError("Informe 'category' ou 'categories'.")
    return payload


def financial_installment_data(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Expande parcelas preparadas e valida a soma contra o principal."""
    installments = data.get("installments")
    if installments is None:
        return [dict(data)]
    if not isinstance(installments, list) or not installments:
        raise OmieToolError("'data.installments' deve ser uma lista não vazia.")
    principal = decimal_value(data.get("amount"), "data.amount")
    expanded: list[dict[str, Any]] = []
    total = Decimal("0")
    count = len(installments)
    if count > 999:
        raise OmieToolError("A quantidade de parcelas não pode exceder 999.")
    for index, raw in enumerate(installments, 1):
        installment = require_object(raw, f"data.installments[{index}]")
        allowed = {
            "due_date",
            "amount",
            "forecast_date",
            "document_number",
            "categories",
            "departments",
            "project",
        }
        reject_unknown_fields(installment, allowed, f"data.installments[{index}]")
        amount = decimal_value(
            installment.get("amount"), f"data.installments[{index}].amount"
        )
        total += amount
        merged = {key: value for key, value in data.items() if key != "installments"}
        merged.update(installment)
        merged["amount"] = decimal_number(amount)
        merged["installment_number"] = f"{index:03d}/{count:03d}"
        expanded.append(merged)
    if total != principal:
        raise OmieToolError("A soma das parcelas deve ser exatamente igual ao principal.")
    return expanded


def title_paid_amount(item: Mapping[str, Any]) -> Decimal:
    """Obtém o valor já liquidado para proteger exclusões."""
    status = normalize_name(item.get("status_titulo"))
    if status in {"pago", "recebido", "liquidado", "quitado"}:
        return Decimal("0.01")
    try:
        return Decimal(str(item.get("valor_pago", 0))).quantize(Decimal("0.01"))
    except InvalidOperation:
        return Decimal("0")


def prepare_financial_call(
    client: OmieClient,
    resource: str,
    operation: str,
    item: Mapping[str, Any],
    request_id: str,
) -> list[PreparedCall]:
    """Prepara títulos, baixas, cancelamentos e conciliações."""
    service = SERVICE_SPECS[resource]
    if operation == "delete" and item.get("confirm_delete") is not True:
        raise OmieToolError("A exclusão exige 'confirm_delete': true.")
    if operation == "create":
        data = require_data(item)
        reject_unknown_fields(data, FINANCIAL_FIELDS, "data")
        expanded = financial_installment_data(data)
        calls: list[PreparedCall] = []
        for index, part in enumerate(expanded, 1):
            integration_id = derived_integration_id(
                request_id,
                resource,
                max_length=60,
                sequence=index if len(expanded) > 1 else None,
            )
            payload = apply_financial_patch(client, {}, part, require_complete=True)
            payload["codigo_lancamento_integracao"] = integration_id
            selector = {"codigo_lancamento_integracao": integration_id}
            existing = maybe_show(client, service, selector)
            calls.append(
                PreparedCall(
                    service,
                    service.mutation_call(operation),
                    payload,
                    resource,
                    operation,
                    request_id,
                    selector,
                    summarize(resource, existing) if existing else None,
                )
            )
        return calls

    if operation in ("pay", "receive"):
        selector = resource_selector(resource, item.get("selector"))
        current = client.call(service, service.show_call, selector)
        data = require_data(item)
        reject_unknown_fields(data, SETTLEMENT_FIELDS, "data")
        amount = decimal_value(data.get("amount"), "data.amount")
        document = decimal_value(
            current.get("valor_documento"), "valor_documento"
        )
        paid = title_paid_amount(current)
        if paid >= document:
            raise OmieToolError("O título já está integralmente liquidado.")
        if amount > document - paid:
            raise OmieToolError("A baixa excede o saldo aberto do título.")
        account_id = resolve_reference(
            client, "current_account", data.get("current_account")
        )[0]
        payload = {
            "codigo_lancamento_integracao": current.get(
                "codigo_lancamento_integracao", ""
            ),
            "codigo_baixa_integracao": derived_integration_id(
                request_id, f"{resource}-settlement", max_length=20
            ),
            "codigo_conta_corrente": account_id,
            "valor": decimal_number(amount),
            "data": date_value(data.get("date"), "data.date"),
        }
        if current.get("codigo_lancamento_omie") is not None:
            payload["codigo_lancamento"] = current["codigo_lancamento_omie"]
        for public, native in (
            ("discount", "desconto"),
            ("interest", "juros"),
            ("fine", "multa"),
        ):
            if public in data:
                payload[native] = decimal_number(
                    decimal_value(data[public], f"data.{public}", allow_zero=True)
                )
        if "observation" in data:
            payload["observacao"] = require_nonempty_string(
                data["observation"], "data.observation", 5000
            )
        if "reconcile" in data:
            if not isinstance(data["reconcile"], bool):
                raise OmieToolError("'data.reconcile' deve ser booleano.")
            payload["conciliar_documento"] = "S" if data["reconcile"] else "N"
        return [
            PreparedCall(
                service,
                service.mutation_call(operation),
                payload,
                resource,
                operation,
                request_id,
            )
        ]

    if operation in ("cancel-payment", "cancel-receipt", "reconcile", "unreconcile"):
        payload = settlement_selector(item.get("selector"))
        if "data" in item:
            data = require_data(item)
            reject_unknown_fields(data, set(), "data")
        return [
            PreparedCall(
                service,
                service.mutation_call(operation),
                payload,
                resource,
                operation,
                request_id,
            )
        ]

    selector = resource_selector(resource, item.get("selector"))
    current = maybe_show(client, service, selector)
    if current is None:
        if operation == "delete":
            return [
                PreparedCall(
                    service,
                    service.mutation_call(operation),
                    selector,
                    resource,
                    operation,
                    request_id,
                    already_applied={"deleted": True},
                )
            ]
        raise OmieToolError("Título financeiro não encontrado.")
    if operation == "delete":
        if item.get("confirm_delete") is not True:
            raise OmieToolError("A exclusão exige 'confirm_delete': true.")
        if title_paid_amount(current) > 0:
            raise OmieToolError("Não é permitido excluir título com baixa ativa.")
        delete_selector = dict(selector)
        if resource == "receivables" and "codigo_lancamento_omie" in delete_selector:
            delete_selector = {"chave_lancamento": delete_selector["codigo_lancamento_omie"]}
        return [
            PreparedCall(
                service,
                service.mutation_call(operation),
                delete_selector,
                resource,
                operation,
                request_id,
            )
        ]
    data = require_data(item)
    if not data:
        raise OmieToolError("'data' deve conter ao menos uma alteração.")
    payload = apply_financial_patch(client, current, data, require_complete=True)
    payload.update(selector)
    return [
        PreparedCall(
            service,
            service.mutation_call(operation),
            payload,
            resource,
            operation,
            request_id,
        )
    ]


def account_entry_nature(value: Any, label: str = "data.nature") -> str:
    """Valida a natureza pública de um lançamento direto."""
    if value not in ACCOUNT_ENTRY_NATURES:
        raise OmieToolError(f"'{label}' deve ser 'expense' ou 'revenue'.")
    return str(value)


def positive_identifier(value: Any, label: str) -> int:
    """Valida identificadores numéricos usados no preparador local."""
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise OmieToolError(f"'{label}' deve ser um inteiro maior que zero.")
    return value


def prepare_account_entry_envelope(
    args: argparse.Namespace,
    *,
    project_root: Path = PROJECT_ROOT,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Cria sem sobrescrita um envelope fechado dentro do trabalho Telegram."""
    request_id = request_identifier(args.request_id)
    nature = account_entry_nature(args.nature, "--nature")
    document_type = require_nonempty_string(
        args.document_type, "--document-type", 5
    )
    if document_type not in ACCOUNT_ENTRY_DOCUMENT_TYPES:
        raise OmieToolError("'--document-type' não pertence à allowlist.")
    data: dict[str, Any] = {
        "nature": nature,
        "account": {"id": positive_identifier(args.account_id, "--account-id")},
        "date": date_value(args.date, "--date"),
        "amount": decimal_number(decimal_value(args.amount, "--amount")),
        "document_type": document_type,
        "category": {
            "code": require_nonempty_string(
                args.category_code, "--category-code", 20
            )
        },
    }
    for option, public_name in (
        ("counterparty_id", "counterparty"),
        ("project_id", "project"),
    ):
        value = getattr(args, option, None)
        if value is not None:
            data[public_name] = {
                "id": positive_identifier(value, f"--{option.replace('_', '-')}")
            }
    for option, public_name, maximum in (
        ("document_number", "document_number", 20),
        ("observation", "observation", 5000),
    ):
        value = getattr(args, option, None)
        if value is not None:
            data[public_name] = require_nonempty_string(
                value, f"--{option.replace('_', '-')}", maximum
            )
    document = {
        "schema_version": SCHEMA_VERSION,
        "request_id": request_id,
        "data": data,
    }
    try:
        stored = write_job_json(
            "omie-account-entry",
            request_id,
            document,
            project_root=project_root,
            environment=environment,
        )
    except JobContextError as exc:
        raise OmieToolError(str(exc)) from exc
    return {"ok": True, "created": stored.created, "path": str(stored.path)}


def execute_prepare_account_entry(args: argparse.Namespace) -> dict[str, Any]:
    """Executa o preparador local sem autenticar ou acessar a API Omie."""
    return prepare_account_entry_envelope(args)


def validate_account_entry_category(
    item: Mapping[str, Any],
    nature: str,
) -> None:
    """Recusa categorias incompatíveis com um lançamento manual direto."""
    if is_inactive(item):
        raise OmieToolError("A categoria do lançamento está inativa.")
    if item.get("totalizadora") == "S":
        raise OmieToolError("A categoria do lançamento não pode ser totalizadora.")
    if item.get("nao_exibir") == "S":
        raise OmieToolError("A categoria do lançamento não está disponível para uso.")
    if item.get("transferencia") == "S":
        raise OmieToolError(
            "Categorias de transferência devem ser usadas pelo recurso 'transfers'."
        )
    expected_flag = ACCOUNT_ENTRY_NATURES[nature][2]
    if item.get(expected_flag) != "S":
        label = "despesa" if nature == "expense" else "receita"
        raise OmieToolError(f"A categoria informada não é uma categoria de {label}.")


def account_entry_category_payload(
    client: OmieClient,
    values: Any,
    total: Decimal,
    nature: str,
) -> list[dict[str, Any]]:
    """Resolve e valida um rateio de categorias de mesma natureza."""
    if not isinstance(values, list) or not values:
        raise OmieToolError("'categories' deve ser uma lista não vazia.")
    entries: list[tuple[Any, str, Decimal]] = []
    modes: set[str] = set()
    for index, raw in enumerate(values, 1):
        item = require_object(raw, f"categories[{index}]")
        reject_unknown_fields(
            item,
            {"code", "name", "amount", "percentage"},
            f"categories[{index}]",
        )
        selectors = [key for key in ("code", "name") if item.get(key) not in (None, "")]
        allocations = [key for key in ("amount", "percentage") if item.get(key) is not None]
        if len(selectors) != 1:
            raise OmieToolError(
                f"'categories[{index}]' deve informar exatamente 'code' ou 'name'."
            )
        if len(allocations) != 1:
            raise OmieToolError(
                f"'categories[{index}]' deve informar exatamente 'amount' ou 'percentage'."
            )
        identifier, category = resolve_reference(
            client, "category", {selectors[0]: item[selectors[0]]}
        )
        validate_account_entry_category(category, nature)
        mode = allocations[0]
        modes.add(mode)
        entries.append(
            (identifier, mode, decimal_value(item[mode], f"categories[{index}].{mode}"))
        )
    if len(modes) != 1:
        raise OmieToolError("Não misture valores e percentuais em 'categories'.")
    mode = next(iter(modes))
    expected = total if mode == "amount" else Decimal("100.00")
    if sum((entry[2] for entry in entries), Decimal("0")) != expected:
        label = "valor do lançamento" if mode == "amount" else "100%"
        raise OmieToolError(f"O rateio de 'categories' deve fechar exatamente em {label}.")
    return [
        {
            "cCodCateg": identifier,
            "nValor" if mode == "amount" else "nPerc": decimal_number(value),
        }
        for identifier, _, value in entries
    ]


def validate_existing_allocations(
    values: Any,
    total: Decimal,
    *,
    label: str,
    amount_key: str,
    percentage_key: str,
) -> None:
    """Confere rateios já existentes após uma atualização do valor total."""
    if not values:
        return
    if not isinstance(values, list):
        raise OmieToolError(f"O rateio existente de '{label}' é inválido.")
    amounts: list[Decimal | None] = []
    percentages: list[Decimal | None] = []
    for index, raw in enumerate(values, 1):
        item = require_object(raw, f"{label}[{index}]")
        if item.get(amount_key) is None and item.get(percentage_key) is None:
            raise OmieToolError(f"O rateio existente de '{label}' é inválido.")
        amounts.append(
            decimal_value(item[amount_key], f"{label}[{index}].{amount_key}")
            if item.get(amount_key) is not None
            else None
        )
        percentages.append(
            decimal_value(item[percentage_key], f"{label}[{index}].{percentage_key}")
            if item.get(percentage_key) is not None
            else None
        )
    for allocation_values, expected, target in (
        (amounts, total, "valor do lançamento"),
        (percentages, Decimal("100.00"), "100%"),
    ):
        present = [value for value in allocation_values if value is not None]
        if present and len(present) != len(values):
            raise OmieToolError(f"O rateio existente de '{label}' é inconsistente.")
        if present and sum(present, Decimal("0")) != expected:
            raise OmieToolError(
                f"O rateio de '{label}' deve fechar exatamente em {target}."
            )


def validate_current_entry_categories(
    client: OmieClient,
    details: Mapping[str, Any],
    total: Decimal,
    nature: str,
) -> None:
    """Valida as categorias preservadas de um lançamento existente."""
    if details.get("cCodCateg") not in (None, ""):
        _, category = resolve_reference(client, "category", {"code": details["cCodCateg"]})
        validate_account_entry_category(category, nature)
        return
    values = details.get("aCodCateg")
    if not isinstance(values, list) or not values:
        raise OmieToolError("Informe 'category' ou 'categories' no lançamento.")
    for index, raw in enumerate(values, 1):
        item = require_object(raw, f"categories[{index}]")
        code = item.get("cCodCateg")
        if code in (None, ""):
            raise OmieToolError("O rateio existente possui categoria sem código.")
        _, category = resolve_reference(client, "category", {"code": code})
        validate_account_entry_category(category, nature)
    validate_existing_allocations(
        values,
        total,
        label="categories",
        amount_key="nValor",
        percentage_key="nPerc",
    )


def current_account_entry_nature(current: Mapping[str, Any]) -> str:
    """Obtém a natureza apenas de origens manuais diretas permitidas."""
    diverse = current.get("diversos", {})
    if not isinstance(diverse, dict):
        raise OmieToolError("A Omie não devolveu a origem do lançamento.")
    origin = diverse.get("cOrigem")
    for nature, (api_nature, api_origin, _) in ACCOUNT_ENTRY_NATURES.items():
        if origin == api_origin:
            returned_nature = diverse.get("cNatureza")
            if returned_nature not in (None, "", api_nature):
                raise OmieToolError("A origem e a natureza do lançamento são incompatíveis.")
            return nature
    raise OmieToolError(
        "Somente lançamentos manuais diretos EXTP ou EXTR podem ser alterados ou excluídos."
    )


def account_entry_payload(
    client: OmieClient,
    current: Mapping[str, Any],
    data: Mapping[str, Any],
    *,
    integration_id: str | None,
) -> dict[str, Any]:
    """Monta um lançamento manual direto de receita ou despesa."""
    reject_unknown_fields(data, ACCOUNT_ENTRY_FIELDS, "data")
    nature = account_entry_nature(data.get("nature"))
    header = (
        dict(current.get("cabecalho", {}))
        if isinstance(current.get("cabecalho"), dict)
        else {}
    )
    details = (
        dict(current.get("detalhes", {}))
        if isinstance(current.get("detalhes"), dict)
        else {}
    )
    departments = (
        list(current.get("departamentos", []))
        if isinstance(current.get("departamentos"), list)
        else []
    )

    if "account" in data:
        header["nCodCC"] = resolve_reference(client, "current_account", data["account"])[0]
    if "date" in data:
        header["dDtLanc"] = date_value(data["date"], "data.date")
    if "amount" in data:
        header["nValorLanc"] = decimal_number(decimal_value(data["amount"], "data.amount"))
    total = decimal_value(header.get("nValorLanc"), "data.amount")

    if "document_type" in data:
        document_type = require_nonempty_string(
            data["document_type"], "data.document_type", 5
        )
        if document_type not in ACCOUNT_ENTRY_DOCUMENT_TYPES:
            raise OmieToolError(
                "'data.document_type' não pertence à allowlist de lançamentos diretos."
            )
        details["cTipo"] = document_type

    if "category" in data and "categories" in data:
        raise OmieToolError("Use 'category' ou 'categories', nunca ambos.")
    if "category" in data:
        identifier, category = resolve_reference(client, "category", data["category"])
        validate_account_entry_category(category, nature)
        details["cCodCateg"] = identifier
        details.pop("aCodCateg", None)
    elif "categories" in data:
        details["aCodCateg"] = account_entry_category_payload(
            client, data["categories"], total, nature
        )
        details.pop("cCodCateg", None)
    else:
        validate_current_entry_categories(client, details, total, nature)

    if "counterparty" in data:
        if data["counterparty"] is None:
            details.pop("nCodCliente", None)
        else:
            details["nCodCliente"] = resolve_reference(client, "customer", data["counterparty"])[0]
    if "project" in data:
        if data["project"] is None:
            details.pop("nCodProjeto", None)
        else:
            details["nCodProjeto"] = resolve_reference(client, "project", data["project"])[0]
    if "departments" in data:
        if data["departments"] == []:
            departments = []
        else:
            allocations = allocation_payload(client, data["departments"], total, "department")
            departments = [
                {
                    "cCodDep": entry["cCodDep"],
                    **({"nValDep": entry["nValDep"]} if "nValDep" in entry else {}),
                    **({"nPerDep": entry["nPerDep"]} if "nPerDep" in entry else {}),
                }
                for entry in allocations
            ]
    else:
        validate_existing_allocations(
            departments,
            total,
            label="departments",
            amount_key="nValDep",
            percentage_key="nPerDep",
        )
    for public, official, maximum in (
        ("document_number", "cNumDoc", 20),
        ("observation", "cObs", 5000),
    ):
        if public in data:
            if data[public] is None:
                details.pop(official, None)
            else:
                details[official] = require_nonempty_string(data[public], f"data.{public}", maximum)

    required = {
        "account": header.get("nCodCC"),
        "date": header.get("dDtLanc"),
        "amount": header.get("nValorLanc"),
        "document_type": details.get("cTipo"),
    }
    missing = [name for name, value in required.items() if value in (None, "")]
    if missing:
        raise OmieToolError(f"Campos obrigatórios do lançamento ausentes: {', '.join(missing)}.")
    if details.get("cTipo") == "TRA":
        raise OmieToolError("Use o recurso 'transfers' para tipos de documento TRA.")
    payload: dict[str, Any] = {"cabecalho": header, "detalhes": details}
    if integration_id:
        payload["cCodIntLanc"] = integration_id
    if departments:
        payload["departamentos"] = departments
    return payload


def prepare_account_entry_call(
    client: OmieClient,
    operation: str,
    item: Mapping[str, Any],
    request_id: str,
) -> list[PreparedCall]:
    """Prepara o ciclo CRUD de um lançamento manual direto."""
    service = SERVICE_SPECS["account-entries"]
    if operation == "create":
        data = require_data(item)
        integration_id = derived_integration_id(request_id, "account-entry", max_length=20)
        payload = account_entry_payload(client, {}, data, integration_id=integration_id)
        selector = {"cCodIntLanc": integration_id}
        existing = maybe_show(client, service, selector)
        if existing is not None:
            existing_nature = current_account_entry_nature(existing)
            if existing_nature != account_entry_nature(data.get("nature")):
                raise OmieToolError("O request_id já identifica lançamento de outra natureza.")
        return [
            PreparedCall(
                service,
                service.mutation_call(operation),
                payload,
                "account-entries",
                operation,
                request_id,
                selector,
                summarize("account-entries", existing) if existing else None,
            )
        ]

    selector = resource_selector("account-entries", item.get("selector"))
    current = maybe_show(client, service, selector)
    if current is None:
        if operation == "delete":
            return [
                PreparedCall(
                    service,
                    service.mutation_call(operation),
                    selector,
                    "account-entries",
                    operation,
                    request_id,
                    selector,
                    {"deleted": True},
                )
            ]
        raise OmieToolError("Lançamento direto não encontrado.")
    current_nature = current_account_entry_nature(current)
    if operation == "delete":
        if item.get("confirm_delete") is not True:
            raise OmieToolError("A exclusão exige 'confirm_delete': true.")
        return [
            PreparedCall(
                service,
                service.mutation_call(operation),
                selector,
                "account-entries",
                operation,
                request_id,
                selector,
            )
        ]

    data = require_data(item)
    if set(data) <= {"nature"}:
        raise OmieToolError("'data' deve conter ao menos uma alteração além de 'nature'.")
    requested_nature = account_entry_nature(data.get("nature"))
    if requested_nature != current_nature:
        raise OmieToolError("Não é permitido converter despesa em receita ou vice-versa.")
    current_integration_id = current.get("cCodIntLanc")
    payload = account_entry_payload(
        client,
        current,
        data,
        integration_id=(
            str(current_integration_id)
            if current_integration_id not in (None, "")
            else None
        ),
    )
    payload.update(selector)
    return [
        PreparedCall(
            service,
            service.mutation_call(operation),
            payload,
            "account-entries",
            operation,
            request_id,
            selector,
        )
    ]


def transfer_payload(
    client: OmieClient,
    current: Mapping[str, Any],
    data: Mapping[str, Any],
    *,
    integration_id: str,
) -> dict[str, Any]:
    """Monta um único lançamento TRA entre duas contas Omie."""
    reject_unknown_fields(data, TRANSFER_FIELDS, "data")
    header = (
        dict(current.get("cabecalho", {}))
        if isinstance(current.get("cabecalho"), dict)
        else {}
    )
    details = dict(current.get("detalhes", {})) if isinstance(current.get("detalhes"), dict) else {}
    transfer = (
        dict(current.get("transferencia", {}))
        if isinstance(current.get("transferencia"), dict)
        else {}
    )
    departments = current.get("departamentos", [])
    if "source_account" in data:
        header["nCodCC"] = resolve_reference(
            client, "current_account", data["source_account"]
        )[0]
    if "destination_account" in data:
        transfer["nCodCCDestino"] = resolve_reference(
            client, "current_account", data["destination_account"]
        )[0]
    if "date" in data:
        header["dDtLanc"] = date_value(data["date"], "data.date")
    if "amount" in data:
        header["nValorLanc"] = decimal_number(
            decimal_value(data["amount"], "data.amount")
        )
    total = decimal_value(header.get("nValorLanc"), "data.amount")
    if header.get("nCodCC") == transfer.get("nCodCCDestino"):
        raise OmieToolError("As contas de origem e destino devem ser diferentes.")
    if "category" in data and "categories" in data:
        raise OmieToolError("Use 'category' ou 'categories', nunca ambos.")
    if "category" in data:
        details["cCodCateg"] = resolve_reference(
            client, "category", data["category"]
        )[0]
        details.pop("aCodCateg", None)
    if "categories" in data:
        allocations = allocation_payload(
            client, data["categories"], total, "category"
        )
        details["aCodCateg"] = [
            {
                "cCodCateg": entry["codigo_categoria"],
                **({"nValor": entry["valor"]} if "valor" in entry else {}),
                **({"nPerc": entry["percentual"]} if "percentual" in entry else {}),
            }
            for entry in allocations
        ]
        details.pop("cCodCateg", None)
    if "counterparty" in data:
        if data["counterparty"] is None:
            details.pop("nCodCliente", None)
        else:
            details["nCodCliente"] = resolve_reference(
                client, "customer", data["counterparty"]
            )[0]
    if "project" in data:
        if data["project"] is None:
            details.pop("nCodProjeto", None)
        else:
            details["nCodProjeto"] = resolve_reference(
                client, "project", data["project"]
            )[0]
    if "departments" in data:
        allocations = allocation_payload(
            client, data["departments"], total, "department"
        )
        departments = [
            {
                "cCodDep": entry["cCodDep"],
                **({"nValDep": entry["nValDep"]} if "nValDep" in entry else {}),
                **({"nPerDep": entry["nPerDep"]} if "nPerDep" in entry else {}),
            }
            for entry in allocations
        ]
    if "document_number" in data:
        details["cNumDoc"] = require_nonempty_string(
            data["document_number"], "data.document_number", 20
        )
    if "observation" in data:
        details["cObs"] = require_nonempty_string(
            data["observation"], "data.observation", 5000
        )
    required = {
        "source_account": header.get("nCodCC"),
        "destination_account": transfer.get("nCodCCDestino"),
        "date": header.get("dDtLanc"),
        "amount": header.get("nValorLanc"),
    }
    missing = [name for name, value in required.items() if value in (None, "")]
    if missing:
        raise OmieToolError(
            f"Campos obrigatórios da transferência ausentes: {', '.join(missing)}."
        )
    if not (details.get("cCodCateg") or details.get("aCodCateg")):
        raise OmieToolError("Informe 'category' ou 'categories' na transferência.")
    details["cTipo"] = "TRA"
    payload = {
        "cCodIntLanc": integration_id,
        "cabecalho": header,
        "detalhes": details,
        "transferencia": transfer,
    }
    if departments:
        payload["departamentos"] = departments
    return payload


def prepare_transfer_call(
    client: OmieClient,
    operation: str,
    item: Mapping[str, Any],
    request_id: str,
) -> list[PreparedCall]:
    """Prepara o ciclo completo de uma transferência entre contas."""
    service = SERVICE_SPECS["transfers"]
    if operation == "delete" and item.get("confirm_delete") is not True:
        raise OmieToolError("A exclusão exige 'confirm_delete': true.")
    if operation == "create":
        data = require_data(item)
        integration_id = derived_integration_id(
            request_id, "transfer", max_length=20
        )
        payload = transfer_payload(
            client, {}, data, integration_id=integration_id
        )
        selector = {"cCodIntLanc": integration_id}
        existing = maybe_show(client, service, selector)
        return [
            PreparedCall(
                service,
                service.mutation_call(operation),
                payload,
                "transfers",
                operation,
                request_id,
                selector,
                summarize("transfers", existing) if existing else None,
            )
        ]
    selector = resource_selector("transfers", item.get("selector"))
    current = maybe_show(client, service, selector)
    if current is None:
        if operation == "delete":
            return [
                PreparedCall(
                    service,
                    service.mutation_call(operation),
                    selector,
                    "transfers",
                    operation,
                    request_id,
                    already_applied={"deleted": True},
                )
            ]
        raise OmieToolError("Transferência não encontrada.")
    if operation == "delete":
        if item.get("confirm_delete") is not True:
            raise OmieToolError("A exclusão exige 'confirm_delete': true.")
        return [
            PreparedCall(
                service,
                service.mutation_call(operation),
                selector,
                "transfers",
                operation,
                request_id,
            )
        ]
    data = require_data(item)
    if not data:
        raise OmieToolError("'data' deve conter ao menos uma alteração.")
    integration_id = str(current.get("cCodIntLanc") or next(iter(selector.values())))
    payload = transfer_payload(
        client, current, data, integration_id=integration_id
    )
    payload.update(selector)
    return [
        PreparedCall(
            service,
            service.mutation_call(operation),
            payload,
            "transfers",
            operation,
            request_id,
        )
    ]


def prepare_mutation_item(
    client: OmieClient,
    resource: str,
    operation: str,
    item: Mapping[str, Any],
    request_id: str,
) -> list[PreparedCall]:
    """Despacha somente combinações recurso/operação cadastradas."""
    if operation == "create":
        required, allowed = {"data"}, {"data"}
    elif operation == "update":
        required, allowed = {"selector", "data"}, {"selector", "data"}
    elif operation == "deactivate":
        required, allowed = {"selector"}, {"selector"}
    elif operation == "delete":
        required = allowed = {"selector", "confirm_delete"}
    elif operation in ("pay", "receive"):
        required, allowed = {"selector", "data"}, {"selector", "data"}
    else:
        required = allowed = {"selector"}
    present = set(item)
    missing = sorted(required - present)
    unexpected = sorted(present - allowed)
    if missing:
        raise OmieToolError(
            f"Campos obrigatórios ausentes no item: {', '.join(missing)}."
        )
    if unexpected:
        raise OmieToolError(
            f"Campos incompatíveis com '{operation}': {', '.join(unexpected)}."
        )
    if resource == "customers":
        return prepare_customer_call(client, operation, item, request_id)
    if resource == "projects":
        return prepare_project_call(client, operation, item, request_id)
    if resource in ("payables", "receivables"):
        return prepare_financial_call(
            client, resource, operation, item, request_id
        )
    if resource == "transfers":
        return prepare_transfer_call(client, operation, item, request_id)
    if resource == "account-entries":
        return prepare_account_entry_call(client, operation, item, request_id)
    raise OmieToolError(f"Mutação não suportada em '{resource}'.")


def summarize_mutation_response(response: Mapping[str, Any]) -> dict[str, Any]:
    """Limita a saída de escrita a identificadores e status operacionais."""
    return pick(
        response,
        (
            "codigo_cliente_omie",
            "codigo_cliente_integracao",
            "codigo",
            "codInt",
            "codigo_lancamento_omie",
            "codigo_lancamento_integracao",
            "codigo_lancamento",
            "codigo_baixa",
            "codigo_baixa_integracao",
            "liquidado",
            "valor_baixado",
            "nCodLanc",
            "cCodIntLanc",
            "codigo_status",
            "descricao_status",
            "cCodStatus",
            "cDesStatus",
            "status",
            "descricao",
        ),
    )


def execute_mutation(
    client: OmieClient,
    args: argparse.Namespace,
) -> dict[str, Any]:
    """Valida o lote inteiro e somente depois executa chamadas sequenciais."""
    if not args.profile:
        raise OmieToolError("Toda mutação exige '--profile' explícito.")
    envelope = load_input_document(args)
    base_request_id = envelope["request_id"]
    prepared: list[PreparedCall] = []
    items = envelope_items(envelope)
    for index, item in enumerate(items, 1):
        item_request_id = (
            base_request_id if len(items) == 1 else f"{base_request_id}:{index}"
        )
        prepared.extend(
            prepare_mutation_item(
                client,
                args.resource,
                args.operation,
                item,
                item_request_id,
            )
        )
    if args.dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "resource": args.resource,
            "operation": args.operation,
            "request_id": base_request_id,
            "calls": [
                {
                    "method": call.method,
                    "params": sanitize_payload(call.params),
                    "already_applied": call.already_applied is not None,
                }
                for call in prepared
            ],
        }
    results: list[dict[str, Any]] = []
    for index, call in enumerate(prepared, 1):
        if call.already_applied is not None:
            results.append(
                {
                    "index": index,
                    "status": "already_applied",
                    "item": sanitize_payload(call.already_applied),
                }
            )
            continue
        try:
            response = client.call(call.service, call.method, call.params)
        except OmieUnknownStateError:
            recovered = None
            if call.recovery_selector is not None:
                recovered = maybe_show(
                    client, call.service, call.recovery_selector
                )
            if recovered is None and call.operation == "delete" and call.recovery_selector:
                results.append(
                    {
                        "index": index,
                        "status": "recovered_after_timeout",
                        "item": {"deleted": True},
                    }
                )
                continue
            if recovered is None:
                raise
            results.append(
                {
                    "index": index,
                    "status": "recovered_after_timeout",
                    "item": sanitize_payload(summarize(call.resource, recovered)),
                }
            )
            continue
        results.append(
            {
                "index": index,
                "status": "applied",
                "response": sanitize_payload(
                    summarize_mutation_response(response)
                ),
            }
        )
    return {
        "ok": True,
        "dry_run": False,
        "resource": args.resource,
        "operation": args.operation,
        "request_id": base_request_id,
        "count": len(results),
        "results": results,
    }


def execute_doctor(
    client: OmieClient,
    _: argparse.Namespace,
) -> dict[str, Any]:
    """Valida autenticação com uma página mínima de empresas."""
    service = SERVICE_SPECS["companies"]
    _, metadata = client.list_page(service, page=1, params={})
    return {
        "ok": True,
        "authenticated": True,
        "company_read": True,
        "company_count": metadata["total_records"],
        "write_capabilities": {
            resource: [operation for operation, _ in service.mutation_calls]
            for resource, service in SERVICE_SPECS.items()
            if service.mutation_calls
        },
        "write_validation": "mocked_only",
        "real_write_tested": False,
    }


def execute_list(
    client: OmieClient,
    args: argparse.Namespace,
) -> dict[str, Any]:
    """Lista registros com paginação limitada."""
    service = SERVICE_SPECS[args.resource]
    validate_date_range(
        getattr(args, "changed_from", None),
        getattr(args, "changed_to", None),
        "alteração",
    )
    if args.resource in ("payables", "receivables"):
        validate_date_range(args.issued_from, args.issued_to, "emissão")
    params = list_params(args.resource, args)
    page = args.page
    collected: list[dict[str, Any]] = []
    metadata: dict[str, int] = {}
    pages_fetched = 0
    while True:
        items, metadata = client.list_page(service, page=page, params=params)
        collected.extend(items)
        pages_fetched += 1
        if not args.all_pages or page >= metadata["total_pages"]:
            break
        if pages_fetched >= client.config.max_pages:
            break
        page += 1
    truncated = (
        args.all_pages
        and bool(metadata)
        and page < metadata["total_pages"]
    )
    return {
        "ok": True,
        "resource": args.resource,
        "count": len(collected),
        "items": [sanitize_payload(summarize(args.resource, item)) for item in collected],
        "pagination": {
            **metadata,
            "pages_fetched": pages_fetched,
            "truncated": truncated,
            "next_page": page + 1 if truncated else None,
        },
    }


def execute_show(
    client: OmieClient,
    args: argparse.Namespace,
) -> dict[str, Any]:
    """Consulta um registro por um seletor explícito."""
    service = SERVICE_SPECS[args.resource]
    params: dict[str, Any] = {}
    for option, api_field, _ in service.selectors:
        value = getattr(args, option.replace("-", "_"), None)
        if value is not None:
            params[api_field] = value
    if args.resource == "service-orders":
        params["cIncluirTarefas"] = "N"
    payload = client.call(service, service.show_call, params)
    return {
        "ok": True,
        "resource": args.resource,
        "item": sanitize_payload(summarize(args.resource, payload)),
    }


def add_list_filters(parser: argparse.ArgumentParser, resource: str) -> None:
    """Adiciona filtros comuns e específicos de listagem."""
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument(
        "--all-pages",
        action="store_true",
        help="Percorre páginas até o limite configurado.",
    )
    if resource not in ("transfers", "account-entries"):
        parser.add_argument("--only-api", action="store_true")
        parser.add_argument("--changed-from", type=validate_date)
        parser.add_argument("--changed-to", type=validate_date)
        change_group = parser.add_mutually_exclusive_group()
        change_group.add_argument("--only-created", action="store_true")
        change_group.add_argument("--only-changed", action="store_true")
    if resource == "products":
        parser.add_argument("--description")
    if resource in ("payables", "receivables"):
        parser.add_argument("--issued-from", type=validate_date)
        parser.add_argument("--issued-to", type=validate_date)
        parser.add_argument("--customer-id", type=int)
        parser.add_argument("--status")
    if resource in ("sales-orders", "service-orders"):
        parser.add_argument("--customer-id", type=int)
        parser.add_argument("--status")
    if resource == "account-entries":
        parser.add_argument(
            "--nature",
            required=True,
            choices=tuple(ACCOUNT_ENTRY_NATURES),
            help="Natureza manual: expense (EXTP) ou revenue (EXTR).",
        )
    parser.set_defaults(handler=execute_list)


def add_show_selectors(
    parser: argparse.ArgumentParser,
    service: ServiceSpec,
) -> None:
    """Adiciona seletores mutuamente exclusivos documentados."""
    selectors = parser.add_mutually_exclusive_group(required=True)
    for option, _, help_text in service.selectors:
        value_type = int if option == "id" else str
        selectors.add_argument(f"--{option}", type=value_type, help=help_text)
    parser.set_defaults(handler=execute_show)


def add_mutation_input(parser: argparse.ArgumentParser) -> None:
    """Adiciona a entrada JSON fechada comum a todas as escritas."""
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input-file", help="Arquivo JSON UTF-8 com o envelope.")
    source.add_argument(
        "--input-stdin",
        action="store_true",
        help="Lê o envelope JSON integralmente da entrada padrão.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Valida e mostra as chamadas sem executar escrita.",
    )
    parser.set_defaults(handler=execute_mutation)


def add_account_entry_prepare(parser: argparse.ArgumentParser) -> None:
    """Expõe somente os campos tipados do envelope direto inicial."""
    parser.add_argument("--request-id", required=True)
    parser.add_argument(
        "--nature", required=True, choices=tuple(ACCOUNT_ENTRY_NATURES)
    )
    parser.add_argument("--account-id", required=True, type=int)
    parser.add_argument("--date", required=True, type=validate_date)
    parser.add_argument("--amount", required=True)
    parser.add_argument(
        "--document-type",
        required=True,
        choices=tuple(sorted(ACCOUNT_ENTRY_DOCUMENT_TYPES)),
    )
    parser.add_argument("--category-code", required=True)
    parser.add_argument("--counterparty-id", type=int)
    parser.add_argument("--project-id", type=int)
    parser.add_argument("--document-number")
    parser.add_argument("--observation")
    parser.set_defaults(handler=execute_prepare_account_entry)


def build_parser() -> argparse.ArgumentParser:
    """Constrói a CLI restrita aos contratos documentados da skill."""
    parser = argparse.ArgumentParser(
        description="Consulta e altera dados permitidos do ERP Omie com credenciais protegidas."
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--profile")
    resources = parser.add_subparsers(dest="resource", required=True)
    doctor = resources.add_parser("doctor", help="Valida autenticação e leitura.")
    doctor.set_defaults(handler=execute_doctor)

    for name, service in SERVICE_SPECS.items():
        resource = resources.add_parser(name, help=f"Consulta {name}.")
        operations = resource.add_subparsers(dest="operation", required=True)
        list_command = operations.add_parser("list", help="Lista registros.")
        add_list_filters(list_command, name)
        show_command = operations.add_parser("show", help="Consulta um registro.")
        add_show_selectors(show_command, service)
        if name == "account-entries":
            prepare = operations.add_parser(
                "prepare",
                help="Prepara um envelope de criação no trabalho Telegram.",
            )
            add_account_entry_prepare(prepare)
        for operation, _ in service.mutation_calls:
            mutation = operations.add_parser(
                operation,
                help=f"Executa {operation} com envelope JSON validado.",
            )
            add_mutation_input(mutation)
    return parser


def print_json(payload: Any, *, stream: Any = sys.stdout) -> None:
    """Imprime JSON UTF-8 sanitizado."""
    print(
        json.dumps(
            sanitize_payload(payload),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        file=stream,
    )


def main() -> int:
    """Carrega credenciais, executa a operação e sanitiza falhas."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    args = build_parser().parse_args()
    if getattr(args, "page", 1) < 1:
        print_json(
            {"ok": False, "error": "'--page' deve ser maior que zero."},
            stream=sys.stderr,
        )
        return 2
    if getattr(args, "handler", None) is execute_mutation and not args.profile:
        print_json(
            {"ok": False, "error": "Toda mutação exige '--profile' explícito."},
            stream=sys.stderr,
        )
        return 2
    client: OmieClient | None = None
    try:
        if args.handler is execute_prepare_account_entry:
            result = args.handler(args)
        else:
            config = load_config(Path(args.config).expanduser().resolve(), args.profile)
            app_key, app_secret = read_entry_credentials(config.credential_ref)
            client = OmieClient(config, app_key, app_secret)
            app_key = ""
            app_secret = ""
            result = args.handler(client, args)
    except (OmieToolError, VaultToolError, OSError) as exc:
        print_json(
            {"ok": False, "error": str(exc), "error_type": type(exc).__name__},
            stream=sys.stderr,
        )
        return 1
    finally:
        if client is not None:
            client.close()
    print_json(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
