"""Consulta contas digitais da CPFL sem expor dados financeiros."""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import http.cookiejar
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parseaddr
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = PROJECT_ROOT / "data" / "config" / "cpfl.toml"
EXAMPLE_CONFIG = PROJECT_ROOT / "config" / "cpfl.example.toml"
GMAIL_SCRIPT = PROJECT_ROOT / "skills" / "gmail" / "scripts" / "gmail.py"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.integration_profiles import (  # noqa: E402
    IntegrationProfileError,
    validate_profile_name,
)
from scripts.vault_entities import (  # noqa: E402
    VaultEntityError,
    inspect_entry,
    read_entry_attribute,
)


class CpflError(RuntimeError):
    """Representa uma falha sanitizada da integração CPFL."""


@dataclass(frozen=True)
class PortalConfig:
    allowed_host: str
    allowed_path: str
    timeout_seconds: int


@dataclass(frozen=True)
class MailConfig:
    sender: str
    search_days: int
    search_limit: int


@dataclass(frozen=True)
class ProfileConfig:
    name: str
    gmail_profile: str
    entity_ref: str
    consumer_unit: str | None


@dataclass(frozen=True)
class CpflConfig:
    portal: PortalConfig
    mail: MailConfig
    profile: ProfileConfig


@dataclass(frozen=True)
class BillMessage:
    identifier: str
    subject: str
    sent_at: str
    consumer_unit: str | None
    reference_month: str | None
    due_date: str | None
    amount: str | None
    portal_url: str


def _required_mapping(values: dict[str, Any], name: str) -> dict[str, Any]:
    section = values.get(name)
    if not isinstance(section, dict):
        raise CpflError(f"A seção [{name}] é obrigatória.")
    return section


def load_config(path: Path, requested_profile: str | None = None) -> CpflConfig:
    """Carrega configuração privada e seleciona um perfil local."""
    resolved = path.expanduser().resolve()
    try:
        with resolved.open("rb") as stream:
            values = tomllib.load(stream)
    except FileNotFoundError as exc:
        raise CpflError(
            f"Configuração ausente. Copie '{EXAMPLE_CONFIG}' para '{DEFAULT_CONFIG}'."
        ) from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise CpflError("Não foi possível carregar a configuração da CPFL.") from exc

    portal = _required_mapping(values, "portal")
    mail = _required_mapping(values, "mail")
    profiles = _required_mapping(values, "profiles")
    selected = requested_profile or str(values.get("default_profile", "")).strip()
    try:
        selected = validate_profile_name(selected)
    except IntegrationProfileError as exc:
        raise CpflError(str(exc)) from exc
    profile_values = profiles.get(selected)
    if not isinstance(profile_values, dict):
        available = ", ".join(sorted(str(name) for name in profiles))
        raise CpflError(f"Perfil '{selected}' não encontrado. Disponíveis: {available}.")
    gmail_profile = str(profile_values.get("gmail_profile", "")).strip()
    entity_ref = str(profile_values.get("entity_ref", "")).strip()
    consumer_unit = str(profile_values.get("consumer_unit", "")).strip() or None
    if not gmail_profile or not entity_ref:
        raise CpflError("O perfil exige 'gmail_profile' e 'entity_ref'.")

    allowed_host = str(portal.get("allowed_host", "")).strip().casefold()
    allowed_path = str(portal.get("allowed_path", "")).strip()
    sender = str(mail.get("sender", "")).strip().casefold()
    timeout_seconds = int(portal.get("timeout_seconds", 30))
    search_days = int(mail.get("search_days", 120))
    search_limit = int(mail.get("search_limit", 20))
    if allowed_host != "contadigital.cpfl.com.br":
        raise CpflError("O host permitido da CPFL não pode ser alterado.")
    if allowed_path.casefold() != "/boleto/boletolink.aspx":
        raise CpflError("O caminho permitido da CPFL não pode ser alterado.")
    if sender != "contadigital@cpfl.com.br":
        raise CpflError("O remetente permitido da CPFL não pode ser alterado.")
    if not 5 <= timeout_seconds <= 120:
        raise CpflError("'timeout_seconds' deve ficar entre 5 e 120.")
    if not 1 <= search_days <= 730 or not 1 <= search_limit <= 100:
        raise CpflError("Limites de pesquisa da CPFL inválidos.")
    return CpflConfig(
        portal=PortalConfig(allowed_host, allowed_path, timeout_seconds),
        mail=MailConfig(sender, search_days, search_limit),
        profile=ProfileConfig(
            selected,
            gmail_profile,
            entity_ref,
            consumer_unit,
        ),
    )


def _run_gmail(config: CpflConfig, arguments: list[str]) -> dict[str, Any]:
    completed = subprocess.run(
        [
            sys.executable,
            str(GMAIL_SCRIPT),
            "--profile",
            config.profile.gmail_profile,
            *arguments,
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=PROJECT_ROOT,
    )
    if completed.returncode != 0:
        raise CpflError("Não foi possível consultar o Gmail configurado.")
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise CpflError("O Gmail devolveu uma resposta inválida.") from exc
    if not isinstance(result, dict):
        raise CpflError("O Gmail devolveu uma estrutura inesperada.")
    return result


def _message_headers(message: dict[str, Any]) -> dict[str, list[str]]:
    headers: dict[str, list[str]] = {}
    for item in (message.get("payload") or {}).get("headers") or []:
        name = str(item.get("name", "")).casefold()
        headers.setdefault(name, []).append(str(item.get("value", "")))
    return headers


def _decode_message_body(payload: dict[str, Any]) -> str:
    chunks: list[str] = []

    def collect(node: dict[str, Any]) -> None:
        data = (node.get("body") or {}).get("data")
        if data:
            try:
                padding = "=" * (-len(data) % 4)
                chunks.append(
                    base64.urlsafe_b64decode(data + padding).decode(
                        "utf-8", errors="replace"
                    )
                )
            except (ValueError, TypeError):
                pass
        for child in node.get("parts") or []:
            collect(child)

    collect(payload)
    return "\n".join(chunks)


class _EmailParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() == "a":
            url = html.unescape(dict(attrs).get("href") or "")
            if url.startswith(("http://", "https://")):
                self.urls.append(url)

    def handle_data(self, data: str) -> None:
        self.text.append(data)


def _validate_portal_url(url: str, config: CpflConfig) -> str:
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() != config.portal.allowed_host
        or parsed.path.casefold() != config.portal.allowed_path.casefold()
    ):
        raise CpflError("A mensagem contém um link fora do portal permitido.")
    return url


def _extract_value(text: str, pattern: str) -> str | None:
    match = re.search(pattern, text, flags=re.IGNORECASE)
    return " ".join(match.group(1).split()) if match else None


def parse_bill_message(message: dict[str, Any], config: CpflConfig) -> BillMessage:
    """Valida a autenticidade básica e extrai o link individual da mensagem."""
    identifier = str(message.get("id", ""))
    if not re.fullmatch(r"[A-Fa-f0-9]+", identifier):
        raise CpflError("A mensagem possui identificador inválido.")
    headers = _message_headers(message)
    sender = parseaddr((headers.get("from") or [""])[0])[1].casefold()
    authentication = " ".join(headers.get("authentication-results") or []).casefold()
    if sender != config.mail.sender:
        raise CpflError("A mensagem não foi enviada pelo remetente oficial da CPFL.")
    authentication_markers = (
        "dkim=pass header.i=@cpfl.com.br",
        "spf=pass",
        "smtp.mailfrom=contadigital@cpfl.com.br",
        "dmarc=pass",
        "header.from=cpfl.com.br",
    )
    if not all(marker in authentication for marker in authentication_markers):
        raise CpflError("A autenticação do e-mail da CPFL não pôde ser confirmada.")
    body = _decode_message_body(message.get("payload") or {})
    parser = _EmailParser()
    parser.feed(body)
    candidates = []
    for url in parser.urls:
        try:
            candidates.append(_validate_portal_url(url, config))
        except CpflError:
            continue
    if len(set(candidates)) != 1:
        raise CpflError("A mensagem não contém um único link oficial da conta.")
    visible_text = " ".join(" ".join(parser.text).split())
    consumer_unit = _extract_value(visible_text, r"Número\s+da\s+UC:\s*([0-9.\-]+)")
    if config.profile.consumer_unit:
        expected = re.sub(r"\D", "", config.profile.consumer_unit)
        actual = re.sub(r"\D", "", consumer_unit or "")
        if actual != expected:
            raise CpflError("A mensagem pertence a outra unidade consumidora.")
    return BillMessage(
        identifier=identifier,
        subject=(headers.get("subject") or [""])[0],
        sent_at=(headers.get("date") or [""])[0],
        consumer_unit=consumer_unit,
        reference_month=_extract_value(
            visible_text, r"Mês\s+de\s+referência:\s*([0-9]{2}/[0-9]{4})"
        ),
        due_date=_extract_value(
            visible_text, r"Data\s+de\s+vencimento:\s*([0-9]{2}/[0-9]{2}/[0-9]{4})"
        ),
        amount=_extract_value(visible_text, r"Valor:\s*(R\$\s*[0-9.,]+)"),
        portal_url=next(iter(set(candidates))),
    )


def _message_by_id(config: CpflConfig, identifier: str) -> BillMessage:
    if not re.fullmatch(r"[A-Fa-f0-9]+", identifier):
        raise CpflError("O ID da mensagem é inválido.")
    message = _run_gmail(config, ["messages", "show", "--id", identifier, "--format", "full"])
    return parse_bill_message(message, config)


def latest_message(config: CpflConfig) -> BillMessage:
    query = f"from:{config.mail.sender} newer_than:{config.mail.search_days}d"
    listing = _run_gmail(
        config,
        ["messages", "list", "--query", query, "--limit", str(config.mail.search_limit)],
    )
    for item in listing.get("messages") or []:
        try:
            return _message_by_id(config, str(item.get("id", "")))
        except CpflError:
            continue
    raise CpflError("Nenhuma conta digital válida da CPFL foi localizada.")


class _FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.action = ""
        self.fields: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag.casefold() == "form" and not self.action:
            self.action = values.get("action") or ""
        if tag.casefold() == "input" and values.get("name"):
            self.fields[str(values["name"])] = values.get("value") or ""


def _open_portal(message: BillMessage, config: CpflConfig) -> tuple[str, str]:
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    headers = {"User-Agent": "Mozilla/5.0 Coworker/1.0"}
    try:
        with opener.open(
            urllib.request.Request(message.portal_url, headers=headers),
            timeout=config.portal.timeout_seconds,
        ) as response:
            page_url = response.geturl()
            page = response.read(2_000_000).decode(
                response.headers.get_content_charset() or "utf-8", errors="replace"
            )
    except Exception as exc:
        raise CpflError("Não foi possível abrir o portal da CPFL.") from exc
    _validate_portal_url(page_url, config)
    form = _FormParser()
    form.feed(page)
    if "txtNumDoc" not in form.fields or "__VIEWSTATE" not in form.fields:
        raise CpflError("O formulário da CPFL mudou e não pode ser processado com segurança.")
    try:
        cpf = read_entry_attribute(config.profile.entity_ref, "CPF")
    except VaultEntityError as exc:
        raise CpflError("Não foi possível obter o CPF da pessoa titular.") from exc
    form.fields["txtNumDoc"] = cpf[:4]
    cpf = ""
    form.fields["Button1"] = form.fields.get("Button1") or "Button"
    action = urllib.parse.urljoin(page_url, form.action or page_url)
    _validate_portal_url(action, config)
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
            timeout=config.portal.timeout_seconds,
        ) as response:
            result_url = response.geturl()
            result = response.read(2_000_000).decode(
                response.headers.get_content_charset() or "utf-8", errors="replace"
            )
    except Exception as exc:
        raise CpflError("A validação da conta no portal da CPFL falhou.") from exc
    _validate_portal_url(result_url, config)
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


def retrieve_payment_data(message: BillMessage, config: CpflConfig) -> dict[str, Any]:
    """Obtém os códigos internamente e exige formatos reconhecidos."""
    _, page = _open_portal(message, config)
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
        "schema_version": 1,
        "provider": "CPFL",
        "message_id": message.identifier,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "bill": {
            "subject": message.subject,
            "sent_at": message.sent_at,
            "consumer_unit": message.consumer_unit,
            "reference_month": message.reference_month,
            "due_date": message.due_date,
            "amount": message.amount,
        },
        "payment": {"barcode": barcode, "pix": pix},
    }


def _safe_output_path(raw_path: Path | None, message_id: str) -> Path:
    data_root = (PROJECT_ROOT / "data").resolve()
    output = (
        raw_path.expanduser().resolve()
        if raw_path
        else (data_root / "work" / "cpfl" / f"{message_id}-payment.json").resolve()
    )
    if output != data_root and data_root not in output.parents:
        raise CpflError("O arquivo de pagamento deve permanecer dentro de 'data/'.")
    return output


def write_private_json(path: Path, payload: dict[str, Any], overwrite: bool) -> bool:
    """Grava atomicamente, aceitando repetição somente para conteúdo de pagamento igual."""
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CpflError("O arquivo de destino existente não é um JSON válido.") from exc
        same_payment = existing.get("payment") == payload.get("payment")
        if same_payment:
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


def _masked_consumer_unit(value: str | None) -> str | None:
    digits = re.sub(r"\D", "", value or "")
    return f"***{digits[-4:]}" if digits else None


def bill_summary(message: BillMessage) -> dict[str, Any]:
    return {
        "message_id": message.identifier,
        "subject": message.subject,
        "sent_at": message.sent_at,
        "consumer_unit": _masked_consumer_unit(message.consumer_unit),
        "reference_month": message.reference_month,
        "due_date": message.due_date,
        "amount": message.amount,
        "portal": {
            "host": urllib.parse.urlsplit(message.portal_url).hostname,
            "path": urllib.parse.urlsplit(message.portal_url).path,
            "individual_url_exposed": False,
        },
    }


def command_doctor(config: CpflConfig) -> dict[str, Any]:
    _run_gmail(config, ["doctor"])
    inspection = inspect_entry(config.profile.entity_ref)
    cpf = next(
        (item for item in inspection["attributes"] if item["name"] == "CPF"),
        None,
    )
    if not cpf or not cpf["present"] or not cpf["protected"]:
        raise CpflError("O perfil exige um CPF protegido na entrada da pessoa titular.")
    return {
        "ok": True,
        "profile": config.profile.name,
        "gmail_profile": config.profile.gmail_profile,
        "entity_ref": config.profile.entity_ref,
        "cpf_present": True,
        "cpf_protected": True,
        "secrets_exposed": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Contas digitais da CPFL.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--profile")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="Valida Gmail, cofre e configuração.")
    commands.add_parser("latest", help="Localiza a conta digital mais recente.")
    payment = commands.add_parser(
        "payment-data", help="Grava PIX e linha digitável em arquivo privado."
    )
    payment.add_argument("--message-id")
    payment.add_argument("--output", type=Path)
    payment.add_argument("--overwrite", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        config = load_config(args.config, args.profile)
        if args.command == "doctor":
            result = command_doctor(config)
        else:
            message = (
                _message_by_id(config, args.message_id)
                if getattr(args, "message_id", None)
                else latest_message(config)
            )
            if args.command == "latest":
                result = {"ok": True, "bill": bill_summary(message)}
            else:
                payment_data = retrieve_payment_data(message, config)
                output = _safe_output_path(args.output, message.identifier)
                written = write_private_json(output, payment_data, args.overwrite)
                result = {
                    "ok": True,
                    "bill": bill_summary(message),
                    "payment": {
                        "barcode": validate_barcode(payment_data["payment"]["barcode"]),
                        "pix": validate_pix(payment_data["payment"]["pix"]),
                        "values_exposed": False,
                    },
                    "output_file": str(output),
                    "file_written": written,
                    "pdf": {"browser_required": True, "captcha_required": True},
                }
                payment_data = {}
    except (CpflError, VaultEntityError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
