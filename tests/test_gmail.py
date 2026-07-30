from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PROJECT_ROOT / "skills" / "gmail" / "scripts" / "gmail.py"
SPEC = importlib.util.spec_from_file_location("gmail_skill", MODULE_PATH)
gmail = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gmail
SPEC.loader.exec_module(gmail)


class FakeResponse:
    def __init__(self, payload) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, *_args):
        return json.dumps(self.payload).encode("utf-8")


class GmailTests(unittest.TestCase):
    def config(self, *, max_pages: int = 20):
        return gmail.GmailConfig(
            "https://gmail.googleapis.com/gmail/v1",
            PROJECT_ROOT / "data" / "config" / "google.toml",
            30,
            50,
            max_pages,
            5_242_880,
        )

    def test_load_config(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "gmail.toml"
            path.write_text(
                'api_base = "https://gmail.googleapis.com/gmail/v1"\n'
                'google_config = "data/config/google.toml"\n'
                "timeout_seconds = 20\n"
                "page_size = 75\n"
                "max_pages = 10\n"
                "max_response_bytes = 1000000\n",
                encoding="utf-8",
            )
            config = gmail.load_config(path)

        self.assertEqual(75, config.page_size)
        self.assertEqual(
            PROJECT_ROOT / "data" / "config" / "google.toml",
            config.google_config,
        )

    def test_client_uses_bearer_and_redacts_access_token(self):
        captured = {}

        def opener(request, *, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse(
                {
                    "emailAddress": "pessoal@example.com",
                    "unexpected": "access-secreto",
                    "access_token": "outro-segredo",
                }
            )

        client = gmail.GmailClient(
            self.config(),
            "access-secreto",
            opener=opener,
        )
        result = client.request("GET", "/users/me/profile")

        self.assertEqual(
            "Bearer access-secreto",
            captured["request"].get_header("Authorization"),
        )
        self.assertEqual("[REDACTED]", result["unexpected"])
        self.assertNotIn("access_token", result)
        client.close()
        self.assertEqual("", client._access_token)

    def test_message_list_paginates_to_configured_limit(self):
        payloads = iter(
            [
                {
                    "messages": [{"id": "a"}],
                    "nextPageToken": "next-one",
                    "resultSizeEstimate": 3,
                },
                {
                    "messages": [{"id": "b"}],
                    "nextPageToken": "next-two",
                    "resultSizeEstimate": 3,
                },
            ]
        )
        urls = []

        def opener(request, *, timeout):
            del timeout
            urls.append(request.full_url)
            return FakeResponse(next(payloads))

        client = gmail.GmailClient(
            self.config(max_pages=2),
            "access",
            opener=opener,
        )
        result = gmail.paginate(
            client,
            "/users/me/messages",
            "messages",
            {"maxResults": 50, "q": "is:unread"},
            all_pages=True,
        )

        self.assertEqual(["a", "b"], [item["id"] for item in result["messages"]])
        self.assertIn("pageToken=next-one", urls[1])
        self.assertTrue(result["pagination"]["truncated"])

    def test_draft_create_dry_run_hides_message_body(self):
        client = gmail.GmailClient(self.config(), "access")
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "message.eml"
            path.write_text(
                "From: pessoal@example.com\n"
                "To: destino@example.com\n"
                "Subject: Teste\n\n"
                "corpo privado",
                encoding="utf-8",
            )
            args = argparse.Namespace(
                message_file=str(path),
                thread_id=None,
                dry_run=True,
            )
            result = gmail.draft_create(client, args)

        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("corpo privado", serialized)
        self.assertEqual("/users/me/drafts", result["request"]["path"])

    def test_modify_requires_at_least_one_label(self):
        client = gmail.GmailClient(self.config(), "access")
        args = argparse.Namespace(
            id="message123",
            add_label=[],
            remove_label=[],
            dry_run=True,
        )
        with self.assertRaises(gmail.GmailToolError):
            gmail.message_modify(client, args)

    def test_modify_rejects_same_label_in_both_sets(self):
        client = gmail.GmailClient(self.config(), "access")
        args = argparse.Namespace(
            id="message123",
            add_label=["INBOX"],
            remove_label=["INBOX"],
            dry_run=True,
        )
        with self.assertRaises(gmail.GmailToolError):
            gmail.message_modify(client, args)

    def test_send_draft_dry_run_is_closed_operation(self):
        client = gmail.GmailClient(self.config(), "access")
        args = argparse.Namespace(id="draft123", dry_run=True)
        result = gmail.draft_send(client, args)

        self.assertEqual("POST", result["request"]["method"])
        self.assertEqual("/users/me/drafts/send", result["request"]["path"])
        self.assertEqual("draft123", result["request"]["draft_id"])

    def test_parser_exposes_profile_but_never_token_or_arbitrary_path(self):
        help_text = gmail.build_parser().format_help()
        self.assertIn("--profile", help_text)
        self.assertNotIn("--token", help_text)
        self.assertNotIn("--method", help_text)


if __name__ == "__main__":
    unittest.main()
