from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (
    PROJECT_ROOT
    / "skills"
    / "forwardemail"
    / "scripts"
    / "forward_email.py"
)
SPEC = importlib.util.spec_from_file_location("forward_email_skill", MODULE_PATH)
forward_email = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = forward_email
SPEC.loader.exec_module(forward_email)


DOMAIN = {
    "id": "d" * 24,
    "name": "example.com",
    "plan": "free",
    "is_verified": True,
}
ALIAS = {
    "id": "a" * 24,
    "name": "contact",
    "recipients": ["destination@example.net"],
    "is_enabled": True,
}


class FakeClient:
    def __init__(self, *, domains=None, aliases=None) -> None:
        self.domains = list(domains if domains is not None else [DOMAIN])
        self.aliases = list(aliases if aliases is not None else [ALIAS])
        self.created_domains = []
        self.created_aliases = []

    def list_domains(self, *, name=None):
        if name is None:
            return [dict(item) for item in self.domains]
        return [
            dict(item)
            for item in self.domains
            if item.get("name", "").lower() == name.lower()
        ]

    def list_aliases(self, domain, *, name=None, recipient=None, query=None):
        del recipient, query
        if domain != DOMAIN["name"]:
            raise AssertionError("domínio inesperado")
        if name is None:
            return [dict(item) for item in self.aliases]
        return [
            dict(item)
            for item in self.aliases
            if item.get("name", "").lower() == name.lower()
        ]

    def get_alias(self, domain, alias_reference):
        if domain != DOMAIN["name"]:
            raise AssertionError("domínio inesperado")
        for item in self.aliases:
            if alias_reference in (item["id"], item["name"]):
                return dict(item)
        raise AssertionError("alias inesperado")

    def create_domain(self, body):
        self.created_domains.append(body)
        return {**DOMAIN, "name": body["domain"]}

    def create_alias(self, domain, body):
        self.created_aliases.append((domain, body))
        return {**ALIAS, **body}


class FakeResponse:
    status = 200

    def __init__(self, payload) -> None:
        self.payload = payload
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class ForwardEmailTests(unittest.TestCase):
    def test_load_config(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "forwardemail.toml"
            path.write_text(
                'api_base = "https://api.forwardemail.net/"\n'
                'credential_ref = "APIs/ForwardEmail"\n'
                "timeout_seconds = 25\n",
                encoding="utf-8",
            )
            config = forward_email.load_config(path)

        self.assertEqual(config.api_base, "https://api.forwardemail.net")
        self.assertEqual(config.credential_ref, "APIs/ForwardEmail")
        self.assertEqual(config.timeout_seconds, 25)

    def test_config_rejects_credential_exfiltration_host(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "forwardemail.toml"
            path.write_text(
                'api_base = "https://example.com"\n'
                'credential_ref = "APIs/ForwardEmail"\n',
                encoding="utf-8",
            )
            with self.assertRaises(forward_email.ForwardEmailToolError):
                forward_email.load_config(path)

    def test_normalizes_domain_and_full_alias_address(self):
        self.assertEqual(
            forward_email.normalize_domain("Example.COM."),
            "example.com",
        )
        self.assertEqual(
            forward_email.normalize_alias_name(
                "Contact@Example.com",
                "example.com",
            ),
            "contact",
        )

    def test_rejects_invalid_domain_and_alias(self):
        with self.assertRaises(forward_email.ForwardEmailToolError):
            forward_email.normalize_domain("localhost")
        with self.assertRaises(forward_email.ForwardEmailToolError):
            forward_email.normalize_alias_name("other@example.net", "example.com")

    def test_sanitizes_secrets_recursively(self):
        payload = {
            "token": "hidden",
            "nested": {
                "api_key": "hidden",
                "smtp_password": "hidden",
                "value": "visible",
            },
        }
        self.assertEqual(
            forward_email.sanitize_payload(payload),
            {"nested": {"value": "visible"}},
        )

    def test_http_client_uses_basic_auth_without_token_in_payload(self):
        captured = {}

        def opener(request, *, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse({"id": "account-id", "email": "me@example.com"})

        config = forward_email.ForwardEmailConfig(
            "https://api.forwardemail.net",
            "APIs/ForwardEmail",
            30,
        )
        client = forward_email.ForwardEmailClient(
            config,
            "test-secret",
            opener=opener,
        )
        account = client.get_account()

        expected = base64.b64encode(b"test-secret:").decode("ascii")
        self.assertEqual(
            captured["request"].get_header("Authorization"),
            f"Basic {expected}",
        )
        self.assertNotIn("test-secret", json.dumps(account))
        client.close()
        self.assertEqual(client._authorization, "")
        self.assertEqual(client._token, "")

    def test_domain_create_dry_run_does_not_mutate(self):
        client = FakeClient(domains=[])
        args = argparse.Namespace(
            domain="new.example.com",
            catchall_recipient=None,
            plan=None,
            team_domain=None,
            dry_run=True,
        )
        result = forward_email.execute_domains_create(client, args)

        self.assertTrue(result["dry_run"])
        self.assertFalse(result["request"]["catchall"])
        self.assertEqual(client.created_domains, [])

    def test_domain_create_is_idempotent(self):
        client = FakeClient()
        args = argparse.Namespace(
            domain="example.com",
            catchall_recipient=None,
            plan=None,
            team_domain=None,
            dry_run=False,
        )
        result = forward_email.execute_domains_create(client, args)

        self.assertFalse(result["changed"])
        self.assertEqual(result["reason"], "domain_already_exists")
        self.assertEqual(client.created_domains, [])

    def test_alias_create_is_idempotent_for_same_recipients(self):
        client = FakeClient()
        args = argparse.Namespace(
            domain="example.com",
            name="contact",
            recipient=["destination@example.net"],
            description=None,
            label=None,
            enabled=None,
            recipient_verification=None,
            imap=None,
            error_code_if_disabled=None,
            dry_run=False,
        )
        result = forward_email.execute_aliases_create(client, args)

        self.assertFalse(result["changed"])
        self.assertEqual(result["reason"], "alias_already_exists")
        self.assertEqual(client.created_aliases, [])

    def test_ambiguous_domain_is_rejected(self):
        duplicate = {**DOMAIN, "id": "e" * 24}
        client = FakeClient(domains=[DOMAIN, duplicate])

        with self.assertRaises(forward_email.ForwardEmailToolError):
            forward_email.exact_domain(client, "example.com")

    def test_parser_never_accepts_a_token_argument(self):
        help_text = forward_email.build_parser().format_help()
        self.assertNotIn("--token", help_text)
        self.assertNotIn("--api-key", help_text)


if __name__ == "__main__":
    unittest.main()
