#!/usr/bin/env python3
"""Obtém dados de uma conta CPFL a partir de um link oficial protegido."""

from __future__ import annotations

import argparse
import hashlib
import html
import http.cookiejar
import json
import os
import re
import sys
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, TextIO


PROJECT_ROOT = Path(__file__).resolve().parents[3]
OFFICIAL_HOST = "contadigital.cpfl.com.br"
OFFICIAL_PATH = "/Boleto/boletolink.aspx"
DEFAULT_TIMEOUT_SECONDS = 30
MAX_LINK_CHARACTERS = 8192
MAX_PAGE_BYTES = 2_000_000

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.vault_entities import (  # noqa: E402
    VaultEntityError,
    inspect_entry,
    read_entry_attribute,
)


class CpflError(RuntimeError):
    """Representa uma falha sanitizada da automação CPFL."""


@dataclass(frozen=True)
class PortalRequest:
    request_id: str
    portal_url: str
    entity_ref: str


class _FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.action = ""
        self.fields: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag.casefold() == "form" and not self.action:
            self.action = html.unescape(values.get("action") or "")
        if tag.casefold() == "input" and values.get("name"):
            self.fields[str(values["name"])] = html.unescape(values.get("value") or "")


def _validate_portal_url(url: str, *, require_query: bool = True) -> str:
    """Aceita somente o endpoint oficial com parâmetros individuais não vazios."""
    if not url or len(url) > MAX_LINK_CHARACTERS or any(char in url for char in "\r\n\0"):
        raise CpflError("O link individual da CPFL é inválido.")
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise CpflError("O link individual da CPFL é inválido.") from exc
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() != OFFICIAL_HOST
        or parsed.path.casefold() != OFFICIAL_PATH.casefold()
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or bool(parsed.fragment)
        or (require_query and not parsed.query)
    ):
        raise CpflError("O link não pertence ao endpoint oficial permitido da CPFL.")
    return url


def _validate_entity_ref(value: str) -> str:
    entity_ref = str(value or "").strip().replace("\\", "/")
    if (
        not entity_ref.startswith("Pessoas/Fisicas/")
        or entity_ref.endswith("/")
        or any(part in {"", ".", ".."} for part in entity_ref.split("/"))
        or any(char in entity_ref for char in "\r\n\0")
    ):
        raise CpflError("A referência da pessoa titular é inválida.")
    return entity_ref


def build_request(link: str, entity_ref: str) -> PortalRequest:
    portal_url = _validate_portal_url(link.strip())
    validated_ref = _validate_entity_ref(entity_ref)
    request_id = hashlib.sha256(portal_url.encode("utf-8")).hexdigest()[:16]
    return PortalRequest(request_id, portal_url, validated_ref)


def read_link(
    *,
    link_file: Path | None,
    link_stdin: bool,
    stdin: TextIO = sys.stdin,
) -> str:
    """Lê o link sem aceitá-lo em argumento, log ou saída estruturada."""
    if bool(link_file) == bool(link_stdin):
        raise CpflError("Informe exatamente uma fonte: --link-file ou --link-stdin.")
    try:
        raw = (
            link_file.expanduser().read_text(encoding="utf-8")
            if link_file is not None
            else stdin.read(MAX_LINK_CHARACTERS + 1)
        )
    except OSError as exc:
        raise CpflError("Não foi possível ler o link individual da CPFL.") from exc
    if len(raw) > MAX_LINK_CHARACTERS:
        raise CpflError("O link individual da CPFL excede o limite permitido.")
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if len(lines) != 1:
        raise CpflError("A fonte deve conter somente um link da CPFL.")
    return _validate_portal_url(lines[0])


def _read_page(response: Any) -> str:
    raw = response.read(MAX_PAGE_BYTES + 1)
    if len(raw) > MAX_PAGE_BYTES:
        raise CpflError("A resposta do portal da CPFL excede o limite permitido.")
    charset = response.headers.get_content_charset() or "utf-8"
    return raw.decode(charset, errors="replace")


def _open_portal(request: PortalRequest, timeout_seconds: int) -> tuple[str, str]:
    """Mantém cookies e envia ao portal somente a parte necessária do CPF."""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    headers = {"User-Agent": "Mozilla/5.0 Coworker/1.0"}
    try:
        with opener.open(
            urllib.request.Request(request.portal_url, headers=headers),
            timeout=timeout_seconds,
        ) as response:
            page_url = _validate_portal_url(response.geturl(), require_query=False)
            page = _read_page(response)
    except CpflError:
        raise
    except Exception as exc:
        raise CpflError("Não foi possível abrir o portal da CPFL.") from exc

    form = _FormParser()
    form.feed(page)
    if "txtNumDoc" not in form.fields or "__VIEWSTATE" not in form.fields:
        raise CpflError("O formulário da CPFL mudou e não pode ser processado com segurança.")
    try:
        cpf = read_entry_attribute(request.entity_ref, "CPF")
    except VaultEntityError as exc:
        raise CpflError("Não foi possível obter o CPF da pessoa titular.") from exc
    try:
        if not re.fullmatch(r"\d{11}", cpf):
            raise CpflError("O CPF protegido da pessoa titular possui formato inválido.")
        form.fields["txtNumDoc"] = cpf[:4]
    finally:
        cpf = ""

    form.fields["Button1"] = form.fields.get("Button1") or "Button"
    action = _validate_portal_url(
        urllib.parse.urljoin(page_url, form.action or page_url),
        require_query=False,
    )
    payload = urllib.parse.urlencode(form.fields).encode("utf-8")
    try:
        with opener.open(
            urllib.request.Request(
                action,
                data=payload,
                headers={
                    **headers,
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Referer": page_url,
                },
            ),
            timeout=timeout_seconds,
        ) as response:
            result_url = _validate_portal_url(response.geturl(), require_query=False)
            result = _read_page(response)
    except CpflError:
        raise
    except Exception as exc:
        raise CpflError("A validação da conta no portal da CPFL falhou.") from exc
    return result_url, result


def _crc16(value: bytes) -> str:
    crc = 0xFFFF
    for byte in value:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return f"{crc:04X}"


def _parse_tlv(payload: str) -> tuple[list[tuple[str, str]], bool]:
    fields: list[tuple[str, str]] = []
    position = 0
    while position + 4 <= len(payload):
        tag = payload[position : position + 2]
        length_text = payload[position + 2 : position + 4]
        if not tag.isdigit() or not length_text.isdigit():
            return fields, False
        length = int(length_text)
        start = position + 4
        end = start + length
        if end > len(payload):
            return fields, False
        fields.append((tag, payload[start:end]))
        position = end
    return fields, position == len(payload)


def validate_barcode(value: str) -> dict[str, Any]:
    digits = re.sub(r"\D", "", value)
    return {
        "present": bool(value),
        "digit_count": len(digits),
        "febraban_collection_line": len(digits) == 48 and digits.startswith("8"),
    }


def validate_pix(value: str) -> dict[str, Any]:
    fields, complete = _parse_tlv(value)
    tags = {tag: field for tag, field in fields}
    crc_valid = (
        len(value) >= 8
        and value[-8:-4] == "6304"
        and _crc16(value[:-4].encode("utf-8")) == value[-4:].upper()
    )
    return {
        "present": bool(value),
        "payload_length": len(value),
        "tlv_complete": complete,
        "format_indicator_valid": tags.get("00") == "01",
        "currency_brl": tags.get("53") == "986",
        "country_br": tags.get("58") == "BR",
        "crc16_valid": crc_valid,
    }


def retrieve_payment_data(
    request: PortalRequest,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Obtém os códigos internamente e exige formatos reconhecidos."""
    _, page = _open_portal(request, timeout_seconds)
    parser = _FormParser()
    parser.feed(page)
    barcode = parser.fields.get("hdCodigoBarras", "")
    pix = parser.fields.get("hdPIX", "")
    barcode_validation = validate_barcode(barcode)
    pix_validation = validate_pix(pix)
    if not barcode_validation["febraban_collection_line"]:
        raise CpflError("A linha digitável da CPFL não possui o formato esperado.")
    if not all(
        pix_validation[key]
        for key in (
            "tlv_complete",
            "format_indicator_valid",
            "currency_brl",
            "country_br",
            "crc16_valid",
        )
    ):
        raise CpflError("O payload PIX da CPFL não possui o formato esperado.")
    return {
        "schema_version": 2,
        "provider": "CPFL",
        "request_id": request.request_id,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "payment": {"barcode": barcode, "pix": pix},
    }


def _safe_output_path(raw_path: Path | None, request_id: str) -> Path:
    data_root = (PROJECT_ROOT / "data").resolve()
    output = (
        raw_path.expanduser().resolve()
        if raw_path
        else (data_root / "work" / "cpfl" / f"{request_id}-payment.json").resolve()
    )
    if output != data_root and data_root not in output.parents:
        raise CpflError("O arquivo de pagamento deve permanecer dentro de 'data/'.")
    return output


def write_private_json(path: Path, payload: dict[str, Any], overwrite: bool) -> bool:
    """Grava atomicamente e repete com segurança somente o mesmo pagamento."""
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CpflError("O arquivo de destino existente não é um JSON válido.") from exc
        if existing.get("payment") == payload.get("payment"):
            return False
        if not overwrite:
            raise CpflError("O destino já existe com outros dados; use --overwrite.")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            stream.write(serialized)
            temporary_name = stream.name
        os.replace(temporary_name, path)
    finally:
        if temporary_name and Path(temporary_name).exists():
            Path(temporary_name).unlink()
    return True


def command_doctor(entity_ref: str) -> dict[str, Any]:
    validated_ref = _validate_entity_ref(entity_ref)
    inspection = inspect_entry(validated_ref)
    cpf = next(
        (item for item in inspection["attributes"] if item["name"] == "CPF"),
        None,
    )
    if not cpf or not cpf["present"] or not cpf["protected"]:
        raise CpflError("A operação exige um CPF protegido na entrada da pessoa titular.")
    return {
        "ok": True,
        "entity_ref": validated_ref,
        "cpf_present": True,
        "cpf_protected": True,
        "configuration_required": False,
        "secrets_exposed": False,
    }


def _add_link_source(parser: argparse.ArgumentParser) -> None:
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--link-file", type=Path)
    source.add_argument("--link-stdin", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Consulta direta de contas da CPFL.")
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="Valida a referência protegida do CPF.")
    doctor.add_argument("--entity-ref", required=True)
    payment = commands.add_parser(
        "payment-data",
        help="Obtém PIX e linha digitável a partir de um link oficial.",
    )
    payment.add_argument("--entity-ref", required=True)
    _add_link_source(payment)
    payment.add_argument("--output", type=Path)
    payment.add_argument("--overwrite", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if not 5 <= args.timeout_seconds <= 120:
            raise CpflError("--timeout-seconds deve ficar entre 5 e 120.")
        if args.command == "doctor":
            result = command_doctor(args.entity_ref)
        else:
            link = read_link(
                link_file=args.link_file,
                link_stdin=args.link_stdin,
            )
            request = build_request(link, args.entity_ref)
            link = ""
            payment_data = retrieve_payment_data(request, args.timeout_seconds)
            output = _safe_output_path(args.output, request.request_id)
            written = write_private_json(output, payment_data, args.overwrite)
            result = {
                "ok": True,
                "request_id": request.request_id,
                "portal": {
                    "host": OFFICIAL_HOST,
                    "path": OFFICIAL_PATH,
                    "individual_url_exposed": False,
                },
                "payment": {
                    "barcode": validate_barcode(payment_data["payment"]["barcode"]),
                    "pix": validate_pix(payment_data["payment"]["pix"]),
                    "values_exposed": False,
                },
                "output_file": str(output),
                "file_written": written,
                "pdf": {"browser_required": True, "captcha_may_be_required": True},
            }
            payment_data = {}
    except (CpflError, VaultEntityError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
