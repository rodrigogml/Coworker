from __future__ import annotations

import base64
import sys
from datetime import timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "instance"))

from skills.totp.core import (  # noqa: E402
    TotpError,
    build_config,
    entry_path,
    format_codes,
    parse_input,
    parse_otpauth_uri,
    totp_codes,
)


def test_rfc_6238_sha1_vector() -> None:
    secret = base64.b32encode(b"12345678901234567890").decode().rstrip("=")
    config = build_config(issuer="Example", account="alice", secret=secret)
    assert totp_codes(config, 59)[0] == "287082"
    assert totp_codes(build_config(issuer="Example", account="alice", secret=secret, digits=8), 59)[0] == "94287082"


def test_parse_otpauth_uri_preserves_parameters() -> None:
    config = parse_otpauth_uri(
        "otpauth://totp/Example:alice%40example.com?secret=JBSWY3DPEHPK3PXP"
        "&issuer=Example&algorithm=SHA256&digits=8&period=45"
    )
    assert config.issuer == "Example"
    assert config.account == "alice@example.com"
    assert config.algorithm == "SHA256"
    assert config.digits == 8
    assert config.period == 45


def test_raw_secret_requires_metadata() -> None:
    with pytest.raises(TotpError):
        parse_input("JBSWY3DPEHPK3PXP")


def test_codes_include_remaining_lifetime_and_safe_entry_path() -> None:
    config = build_config(issuer="Acme/Cloud", account="alice", secret="JBSWY3DPEHPK3PXP")
    assert entry_path(config) == "TOTP/Acme／Cloud — alice"
    rendered = format_codes(config, 59, local_tz=timezone.utc)
    assert "🔑 Acme/Cloud — alice" in rendered
    assert "(+1s)" in rendered
    assert "(+31s)" in rendered


def test_uri_rejects_non_totp_scheme() -> None:
    with pytest.raises(TotpError):
        parse_otpauth_uri("otpauth://hotp/Example:alice?secret=JBSWY3DPEHPK3PXP")
