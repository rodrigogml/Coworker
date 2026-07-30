#!/usr/bin/env python3
"""Cliente HTTP restrito e sanitizado para APIs Google."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable


class GoogleApiError(Exception):
    """Erro seguro de uma API Google."""

    def __init__(
        self,
        service: str,
        status: int | None,
        code: str,
        message: str,
    ) -> None:
        prefix = f"{service} HTTP {status}" if status is not None else service
        super().__init__(f"{prefix} ({code}): {message}")


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Impede que o cabeçalho Bearer seja reenviado a outra origem."""

    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def validate_api_base(value: Any, *, host: str, path: str, field: str) -> str:
    """Aceita somente a origem e o prefixo oficiais esperados."""
    endpoint = str(value or "").rstrip("/")
    parsed = urllib.parse.urlparse(endpoint)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"'{field}' contém uma porta inválida.") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != host
        or parsed.path.rstrip("/") != path.rstrip("/")
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"'{field}' deve apontar para o endpoint oficial esperado.")
    return endpoint


def _safe_error(payload: Any) -> tuple[str, str]:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            code = error.get("status") or error.get("code")
            message = error.get("message")
            return (
                str(code)[:100] if code is not None else "api_error",
                str(message)[:500] if message else "A API recusou a operação.",
            )
    return "api_error", "A API recusou a operação."


class GoogleApiClient:
    """Executa somente métodos e caminhos relativos fechados."""

    def __init__(
        self,
        api_base: str,
        access_token: str,
        service: str,
        *,
        timeout_seconds: int,
        max_response_bytes: int,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        if not access_token:
            raise ValueError(f"O access token de {service} está vazio.")
        self.api_base = api_base.rstrip("/")
        self.service = service
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self._access_token = access_token
        self._opener = opener or urllib.request.build_opener(
            _NoRedirectHandler()
        ).open

    def close(self) -> None:
        self._access_token = ""

    def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        body: bytes | None = None,
        content_type: str | None = None,
        expect_json: bool = True,
        response_limit: int | None = None,
    ) -> Any:
        if not self._access_token:
            raise ValueError(f"O cliente {self.service} já foi encerrado.")
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            raise ValueError("Método interno da API inválido.")
        if (
            not path.startswith("/")
            or ".." in path
            or "?" in path
            or "#" in path
            or "\\" in path
        ):
            raise ValueError("Caminho interno da API inválido.")
        if payload is not None and body is not None:
            raise ValueError("A requisição não pode ter dois corpos.")
        clean_query = {
            key: value
            for key, value in (query or {}).items()
            if value is not None and value != []
        }
        url = f"{self.api_base}{path}"
        if clean_query:
            url += "?" + urllib.parse.urlencode(clean_query, doseq=True)
        headers = {
            "Accept": "application/json" if expect_json else "*/*",
            "Authorization": f"Bearer {self._access_token}",
            "User-Agent": f"BOTina-{self.service}/1.0",
        }
        request_body = body
        if payload is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
            request_body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        elif body is not None and content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(
            url,
            data=request_body,
            headers=headers,
            method=method,
        )
        limit = response_limit or self.max_response_bytes
        try:
            with self._opener(request, timeout=self.timeout_seconds) as response:
                raw = response.read(limit + 1)
        except urllib.error.HTTPError as exc:
            try:
                error_raw = exc.read(self.max_response_bytes + 1)
                if len(error_raw) > self.max_response_bytes:
                    error_payload = {}
                else:
                    error_payload = json.loads(error_raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                error_payload = {}
            code, message = _safe_error(error_payload)
            code = code.replace(self._access_token, "[REDACTED]")
            message = message.replace(self._access_token, "[REDACTED]")
            raise GoogleApiError(
                self.service,
                exc.code,
                code,
                message,
            ) from exc
        except urllib.error.URLError as exc:
            raise GoogleApiError(
                self.service,
                None,
                "network_error",
                "Falha de comunicação com a API.",
            ) from exc
        if len(raw) > limit:
            raise GoogleApiError(
                self.service,
                None,
                "response_too_large",
                "A resposta excede o limite local configurado.",
            )
        if not expect_json:
            return raw
        if not raw:
            return None
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GoogleApiError(
                self.service,
                None,
                "invalid_response",
                "A API devolveu JSON inválido.",
            ) from exc
        return self._redact(decoded)

    def _redact(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): self._redact(item)
                for key, item in value.items()
                if str(key).lower() not in {"access_token", "refresh_token"}
            }
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        if isinstance(value, str):
            return value.replace(self._access_token, "[REDACTED]")
        return value


def paginate(
    client: GoogleApiClient,
    path: str,
    result_key: str,
    query: dict[str, Any],
    *,
    all_pages: bool,
    max_pages: int,
) -> dict[str, Any]:
    """Agrega páginas preservando o token caso a leitura seja truncada."""
    results: list[Any] = []
    page_token = query.get("pageToken")
    base_query = dict(query)
    base_query.pop("pageToken", None)
    pages_read = 0
    next_token: str | None = None
    response: dict[str, Any] = {}
    while True:
        current = dict(base_query)
        current["pageToken"] = page_token
        raw_response = client.request("GET", path, query=current)
        if not isinstance(raw_response, dict):
            raise GoogleApiError(
                client.service,
                None,
                "invalid_response",
                "A lista devolvida é inválida.",
            )
        response = raw_response
        page_results = response.get(result_key, [])
        if not isinstance(page_results, list):
            raise GoogleApiError(
                client.service,
                None,
                "invalid_response",
                "Os itens devolvidos são inválidos.",
            )
        results.extend(page_results)
        pages_read += 1
        raw_next = response.get("nextPageToken")
        next_token = raw_next if isinstance(raw_next, str) else None
        if not all_pages or not next_token or pages_read >= max_pages:
            break
        page_token = next_token
    return {
        result_key: results,
        "pagination": {
            "pages_read": pages_read,
            "next_page_token": next_token,
            "truncated": bool(next_token),
        },
    }
