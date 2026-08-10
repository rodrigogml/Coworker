"""Parsing, validation and calculation for RFC 6238 TOTP credentials."""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import hmac
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, unquote, urlparse


class TotpError(ValueError):
    """Erro sanitizado de parsing ou cálculo TOTP."""


@dataclass(frozen=True)
class TotpConfig:
    issuer: str
    account: str
    secret: str
    algorithm: str = "SHA1"
    digits: int = 6
    period: int = 30


@dataclass(frozen=True)
class TotpRecord:
    config: TotpConfig
    entry: str


def _text(value: object, label: str, *, required: bool = True) -> str:
    result = str(value or "").strip()
    if required and not result:
        raise TotpError(f"{label} não pode ficar vazio.")
    if any(char in result for char in "\r\n\0") or len(result) > 255:
        raise TotpError(f"{label} é inválido.")
    return result


def sanitize_component(value: str) -> str:
    """Converte um issuer/conta em componente seguro de título do KeePassXC."""
    value = _text(value, "Identificador")
    value = value.replace("/", "／").replace("\\", "＼")
    return value.strip(" .") or "TOTP"


def normalize_secret(value: str) -> str:
    secret = re.sub(r"\s+", "", str(value or "")).upper()
    if not secret or not re.fullmatch(r"[A-Z2-7]+=*", secret):
        raise TotpError("A chave TOTP não é uma Base32 válida.")
    secret = secret.rstrip("=")
    try:
        base64.b32decode(secret + "=" * ((8 - len(secret) % 8) % 8), casefold=True)
    except (ValueError, binascii.Error) as exc:
        raise TotpError("A chave TOTP não é uma Base32 válida.") from exc
    return secret


def _algorithm(value: str) -> str:
    algorithm = str(value or "SHA1").strip().upper().replace("-", "")
    if algorithm not in {"SHA1", "SHA256", "SHA512"}:
        raise TotpError("O algoritmo TOTP deve ser SHA1, SHA256 ou SHA512.")
    return algorithm


def _integer(value: object, label: str, allowed: set[int]) -> int:
    try:
        number = int(str(value))
    except (TypeError, ValueError) as exc:
        raise TotpError(f"{label} é inválido.") from exc
    if number not in allowed:
        raise TotpError(f"{label} é inválido.")
    return number


def build_config(
    *, issuer: str, account: str, secret: str, algorithm: str = "SHA1",
    digits: int | str = 6, period: int | str = 30,
) -> TotpConfig:
    return TotpConfig(
        issuer=_text(issuer, "Issuer"),
        account=_text(account, "Conta"),
        secret=normalize_secret(secret),
        algorithm=_algorithm(algorithm),
        digits=_integer(digits, "Quantidade de dígitos", {6, 8}),
        period=_integer(period, "Período", set(range(1, 301))),
    )


def parse_otpauth_uri(uri: str, *, fallback_issuer: str = "", fallback_account: str = "") -> TotpConfig:
    value = str(uri or "").strip()
    parsed = urlparse(value)
    if parsed.scheme.casefold() != "otpauth" or parsed.netloc.casefold() != "totp":
        raise TotpError("A URI deve usar o esquema otpauth://totp.")
    label = unquote(parsed.path.lstrip("/"))
    issuer_from_label, separator, account_from_label = label.partition(":")
    query = parse_qs(parsed.query, keep_blank_values=True)

    def query_value(name: str) -> str:
        values = query.get(name, [])
        return str(values[0]).strip() if values else ""

    issuer = query_value("issuer") or issuer_from_label or fallback_issuer
    account = account_from_label if separator else label
    account = account or fallback_account
    return build_config(
        issuer=issuer,
        account=account,
        secret=query_value("secret"),
        algorithm=query_value("algorithm") or "SHA1",
        digits=query_value("digits") or 6,
        period=query_value("period") or 30,
    )


def parse_input(value: str, *, issuer: str = "", account: str = "") -> TotpConfig:
    value = str(value or "").strip()
    if value.casefold().startswith("otpauth://"):
        return parse_otpauth_uri(value, fallback_issuer=issuer, fallback_account=account)
    return build_config(issuer=issuer, account=account, secret=value)


def _digest(name: str) -> Callable[..., "hashlib._Hash"]:
    return getattr(hashlib, name.lower())


def generate_code(config: TotpConfig, timestamp: float) -> str:
    counter = int(timestamp // config.period)
    key = base64.b32decode(config.secret + "=" * ((8 - len(config.secret) % 8) % 8), casefold=True)
    digest = hmac.new(key, counter.to_bytes(8, "big"), _digest(config.algorithm)).digest()
    offset = digest[-1] & 0x0F
    number = int.from_bytes(digest[offset:offset + 4], "big") & 0x7FFFFFFF
    return str(number % (10 ** config.digits)).zfill(config.digits)


def totp_codes(config: TotpConfig, timestamp: float | None = None) -> tuple[str, str, float, float, float]:
    now = time.time() if timestamp is None else float(timestamp)
    current_start = now - (now % config.period)
    current_expiry = current_start + config.period
    next_expiry = current_expiry + config.period
    return (
        generate_code(config, current_start),
        generate_code(config, current_expiry),
        current_expiry,
        next_expiry,
        max(0.0, current_expiry - now),
    )


def format_codes(config: TotpConfig, timestamp: float | None = None, *, local_tz=None) -> str:
    current, following, current_expiry, next_expiry, remaining = totp_codes(config, timestamp)
    tz = local_tz or datetime.now().astimezone().tzinfo or timezone.utc
    current_clock = datetime.fromtimestamp(current_expiry, tz).strftime("%H:%M:%S")
    next_clock = datetime.fromtimestamp(next_expiry, tz).strftime("%H:%M:%S")
    return (
        f"🔑 {config.issuer} — {config.account}\n"
        f"{current} [{current_clock}] (+{round(remaining):d}s)\n"
        f"{following} [{next_clock}] (+{round(remaining + config.period):d}s)"
    )


def decode_qr(path: Path) -> str:
    """Decodifica o primeiro QR Code usando zxing-cpp sem enviar a imagem à rede."""
    try:
        import zxingcpp
        from PIL import Image
    except ImportError as exc:
        raise TotpError("A dependência local de leitura de QR Code não está instalada.") from exc
    try:
        image = Image.open(path)
        results = zxingcpp.read_barcodes(image)
    except Exception as exc:
        raise TotpError("Não foi possível ler o QR Code.") from exc
    for result in results:
        text = str(getattr(result, "text", "") or "").strip()
        if text:
            return text
    raise TotpError("O QR Code não contém dados legíveis.")


def decode_qr_bytes(data: bytes) -> str:
    """Decodifica QR em memória sem persistir a imagem recebida."""
    if not data or len(data) > 4 * 1024 * 1024:
        raise TotpError("A imagem do QR Code excede o limite.")
    try:
        import zxingcpp
        from PIL import Image
        with Image.open(io.BytesIO(data)) as image:
            if image.width * image.height > 16_000_000:
                raise TotpError("A imagem do QR Code excede o limite de pixels.")
            results = zxingcpp.read_barcodes(image)
    except TotpError:
        raise
    except ImportError as exc:
        raise TotpError("A dependência local de leitura de QR Code não está instalada.") from exc
    except Exception as exc:
        raise TotpError("Não foi possível ler o QR Code.") from exc
    for result in results:
        text = str(getattr(result, "text", "") or "").strip()
        if text:
            return text
    raise TotpError("O QR Code não contém dados legíveis.")


def entry_path(config: TotpConfig) -> str:
    return f"TOTP/{sanitize_component(config.issuer)} — {sanitize_component(config.account)}"
