#!/usr/bin/env python3
"""Consulta serviços permitidos da API Omie sem expor credenciais."""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = PROJECT_ROOT / "data" / "config" / "omie.toml"
EXAMPLE_CONFIG = PROJECT_ROOT / "config" / "omie.example.toml"
PROJECT_SCRIPTS = PROJECT_ROOT / "scripts"
if str(PROJECT_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(PROJECT_SCRIPTS))

from credential_vault import VaultToolError, read_entry_credentials  # noqa: E402
from integration_profiles import (  # noqa: E402
    IntegrationProfileError,
    resolve_credential_ref,
)


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
    """Contrato de leitura de um serviço permitido."""

    resource: str
    path: str
    list_call: str
    list_key: str
    show_call: str
    selectors: tuple[tuple[str, str, str], ...]


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
            f"Configuração local não encontrada em '{path}'. Copie "
            f"'{EXAMPLE_CONFIG}' para '{DEFAULT_CONFIG}'."
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
        if method not in (service.list_call, service.show_call):
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
        except urllib.error.URLError as exc:
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
                "pagina": page,
                "registros_por_pagina": self.config.page_size,
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
            "page": integer_or(payload.get("pagina"), page),
            "total_pages": integer_or(payload.get("total_de_paginas"), page),
            "records": integer_or(payload.get("registros"), len(items)),
            "total_records": integer_or(
                payload.get("total_de_registros"),
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
                "status_titulo",
            ),
        )
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
    params: dict[str, Any] = {"apenas_importado_api": "S" if args.only_api else "N"}
    if args.changed_from:
        params["filtrar_por_data_de"] = args.changed_from
    if args.changed_to:
        params["filtrar_por_data_ate"] = args.changed_to
    if args.only_created:
        params["filtrar_apenas_inclusao"] = "S"
    if args.only_changed:
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
        "write_capabilities": False,
    }


def execute_list(
    client: OmieClient,
    args: argparse.Namespace,
) -> dict[str, Any]:
    """Lista registros com paginação limitada."""
    service = SERVICE_SPECS[args.resource]
    validate_date_range(args.changed_from, args.changed_to, "alteração")
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


def build_parser() -> argparse.ArgumentParser:
    """Constrói a CLI somente de leitura da skill."""
    parser = argparse.ArgumentParser(
        description="Consulta dados do ERP Omie com credenciais protegidas."
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
    """Carrega credenciais, executa a consulta e sanitiza falhas."""
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
    client: OmieClient | None = None
    try:
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
