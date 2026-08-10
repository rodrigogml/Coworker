"""Casos de uso TOTP sem dependência de Telegram, HTTP ou Codex."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from typing import Any

from skills.totp import vault
from skills.totp.core import TotpConfig, TotpError, build_config, decode_qr_bytes, parse_input, totp_codes


def password_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return "pbkdf2-sha256$200000$" + base64.urlsafe_b64encode(salt).decode() + "$" + base64.urlsafe_b64encode(digest).decode()


def password_matches(password: str, encoded: str | None) -> bool:
    try:
        kind, rounds, salt, digest = (encoded or "").split("$", 3)
        if kind != "pbkdf2-sha256":
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.urlsafe_b64decode(salt), int(rounds))
        return hmac.compare_digest(actual, base64.urlsafe_b64decode(digest))
    except (ValueError, TypeError, UnicodeError):
        return False


class TotpApplication:
    """Serviço de domínio TOTP. Não conhece a interface que o chamou."""

    def _ensure_new_entry(self, config: TotpConfig, *, replacing: str | None = None) -> None:
        target = vault.entry_path(config)
        for item in vault.list_records():
            if item["entry"] == target and item["entry"] != replacing:
                raise ValueError("já existe um token com este Issuer e Conta")

    def configured(self) -> bool:
        return bool(vault.get_protection_hash())

    def setup_password(self, password: str) -> None:
        if self.configured():
            raise ValueError("a proteção já foi configurada")
        if len(password) < 8:
            raise ValueError("a senha deve ter ao menos 8 caracteres")
        vault.set_protection_hash(password_hash(password))

    def verify_password(self, password: str) -> bool:
        return password_matches(password, vault.get_protection_hash())

    def authorized_tokens(self, password: str, selector: str = "") -> list[dict[str, Any]]:
        """Autentica uma chamada não-Telegram e devolve somente códigos/metadados."""
        if not self.verify_password(password):
            raise PermissionError("senha do TOTP inválida")
        tokens = self.list_tokens()
        needle = selector.strip().casefold()
        if not needle:
            return tokens
        return [item for item in tokens if needle in f"{item['issuer']} {item['account']}".casefold()]

    def change_password(self, current: str, password: str) -> None:
        if not self.verify_password(current):
            raise PermissionError("senha atual inválida")
        if len(password) < 8:
            raise ValueError("a nova senha deve ter ao menos 8 caracteres")
        vault.set_protection_hash(password_hash(password))

    def list_tokens(self) -> list[dict[str, Any]]:
        result = []
        for item in vault.list_records():
            record = vault.read(item["entry"])
            code, _, _, _, remaining = totp_codes(record.config)
            result.append({"entry": item["entry"], "issuer": record.config.issuer, "account": record.config.account, "code": code, "remaining": round(remaining)})
        return result

    def add(self, *, issuer: str, account: str, secret: str) -> list[dict[str, Any]]:
        config = parse_input(secret) if secret.startswith("otpauth://") else build_config(issuer=issuer, account=account, secret=secret)
        self._ensure_new_entry(config)
        vault.store(config)
        return self.list_tokens()

    def begin_qr(self, data: bytes) -> TotpConfig:
        return parse_input(decode_qr_bytes(data))

    def confirm_qr(self, config: TotpConfig) -> list[dict[str, Any]]:
        self._ensure_new_entry(config)
        vault.store(config)
        return self.list_tokens()

    def edit(self, entry: str, *, issuer: str, account: str) -> list[dict[str, Any]]:
        old = vault.read(entry)
        config = build_config(issuer=issuer or old.config.issuer, account=account or old.config.account, secret=old.config.secret, algorithm=old.config.algorithm, digits=str(old.config.digits), period=str(old.config.period))
        self._ensure_new_entry(config, replacing=old.entry)
        vault.store(config)
        if vault.entry_path(old.config) != vault.entry_path(config):
            vault.delete(old.entry)
        return self.list_tokens()

    def delete(self, entry: str) -> list[dict[str, Any]]:
        vault.delete(entry)
        return self.list_tokens()
