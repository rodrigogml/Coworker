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
MODULE_PATH = (
    PROJECT_ROOT / "skills" / "todoist-manage" / "scripts" / "todoist.py"
)
SPEC = importlib.util.spec_from_file_location("todoist_skill", MODULE_PATH)
todoist = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = todoist
SPEC.loader.exec_module(todoist)


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


class TodoistTests(unittest.TestCase):
    def config(self, *, max_pages: int = 20):
        return todoist.TodoistConfig(
            "https://api.todoist.com/api/v1",
            "APIs/Todoist",
            30,
            100,
            max_pages,
        )

    def test_load_config(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "todoist.toml"
            path.write_text(
                'api_base = "https://api.todoist.com/api/v1/"\n'
                'credential_ref = "APIs/Todoist"\n'
                "timeout_seconds = 25\n"
                "page_size = 150\n"
                "max_pages = 10\n",
                encoding="utf-8",
            )
            config = todoist.load_config(path)

        self.assertEqual("https://api.todoist.com/api/v1", config.api_base)
        self.assertEqual("APIs/Todoist", config.credential_ref)
        self.assertEqual(150, config.page_size)

    def test_config_rejects_credential_exfiltration_host(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "todoist.toml"
            path.write_text(
                'api_base = "https://example.com/api/v1"\n'
                'credential_ref = "APIs/Todoist"\n',
                encoding="utf-8",
            )
            with self.assertRaises(todoist.TodoistToolError):
                todoist.load_config(path)

    def test_client_uses_bearer_without_returning_token(self):
        captured = {}

        def opener(request, *, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse(
                {
                    "results": [
                        {
                            "id": "task1",
                            "content": "Teste",
                            "token": "response-secret",
                        }
                    ],
                    "next_cursor": None,
                }
            )

        client = todoist.TodoistClient(
            self.config(),
            "token-de-teste",
            opener=opener,
        )
        result = client.list(
            "/tasks",
            {"limit": 50},
            all_pages=False,
        )

        self.assertEqual(
            "Bearer token-de-teste",
            captured["request"].get_header("Authorization"),
        )
        self.assertNotIn("token-de-teste", json.dumps(result))
        self.assertNotIn("response-secret", json.dumps(result))
        client.close()
        self.assertEqual("", client._token)

    def test_api_error_redacts_token(self):
        def opener(request, *, timeout):
            del request, timeout
            payload = json.dumps(
                {"error": "token-de-teste não é válido"}
            ).encode("utf-8")
            raise urllib.error.HTTPError(
                "https://api.todoist.com/api/v1/tasks",
                401,
                "Unauthorized",
                {},
                io.BytesIO(payload),
            )

        client = todoist.TodoistClient(
            self.config(),
            "token-de-teste",
            opener=opener,
        )
        with self.assertRaises(todoist.TodoistApiError) as raised:
            client.request("GET", "/tasks")

        self.assertNotIn("token-de-teste", str(raised.exception))
        self.assertIn("[REDACTED]", str(raised.exception))

    def test_all_pages_deduplicates_and_stops_at_limit(self):
        payloads = iter(
            [
                {
                    "results": [{"id": "a"}, {"id": "b"}],
                    "next_cursor": "cursor-2",
                },
                {
                    "results": [{"id": "b"}, {"id": "c"}],
                    "next_cursor": "cursor-3",
                },
            ]
        )
        requests = []

        def opener(request, *, timeout):
            del timeout
            requests.append(request)
            return FakeResponse(next(payloads))

        client = todoist.TodoistClient(
            self.config(max_pages=2),
            "token",
            opener=opener,
        )
        result = client.list("/tasks", {"limit": 100}, all_pages=True)

        self.assertEqual(["a", "b", "c"], [item["id"] for item in result["results"]])
        self.assertTrue(result["pagination"]["truncated"])
        self.assertIn("cursor=cursor-2", requests[1].full_url)

    def test_task_create_dry_run_does_not_call_api(self):
        def opener(*_args, **_kwargs):
            self.fail("A API não deveria ser chamada em dry-run.")

        client = todoist.TodoistClient(
            self.config(),
            "token",
            opener=opener,
        )
        args = argparse.Namespace(
            content="Enviar relatório",
            description=None,
            project_id=None,
            section_id=None,
            parent_id=None,
            labels=["trabalho"],
            priority=2,
            due_string="amanhã",
            due_date=None,
            due_datetime=None,
            dry_run=True,
        )
        result = todoist.tasks_create(client, args)

        self.assertTrue(result["dry_run"])
        self.assertEqual("POST", result["request"]["method"])
        self.assertEqual("/tasks", result["request"]["path"])
        self.assertEqual(2, result["request"]["payload"]["priority"])

    def test_move_requires_exactly_one_destination(self):
        client = todoist.TodoistClient(self.config(), "token")
        args = argparse.Namespace(
            id="task1",
            project_id="project1",
            section_id="section1",
            parent_id=None,
            dry_run=True,
        )
        with self.assertRaises(todoist.TodoistToolError):
            todoist.tasks_move(client, args)

    def test_create_rejects_blank_task_content(self):
        client = todoist.TodoistClient(self.config(), "token")
        args = argparse.Namespace(
            content="   ",
            description=None,
            project_id=None,
            section_id=None,
            parent_id=None,
            labels=None,
            priority=None,
            due_string=None,
            due_date=None,
            due_datetime=None,
            dry_run=True,
        )
        with self.assertRaises(todoist.TodoistToolError):
            todoist.tasks_create(client, args)

    def test_delete_uses_closed_path_and_method(self):
        client = todoist.TodoistClient(self.config(), "token")
        args = argparse.Namespace(id="task1", dry_run=True)
        result = todoist.resource_action(
            client,
            args,
            "tasks",
            "",
            "DELETE",
        )

        self.assertEqual("DELETE", result["request"]["method"])
        self.assertEqual("/tasks/task1", result["request"]["path"])

    def test_parser_never_accepts_token_or_arbitrary_path(self):
        parser = todoist.build_parser()
        help_text = parser.format_help()
        self.assertNotIn("--token", help_text)
        self.assertNotIn("--path", help_text)
        self.assertNotIn("--method", help_text)


if __name__ == "__main__":
    unittest.main()
