from __future__ import annotations

import argparse
import importlib.util
import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from tempfile import TemporaryDirectory


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PROJECT_ROOT / "skills" / "notion-manage" / "scripts" / "notion.py"
SPEC = importlib.util.spec_from_file_location("notion_skill", MODULE_PATH)
notion = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = notion
SPEC.loader.exec_module(notion)


PAGE_ID = "12345678-1234-1234-1234-123456789abc"
OTHER_PAGE_ID = "abcdefab-abcd-abcd-abcd-abcdefabcdef"


class FakeResponse:
    status = 200

    def __init__(self, payload) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        if self.payload is None:
            return b""
        return json.dumps(self.payload).encode("utf-8")


def page(page_id: str, title: str) -> dict:
    return {
        "object": "page",
        "id": page_id,
        "url": f"https://notion.so/{page_id}",
        "properties": {
            "title": {
                "type": "title",
                "title": [{"plain_text": title}],
            }
        },
    }


class NotionTests(unittest.TestCase):
    def config(
        self,
        *,
        max_pages: int = 20,
        scan_max_pages: int = 10,
    ):
        return notion.NotionConfig(
            "https://api.notion.com/v1",
            "2026-03-11",
            "APIs/Notion",
            30,
            50,
            max_pages,
            scan_max_pages,
            0.0,
        )

    def test_load_config(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "notion.toml"
            path.write_text(
                'api_base = "https://api.notion.com/v1/"\n'
                'api_version = "2026-03-11"\n'
                'credential_ref = "APIs/Notion"\n'
                "timeout_seconds = 25\n"
                "page_size = 75\n"
                "max_pages = 12\n"
                "scan_max_pages = 8\n"
                "request_interval_seconds = 0.4\n",
                encoding="utf-8",
            )
            config = notion.load_config(path)

        self.assertEqual("https://api.notion.com/v1", config.api_base)
        self.assertEqual("2026-03-11", config.api_version)
        self.assertEqual(75, config.page_size)
        self.assertEqual(0.4, config.request_interval_seconds)

    def test_config_rejects_exfiltration_host_and_unknown_version(self):
        for api_base, version in (
            ("https://example.com/v1", "2026-03-11"),
            ("https://api.notion.com/v1", "2025-09-03"),
        ):
            with self.subTest(api_base=api_base, version=version):
                with TemporaryDirectory() as temporary:
                    path = Path(temporary) / "notion.toml"
                    path.write_text(
                        f'api_base = "{api_base}"\n'
                        f'api_version = "{version}"\n'
                        'credential_ref = "APIs/Notion"\n',
                        encoding="utf-8",
                    )
                    with self.assertRaises(notion.NotionToolError):
                        notion.load_config(path)

    def test_client_uses_bearer_and_version_without_returning_token(self):
        captured = {}

        def opener(request, *, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse(
                {
                    "id": PAGE_ID,
                    "token": "response-secret",
                    "name": "BOTina token-de-teste",
                }
            )

        client = notion.NotionClient(
            self.config(),
            "token-de-teste",
            opener=opener,
        )
        result = client.request("GET", "/users/me")

        self.assertEqual(
            "Bearer token-de-teste",
            captured["request"].get_header("Authorization"),
        )
        self.assertEqual(
            "2026-03-11",
            captured["request"].get_header("Notion-version"),
        )
        self.assertNotIn("token-de-teste", json.dumps(result))
        self.assertNotIn("response-secret", json.dumps(result))
        self.assertEqual("BOTina [REDACTED]", result["name"])
        client.close()
        self.assertEqual("", client._token)

    def test_api_error_redacts_token(self):
        def opener(request, *, timeout):
            del request, timeout
            payload = json.dumps(
                {
                    "code": "unauthorized",
                    "message": "token-de-teste não é válido",
                }
            ).encode("utf-8")
            raise urllib.error.HTTPError(
                "https://api.notion.com/v1/users/me",
                401,
                "Unauthorized",
                {},
                io.BytesIO(payload),
            )

        client = notion.NotionClient(
            self.config(),
            "token-de-teste",
            opener=opener,
        )
        with self.assertRaises(notion.NotionApiError) as raised:
            client.request("GET", "/users/me")

        self.assertNotIn("token-de-teste", str(raised.exception))
        self.assertIn("[REDACTED]", str(raised.exception))

    def test_search_paginates_and_extracts_titles(self):
        payloads = iter(
            [
                {
                    "results": [page(PAGE_ID, "Primeira")],
                    "has_more": True,
                    "next_cursor": OTHER_PAGE_ID,
                },
                {
                    "results": [page(OTHER_PAGE_ID, "Segunda")],
                    "has_more": False,
                    "next_cursor": None,
                },
            ]
        )
        bodies = []

        def opener(request, *, timeout):
            del timeout
            bodies.append(json.loads(request.data))
            return FakeResponse(next(payloads))

        client = notion.NotionClient(
            self.config(max_pages=2),
            "token",
            opener=opener,
        )
        result = notion.search_pages(
            client,
            query="Nota",
            max_pages=2,
        )

        self.assertEqual(["Primeira", "Segunda"], [
            item["title"] for item in result["results"]
        ])
        self.assertEqual(OTHER_PAGE_ID, bodies[1]["start_cursor"])
        self.assertEqual(
            {"property": "object", "value": "page"},
            bodies[0]["filter"],
        )

    def test_find_content_is_bounded_and_returns_snippet(self):
        responses = iter(
            [
                FakeResponse(
                    {
                        "results": [
                            page(PAGE_ID, "Planejamento"),
                            page(OTHER_PAGE_ID, "Ignorada"),
                        ],
                        "has_more": False,
                        "next_cursor": None,
                    }
                ),
                FakeResponse(
                    {
                        "object": "page_markdown",
                        "id": PAGE_ID,
                        "markdown": "# Plano\n\nA palavra Especial está aqui.",
                        "truncated": False,
                        "unknown_block_ids": [],
                    }
                ),
            ]
        )

        def opener(request, *, timeout):
            del request, timeout
            return next(responses)

        client = notion.NotionClient(
            self.config(scan_max_pages=1),
            "token",
            opener=opener,
        )
        args = argparse.Namespace(
            query="especial",
            title=None,
            max_pages=1,
            case_sensitive=False,
        )
        result = notion.pages_find_content(client, args)

        self.assertEqual(1, result["scan"]["pages_scanned"])
        self.assertEqual("Planejamento", result["matches"][0]["title"])
        self.assertIn("Especial", result["matches"][0]["snippet"])

    def test_create_dry_run_hides_markdown_and_does_not_call_api(self):
        def opener(*_args, **_kwargs):
            self.fail("A API não deveria ser chamada em dry-run.")

        client = notion.NotionClient(self.config(), "token", opener=opener)
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "note.md"
            path.write_text("conteúdo privado de teste", encoding="utf-8")
            args = argparse.Namespace(
                parent_page_id=PAGE_ID,
                title="Nova nota",
                markdown_file=str(path),
                dry_run=True,
            )
            result = notion.pages_create(client, args)

        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("conteúdo privado de teste", serialized)
        self.assertEqual(
            len("conteúdo privado de teste".encode("utf-8")),
            result["request"]["content"]["bytes"],
        )
        self.assertEqual("POST", result["request"]["method"])

    def test_edit_sends_recommended_update_content_shape(self):
        captured = {}

        def opener(request, *, timeout):
            del timeout
            captured["payload"] = json.loads(request.data)
            return FakeResponse(
                {
                    "object": "page_markdown",
                    "id": PAGE_ID,
                    "markdown": "texto novo",
                    "truncated": False,
                    "unknown_block_ids": [],
                }
            )

        client = notion.NotionClient(self.config(), "token", opener=opener)
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "changes.json"
            path.write_text(
                json.dumps([{"old_str": "texto antigo", "new_str": "texto novo"}]),
                encoding="utf-8",
            )
            args = argparse.Namespace(
                id=PAGE_ID,
                changes_file=str(path),
                dry_run=False,
            )
            result = notion.pages_edit(client, args)

        self.assertEqual("update_content", captured["payload"]["type"])
        self.assertEqual(
            "texto antigo",
            captured["payload"]["update_content"]["content_updates"][0]["old_str"],
        )
        self.assertNotIn("markdown", result)
        self.assertEqual(PAGE_ID, result["id"])

    def test_replace_dry_run_hides_content(self):
        client = notion.NotionClient(self.config(), "token")
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "replacement.md"
            path.write_text("# Segredo da nota", encoding="utf-8")
            args = argparse.Namespace(
                id=PAGE_ID,
                markdown_file=str(path),
                dry_run=True,
            )
            result = notion.pages_replace(client, args)

        self.assertNotIn(
            "Segredo da nota",
            json.dumps(result, ensure_ascii=False),
        )
        self.assertEqual(
            f"/pages/{PAGE_ID}/markdown",
            result["request"]["path"],
        )

    def test_trash_and_restore_use_in_trash(self):
        client = notion.NotionClient(self.config(), "token")
        args = argparse.Namespace(id=PAGE_ID, dry_run=True)

        trashed = notion.pages_trash_state(client, args, in_trash=True)
        restored = notion.pages_trash_state(client, args, in_trash=False)

        self.assertEqual({"in_trash": True}, trashed["request"]["payload"])
        self.assertEqual({"in_trash": False}, restored["request"]["payload"])

    def test_changes_file_rejects_extra_fields(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "changes.json"
            path.write_text(
                json.dumps(
                    [{"old_str": "a", "new_str": "b", "replace_all": True}]
                ),
                encoding="utf-8",
            )
            with self.assertRaises(notion.NotionToolError):
                notion.read_changes_file(str(path))

    def test_parser_never_accepts_token_or_arbitrary_request(self):
        parser = notion.build_parser()
        help_text = parser.format_help()
        self.assertNotIn("--token", help_text)
        self.assertNotIn("--path", help_text)
        self.assertNotIn("--method", help_text)


if __name__ == "__main__":
    unittest.main()
