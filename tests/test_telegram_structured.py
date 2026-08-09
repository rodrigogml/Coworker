"""Testes dos contratos estruturados e da entrega segura pelo Telegram."""

from __future__ import annotations

import hashlib
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from interfaces.telegram.codex import (
    CodexAdapter,
    CodexCancelledError,
    CodexExecutionError,
    CodexModel,
    CodexOptions,
    CodexProgress,
    ProcessRegistry,
)
from interfaces.telegram.credential_broker import (
    create_request,
    parse_field_spec,
)
from interfaces.telegram.gateway import Gateway
from interfaces.telegram.config import (
    CodexConfig,
    FeedbackConfig,
    MediaConfig,
    PairingConfig,
    ProcessorConfig,
    TelegramConfigError,
    TelegramConfig,
    WebhookConfig,
    load_config,
)
from interfaces.telegram.contracts import Attachment, InboundMessage, ReplyContext, TelegramReceipt
from interfaces.telegram.identity import InstanceIdentity
from interfaces.telegram.job_context import (
    JobContextError,
    write_job_json,
)
from interfaces.telegram.processors import ProcessorError, ProcessorRegistry
from interfaces.telegram.state import StateStore
from interfaces.telegram.telegram_api import TelegramApi, TelegramApiError
from interfaces.telegram.workspace import (
    JobWorkspace,
    WorkspaceError,
    parse_delivery,
    validate_artifact,
)


TEST_IDENTITY = InstanceIdentity(
    "teste",
    "Assistente Teste",
    "pt-BR",
    "neutral",
    "",
    "Assistente usada em testes.",
    "direto",
    "nenhum",
    "moderada",
    "conciso",
    "Identidade fictícia usada somente pelos testes automatizados.",
)


class StructuredStateTests(unittest.TestCase):
    def test_database_created_with_001_receives_all_later_migrations(self) -> None:
        import interfaces.telegram.state as state_module

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "migrations"
            first.mkdir()
            source = state_module.MIGRATIONS_DIR / "001_initial.sql"
            (first / source.name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
            with patch.object(state_module, "MIGRATIONS_DIR", first):
                StateStore(root / "state").close()

            state = StateStore(root / "state")
            try:
                versions = {
                    row[0]
                    for row in state.connection.execute("SELECT version FROM schema_migrations")
                }
                columns = {
                    row[1]
                    for row in state.connection.execute("PRAGMA table_info(codex_preferences)")
                }
            finally:
                state.close()

        self.assertIn("001_initial.sql", versions)
        self.assertIn("002_structured_delivery.sql", versions)
        self.assertIn("004_progress_preferences.sql", versions)
        self.assertIn("progress_mode", columns)

    def test_sent_message_id_and_reference_context_are_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = StateStore(Path(temporary))
            state.update_seen(1)
            record_id = state.record_message(
                1, 10, 20, "out", "resposta", "sent",
                reply_to_message_id=19, thread_id="thread", turn_id="turn",
            )
            stored = state.message(10, 20)
            missing = state.message(10, 999)
            state.close()

        self.assertGreater(record_id, 0)
        self.assertEqual(19, stored["reply_to_message_id"])
        self.assertEqual("thread", stored["thread_id"])
        self.assertIsNone(missing)

    def test_uploading_artifact_becomes_unknown_after_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = StateStore(Path(temporary))
            state.update_seen(1)
            job = state.create_job(1, 10)
            artifact = state.record_artifact(
                job, direction="out", local_path=Path("file.bin"), relative_path="file.bin",
                requested_kind="auto", effective_kind="document", caption="",
                mime_type="application/octet-stream", size_bytes=1, sha256="0" * 64,
            )
            state.mark_artifact(artifact, "uploading")
            state.recover_interrupted_jobs()
            value = state.connection.execute(
                "SELECT upload_state FROM artifacts WHERE id=?", (artifact,)
            ).fetchone()[0]
            state.close()

        self.assertEqual("unknown", value)

    def test_legacy_private_config_receives_safe_defaults_without_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "telegram.toml"
            (root / "identity.toml").write_text(
                '''[identity]\ninstance_id = "teste"\ndisplay_name = "Assistente Teste"\nlanguage = "pt-BR"\ngrammatical_gender = "neutral"\npronouns = ""\nsummary = "Assistente usada em testes."\ntone = "direto"\nhumor = "nenhum"\nenthusiasm = "moderada"\nwriting_style = "conciso"\nbio = "Identidade fictícia usada somente pelos testes automatizados."\n''',
                encoding="utf-8",
            )
            normalized = root.as_posix()
            config_path.write_text(
                f'''transport = "polling"\ncredential_ref = "entry"\nproject_root = "{normalized}"\nstate_dir = "data/telegram/state"\n'''
                "poll_timeout_seconds = 10\nrequest_timeout_seconds = 10\n"
                "[pairing]\nttl_seconds = 600\nmax_attempts = 5\n"
                "[codex]\nexecutable = \"\"\nhome_dir = \"\"\nsandbox = \"workspace-write\"\nnetwork_access = false\napproval_policy = \"never\"\ntimeout_seconds = 60\nadditional_directories = []\n"
                f'''[media]\ninbox_dir = "data/telegram/inbox"\nmax_download_bytes = 1000\n'''
                "[webhook]\npublic_url = \"\"\nsecret_credential_ref = \"\"\nlisten_host = \"127.0.0.1\"\nlisten_port = 8787\n",
                encoding="utf-8",
            )
            before = config_path.read_bytes()
            config = load_config(config_path, require_codex=False)
            after = config_path.read_bytes()

        self.assertEqual("exec", config.codex.backend)
        self.assertEqual(
            Path(__file__).resolve().parents[1] / "instance" / "data" / "telegram" / "jobs",
            config.media.jobs_dir,
        )
        self.assertEqual(20 * 1024 * 1024, config.media.max_upload_bytes)
        self.assertEqual(before, after)

    def test_persistent_paths_outside_instance_data_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "telegram.toml"
            (root / "identity.toml").write_text(
                '''[identity]\ninstance_id = "teste"\ndisplay_name = "Assistente Teste"\nlanguage = "pt-BR"\ngrammatical_gender = "neutral"\npronouns = ""\nsummary = "Assistente usada em testes."\ntone = "direto"\nhumor = "nenhum"\nenthusiasm = "moderada"\nwriting_style = "conciso"\nbio = "Identidade fictícia."\n''',
                encoding="utf-8",
            )
            config_path.write_text(
                'transport = "polling"\ncredential_ref = "entry"\n'
                'state_dir = "C:/fora-da-instancia"\n'
                '[pairing]\nttl_seconds = 600\nmax_attempts = 5\n'
                '[codex]\nhome_dir = "data/codex"\n'
                '[media]\ninbox_dir = "data/telegram/inbox"\n'
                '[webhook]\npublic_url = ""\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(TelegramConfigError, "dentro de"):
                load_config(config_path, require_codex=False)


class WorkspaceTests(unittest.TestCase):
    def test_job_json_writer_is_confined_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = JobWorkspace.create(root / "data" / "telegram" / "jobs", 7)
            environment = {"COWORKER_JOB_DERIVED": str(workspace.derived_dir)}
            document = {"schema_version": 1, "request_id": "request-7"}

            first = write_job_json(
                "omie-account-entry",
                "request-7",
                document,
                project_root=root,
                environment=environment,
            )
            second = write_job_json(
                "omie-account-entry",
                "request-7",
                document,
                project_root=root,
                environment=environment,
            )
            stored = json.loads(first.path.read_text(encoding="utf-8"))
            temporary_files = list(workspace.derived_dir.glob(".*.tmp"))

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.path, second.path)
        self.assertEqual(document, stored)
        self.assertEqual([], temporary_files)

    def test_job_json_writer_rejects_missing_external_and_conflicting_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            workspace = JobWorkspace.create(root / "data" / "telegram" / "jobs", 7)
            environment = {"COWORKER_JOB_DERIVED": str(workspace.derived_dir)}
            first = write_job_json(
                "omie-account-entry",
                "request-7",
                {"value": 1},
                project_root=root,
                environment=environment,
            )
            with self.assertRaises(JobContextError):
                write_job_json(
                    "omie-account-entry",
                    "request-7",
                    {"value": 2},
                    project_root=root,
                    environment=environment,
                )
            with self.assertRaises(JobContextError):
                write_job_json(
                    "../escape",
                    "request-8",
                    {"value": 1},
                    project_root=root,
                    environment=environment,
                )
            with self.assertRaises(JobContextError):
                write_job_json(
                    "omie-account-entry",
                    "request-8",
                    {"value": 1},
                    project_root=root,
                    environment={},
                )
            external = Path(temporary) / "external" / "derived"
            external.mkdir(parents=True)
            with self.assertRaises(JobContextError):
                write_job_json(
                    "omie-account-entry",
                    "request-8",
                    {"value": 1},
                    project_root=root,
                    environment={"COWORKER_JOB_DERIVED": str(external)},
                )

        self.assertTrue(first.path.name.startswith("omie-account-entry-"))

    def test_job_json_writer_handles_identical_concurrent_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = JobWorkspace.create(root / "data" / "telegram" / "jobs", 7)
            environment = {"COWORKER_JOB_DERIVED": str(workspace.derived_dir)}
            barrier = threading.Barrier(2)
            results = []
            failures = []

            def write() -> None:
                try:
                    barrier.wait()
                    results.append(
                        write_job_json(
                            "omie-account-entry",
                            "request-7",
                            {"value": 1},
                            project_root=root,
                            environment=environment,
                        )
                    )
                except Exception as exc:  # pragma: no cover - diagnostic capture
                    failures.append(exc)

            threads = [threading.Thread(target=write) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual([], failures)
        self.assertEqual(2, len(results))
        self.assertEqual(1, sum(result.created for result in results))

    def test_job_json_writer_rejects_symbolic_link_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = JobWorkspace.create(root / "data" / "telegram" / "jobs", 7)
            environment = {"COWORKER_JOB_DERIVED": str(workspace.derived_dir)}
            stored = write_job_json(
                "omie-account-entry",
                "request-7",
                {"value": 1},
                project_root=root,
                environment=environment,
            )
            external = root / "external.json"
            external.write_text('{"value": 1}\n', encoding="utf-8")
            stored.path.unlink()
            try:
                stored.path.symlink_to(external)
            except OSError:
                self.skipTest("Criação de symlink indisponível nesta instalação.")

            with self.assertRaises(JobContextError):
                write_job_json(
                    "omie-account-entry",
                    "request-7",
                    {"value": 1},
                    project_root=root,
                    environment=environment,
                )

    def test_credential_request_uses_only_gateway_job_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = JobWorkspace.create(root / "jobs", 7)
            environment = {
                "COWORKER_JOB_OUTPUT": str(workspace.output_dir),
                "COWORKER_CHAT_ID": "10",
            }
            with patch.dict(os.environ, environment, clear=False):
                request = create_request(
                    "APIs/Omie",
                    "Informe as credenciais da Omie.",
                    [
                        parse_field_spec("username:App Key"),
                        parse_field_spec("password:App Secret"),
                    ],
                    600,
                )

            payload = json.loads(request.request_path.read_text(encoding="utf-8"))

        self.assertEqual(7, payload["job_id"])
        self.assertEqual(10, payload["chat_id"])
        self.assertEqual(["username", "password"], [item["name"] for item in payload["fields"]])
        self.assertNotIn("value", str(payload).lower())

    def test_structured_and_legacy_results_are_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = JobWorkspace.create(Path(temporary), 1)
            (workspace.output_dir / "result.txt").write_text("ok", encoding="utf-8")
            structured = parse_delivery(
                json.dumps({"text": "feito", "artifacts": [{"path": "result.txt", "kind": "auto", "caption": "Resultado"}]}),
                workspace,
                100,
            )
            legacy = parse_delivery("resposta antiga", workspace, 100)

        self.assertEqual("feito", structured.text)
        self.assertEqual("document", structured.artifacts[0].effective_kind)
        self.assertEqual("resposta antiga", legacy.text)
        self.assertFalse(legacy.artifacts)

    def test_structured_result_without_artifacts_is_valid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = JobWorkspace.create(Path(temporary), 1)
            delivery = parse_delivery('{"text":"somente texto","artifacts":[]}', workspace, 100)

        self.assertEqual("somente texto", delivery.text)
        self.assertEqual((), delivery.artifacts)

    def test_artifact_must_exist_inside_output_and_respect_limits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = JobWorkspace.create(root, 1)
            valid = workspace.output_dir / "valid.bin"
            valid.write_bytes(b"abc")
            artifact = validate_artifact(workspace, "valid.bin", "document", "", 3)
            with self.assertRaises(WorkspaceError):
                validate_artifact(workspace, "../outside.bin", "auto", "", 10)
            with self.assertRaises(WorkspaceError):
                validate_artifact(workspace, "missing.bin", "auto", "", 10)
            empty = workspace.output_dir / "empty.bin"
            empty.touch()
            with self.assertRaises(WorkspaceError):
                validate_artifact(workspace, "empty.bin", "auto", "", 10)
            with self.assertRaises(WorkspaceError):
                validate_artifact(workspace, "valid.bin", "auto", "", 2)

        self.assertEqual(hashlib.sha256(b"abc").hexdigest(), artifact.sha256)

    def test_png_with_alpha_is_sent_as_document(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = JobWorkspace.create(Path(temporary), 1)
            png = workspace.output_dir / "alpha.png"
            header = bytearray(26)
            header[:8] = b"\x89PNG\r\n\x1a\n"
            header[12:16] = b"IHDR"
            header[25] = 6
            png.write_bytes(header)
            artifact = validate_artifact(workspace, "alpha.png", "auto", "", 100)

        self.assertEqual("document", artifact.effective_kind)

    def test_symlink_is_rejected_when_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = JobWorkspace.create(root, 1)
            outside = root / "outside.txt"
            outside.write_text("x", encoding="utf-8")
            link = workspace.output_dir / "link.txt"
            try:
                link.symlink_to(outside)
            except OSError:
                self.skipTest("Criação de symlink indisponível nesta instalação.")
            with self.assertRaises(WorkspaceError):
                validate_artifact(workspace, "link.txt", "auto", "", 100)

    def test_publish_artifact_uses_only_gateway_output_and_does_not_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "output"
            output.mkdir()
            source = root / "image.png"
            source.write_bytes(b"image")
            script = Path(__file__).resolve().parents[1] / "instance" / "interfaces" / "telegram" / "scripts" / "publish_artifact.py"
            environment = {**__import__("os").environ, "COWORKER_JOB_OUTPUT": str(output)}
            first = subprocess.run(
                [sys.executable, str(script), str(source)], capture_output=True,
                text=True, encoding="utf-8", env=environment, check=False,
            )
            second = subprocess.run(
                [sys.executable, str(script), str(source)], capture_output=True,
                text=True, encoding="utf-8", env=environment, check=False,
            )

        self.assertEqual(0, first.returncode)
        self.assertEqual(0, second.returncode)
        self.assertNotEqual(json.loads(first.stdout)["path"], json.loads(second.stdout)["path"])


class TelegramUploadTests(unittest.TestCase):
    def test_split_text_replies_only_with_the_first_chunk(self) -> None:
        api = TelegramApi("test-token", 10)
        with patch.object(api, "call", return_value={"message_id": 1}) as call:
            receipts = api.send_text(10, "linha\n" * 1000, reply_to_message_id=9)

        self.assertGreater(len(receipts), 1)
        self.assertEqual({"message_id": 9}, call.call_args_list[0].args[1]["reply_parameters"])
        self.assertNotIn("reply_parameters", call.call_args_list[1].args[1])

    def test_photo_and_document_use_native_multipart_methods(self) -> None:
        api = TelegramApi("test-token", 10)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "file.bin"
            path.write_bytes(b"value")
            with patch.object(
                api,
                "call_multipart",
                side_effect=[
                    {"message_id": 1, "photo": [{"file_id": "photo-id"}]},
                    {"message_id": 2, "document": {"file_id": "document-id"}},
                ],
            ) as call:
                photo = api.send_photo(10, path, reply_to_message_id=9)
                document = api.send_document(10, path)

        self.assertEqual("photo-id", photo.file_id)
        self.assertEqual("document-id", document.file_id)
        self.assertEqual("sendPhoto", call.call_args_list[0].args[0])
        self.assertEqual({"message_id": 9}, call.call_args_list[0].args[1]["reply_parameters"])
        self.assertEqual("sendDocument", call.call_args_list[1].args[0])

    def test_multipart_contains_file_and_never_places_token_in_body(self) -> None:
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"ok":true,"result":{"message_id":1}}'

        api = TelegramApi("secret-token", 10)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "file.txt"
            path.write_text("content", encoding="utf-8")
            with patch("interfaces.telegram.telegram_api.urllib.request.urlopen", return_value=Response()) as open_url:
                result = api.call_multipart("sendDocument", {"chat_id": 10}, {"document": path})
        request = open_url.call_args.args[0]
        body = request.data

        self.assertEqual(1, result["message_id"])
        self.assertIn(b'name="document"; filename="file.txt"', body)
        self.assertIn(b"content", body)
        self.assertNotIn(b"secret-token", body)


class ProcessorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = ProcessorRegistry(ProcessorConfig(1000, 2, 1000, 10, 60, 5))

    def test_text_and_json_are_prepared_without_external_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            text = root / "file.txt"
            text.write_text("conteúdo", encoding="utf-8")
            data = root / "file.json"
            data.write_text('{"ok": true}', encoding="utf-8")
            prepared_text = self.registry.prepare(Attachment("current", "1", local_path=text))
            prepared_json = self.registry.prepare(Attachment("current", "2", local_path=data))

        self.assertEqual("conteúdo", prepared_text.text)
        self.assertIn('"ok": true', prepared_json.text)

    def test_zip_member_limit_blocks_archive_bombs_early(self) -> None:
        import zipfile

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "many.zip"
            with zipfile.ZipFile(path, "w") as archive:
                for index in range(3):
                    archive.writestr(f"{index}.txt", "x")
            with self.assertRaises(ProcessorError):
                self.registry.prepare(Attachment("current", "1", local_path=path))


class AppServerBackendTests(unittest.TestCase):
    def test_app_server_error_keeps_sanitized_reason(self) -> None:
        responses: queue.Queue[str | None] = queue.Queue()
        responses.put(json.dumps({
            "id": 3,
            "error": {"code": "invalid_params", "message": "sandbox policy inválida"},
        }))
        with self.assertRaisesRegex(CodexExecutionError, "invalid_params: sandbox policy inválida"):
            CodexAdapter._wait_app_server_response(responses, 3, time.monotonic() + 1)

    def _adapter(self, root: Path) -> CodexAdapter:
        executable = root / "codex.exe"
        executable.touch()
        config = CodexConfig(
            executable, root / "home", "workspace-write", True, "never", 60, (),
            "app-server", root / "home" / "generated_images",
        )
        return CodexAdapter(config, root, ProcessRegistry())

    def test_app_server_uses_local_image_and_authoritative_completed_events(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "image.png"
            image.write_bytes(b"image")
            schema = root / "schema.json"
            schema.write_text('{"type":"object"}', encoding="utf-8")
            adapter = self._adapter(root)
            process = SimpleNamespace(pid=123)
            events: queue.Queue[str | None] = queue.Queue()
            events.put(json.dumps({"method": "item/completed", "params": {"item": {"type": "agentMessage", "text": '{"text":"ok","artifacts":[]}'}}}))
            events.put(json.dumps({"method": "turn/completed", "params": {"turn": {"id": "turn-1", "status": "completed"}}}))
            with (
                patch.object(adapter, "_start_app_server", return_value=(process, events)),
                patch.object(adapter, "_initialize_app_server"),
                patch.object(adapter, "_wait_app_server_response", side_effect=[{"thread": {"id": "thread-1"}}, {"turn": {"id": "turn-1"}}]),
                patch.object(adapter, "_send_app_server_message") as send,
                patch.object(adapter, "_stop_process"),
            ):
                result = adapter.run(
                    10,
                    "pedido",
                    None,
                    [image],
                    output_schema=schema,
                    options=CodexOptions("gpt-test", "high", "fast", "low"),
                )

        turn_start = next(
            call.args[1]
            for call in send.call_args_list
            if call.args[1].get("method") == "turn/start"
        )
        self.assertIn({"type": "localImage", "path": str(image)}, turn_start["params"]["input"])
        self.assertEqual({"type": "object"}, turn_start["params"]["outputSchema"])
        self.assertEqual("gpt-test", turn_start["params"]["model"])
        self.assertEqual("high", turn_start["params"]["effort"])
        self.assertEqual("fast", turn_start["params"]["serviceTier"])
        self.assertEqual("thread-1", result.thread_id)
        self.assertEqual("turn-1", result.turn_id)

    def test_model_catalog_preserves_dynamic_reasoning_efforts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            adapter = self._adapter(Path(temporary))
            process = SimpleNamespace()
            with (
                patch.object(adapter, "_start_app_server", return_value=(process, queue.Queue())),
                patch.object(adapter, "_initialize_app_server"),
                patch.object(
                    adapter,
                    "_wait_app_server_response",
                    return_value={
                        "data": [
                            {
                                "model": "gpt-test",
                                "displayName": "GPT Test",
                                "isDefault": True,
                                "defaultReasoningEffort": "max",
                                "supportedReasoningEfforts": [
                                    {"reasoningEffort": "max"},
                                    {"reasoningEffort": "ultra"},
                                ],
                                "additionalSpeedTiers": ["fast"],
                            }
                        ]
                    },
                ),
                patch.object(adapter, "_send_app_server_message"),
                patch.object(adapter, "_stop_process"),
            ):
                models = adapter.models(force=True)

        self.assertEqual("gpt-test", models[0].model)
        self.assertEqual(("max", "ultra"), models[0].supported_reasoning_efforts)
        self.assertTrue(models[0].supports_fast)

    def test_app_server_streams_only_commentary_and_sanitized_milestones(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            adapter = self._adapter(root)
            process = SimpleNamespace(pid=123)
            events: queue.Queue[str | None] = queue.Queue()
            for event in (
                {
                    "method": "item/started",
                    "params": {
                        "item": {
                            "id": "comment-1",
                            "type": "agentMessage",
                            "phase": "commentary",
                            "text": "",
                        }
                    },
                },
                {
                    "method": "item/agentMessage/delta",
                    "params": {"itemId": "comment-1", "delta": "Verificando agora."},
                },
                {
                    "method": "item/completed",
                    "params": {
                        "item": {
                            "id": "comment-1",
                            "type": "agentMessage",
                            "phase": "commentary",
                            "text": "Verificando agora.",
                        }
                    },
                },
                {
                    "method": "item/started",
                    "params": {
                        "item": {
                            "id": "command-1",
                            "type": "commandExecution",
                            "command": "programa --token SEGREDO",
                        }
                    },
                },
                {
                    "method": "item/completed",
                    "params": {
                        "item": {
                            "id": "reason-1",
                            "type": "reasoning",
                            "content": "raciocínio privado",
                        }
                    },
                },
                {
                    "method": "item/completed",
                    "params": {
                        "item": {
                            "id": "final-1",
                            "type": "agentMessage",
                            "phase": "final_answer",
                            "text": '{"text":"ok","artifacts":[]}',
                        }
                    },
                },
                {
                    "method": "turn/completed",
                    "params": {"turn": {"id": "turn-1", "status": "completed"}},
                },
            ):
                events.put(json.dumps(event))
            progress: list[CodexProgress] = []
            with (
                patch.object(adapter, "_start_app_server", return_value=(process, events)),
                patch.object(adapter, "_initialize_app_server"),
                patch.object(
                    adapter,
                    "_wait_app_server_response",
                    side_effect=[{"thread": {"id": "thread-1"}}, {"turn": {"id": "turn-1"}}],
                ),
                patch.object(adapter, "_send_app_server_message"),
                patch.object(adapter, "_stop_process"),
            ):
                result = adapter.run(10, "pedido", None, [], on_progress=progress.append)

        combined = " ".join(item.text for item in progress)
        self.assertEqual('{"text":"ok","artifacts":[]}', result.final_message)
        self.assertIn("Verificando agora.", combined)
        self.assertIn("Executando uma ferramenta local.", combined)
        self.assertNotIn("SEGREDO", combined)
        self.assertNotIn("raciocínio privado", combined)

    def test_job_environment_exposes_only_broker_and_workspace_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            adapter = self._adapter(root)
            output = root / "jobs" / "1" / "output"

            environment = adapter._job_environment(output, 10)

        self.assertEqual(str(output), environment["COWORKER_JOB_OUTPUT"])
        self.assertEqual(
            str(output.parent / "input"),
            environment["COWORKER_JOB_INPUT"],
        )
        self.assertEqual(
            str(output.parent / "derived"),
            environment["COWORKER_JOB_DERIVED"],
        )
        self.assertEqual("10", environment["COWORKER_CHAT_ID"])

    def test_app_server_interrupted_turn_is_reported_as_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            adapter = self._adapter(root)
            process = SimpleNamespace(pid=123)
            events: queue.Queue[str | None] = queue.Queue()
            events.put(json.dumps({"method": "turn/completed", "params": {"turn": {"id": "turn-1", "status": "interrupted"}}}))
            with (
                patch.object(adapter, "_start_app_server", return_value=(process, events)),
                patch.object(adapter, "_initialize_app_server"),
                patch.object(adapter, "_wait_app_server_response", side_effect=[{"thread": {"id": "thread-1"}}, {"turn": {"id": "turn-1"}}]),
                patch.object(adapter, "_send_app_server_message"),
                patch.object(adapter, "_stop_process"),
            ):
                with self.assertRaises(CodexCancelledError):
                    adapter.run(10, "pedido", None, [])

    def test_app_server_turn_error_is_reported_without_raw_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            adapter = self._adapter(root)
            process = SimpleNamespace(pid=123)
            events: queue.Queue[str | None] = queue.Queue()
            events.put(json.dumps({
                "method": "turn/completed",
                "params": {"turn": {
                    "id": "turn-1",
                    "status": "failed",
                    "error": {
                        "code": "model_error",
                        "message": "Falha\nsem detalhes do prompt",
                        "debugPayload": "NAO-DEVOLVER-ISTO",
                    },
                }},
            }))
            with (
                patch.object(adapter, "_start_app_server", return_value=(process, events)),
                patch.object(adapter, "_initialize_app_server"),
                patch.object(
                    adapter,
                    "_wait_app_server_response",
                    side_effect=[{"thread": {"id": "thread-1"}}, {"turn": {"id": "turn-1"}}],
                ),
                patch.object(adapter, "_send_app_server_message"),
                patch.object(adapter, "_stop_process"),
            ):
                with self.assertRaisesRegex(
                    CodexExecutionError,
                    r"status 'failed': model_error: Falha sem detalhes do prompt",
                ) as raised:
                    adapter.run(10, "pedido", None, [])

        self.assertNotIn("NAO-DEVOLVER-ISTO", str(raised.exception))

    def test_app_server_failed_turn_without_error_keeps_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            adapter = self._adapter(root)
            process = SimpleNamespace(pid=123)
            events: queue.Queue[str | None] = queue.Queue()
            events.put(json.dumps({
                "method": "turn/completed",
                "params": {"turn": {"id": "turn-1", "status": "failed"}},
            }))
            with (
                patch.object(adapter, "_start_app_server", return_value=(process, events)),
                patch.object(adapter, "_initialize_app_server"),
                patch.object(
                    adapter,
                    "_wait_app_server_response",
                    side_effect=[{"thread": {"id": "thread-1"}}, {"turn": {"id": "turn-1"}}],
                ),
                patch.object(adapter, "_send_app_server_message"),
                patch.object(adapter, "_stop_process"),
            ):
                with self.assertRaisesRegex(
                    CodexExecutionError,
                    r"status 'failed', sem resposta final",
                ):
                    adapter.run(10, "pedido", None, [])


class _FakeTelegramApi:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, int | None]] = []
        self.deleted: list[tuple[int, int]] = []
        self.typing: list[int] = []
        self.edited: list[tuple[int, int, str, dict | None]] = []
        self.callbacks: list[tuple[str, str | None, bool]] = []
        self.drafts: list[tuple[int, int, str]] = []

    def send_text(
        self, chat_id: int, text: str, *, reply_to_message_id: int | None = None,
        reply_markup=None,
    ):
        self.sent.append((chat_id, text, reply_to_message_id))
        return [TelegramReceipt(100 + len(self.sent))]

    def edit_text(self, chat_id: int, message_id: int, text: str, *, reply_markup=None):
        self.edited.append((chat_id, message_id, text, reply_markup))
        return TelegramReceipt(message_id)

    def answer_callback_query(self, callback_id: str, text=None, *, show_alert=False):
        self.callbacks.append((callback_id, text, show_alert))
        return True

    def close(self) -> None:
        pass

    def delete_message(self, chat_id: int, message_id: int) -> bool:
        self.deleted.append((chat_id, message_id))
        return True

    def send_typing(self, chat_id: int) -> None:
        self.typing.append(chat_id)

    def send_draft(self, chat_id: int, draft_id: int, text: str) -> bool:
        self.drafts.append((chat_id, draft_id, text))
        return True

    def set_profile(self, **_values):
        return True


class GatewayContextTests(unittest.TestCase):
    def _gateway(self, root: Path) -> Gateway:
        executable = root / "codex.exe"
        executable.touch()
        config = TelegramConfig(
            TEST_IDENTITY, "polling", "credential", root, root / "state", 10, 10,
            PairingConfig(600, 5),
            CodexConfig(executable, root / "codex", "workspace-write", False, "never", 60, ()),
            MediaConfig(root / "inbox", root / "jobs", 1000, 1000),
            ProcessorConfig(1000, 10, 1000, 10, 60, 5),
            WebhookConfig("", "", "127.0.0.1", 8787),
        )
        return Gateway(config, _FakeTelegramApi())

    def test_acknowledgement_distinguishes_immediate_work_from_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            gateway = self._gateway(Path(temporary))
            gateway.config = replace(
                gateway.config,
                feedback=FeedbackConfig(("Agora",), ("Fila",), 0.01),
            )
            with gateway.state.connection:
                gateway.state.connection.execute(
                    """INSERT INTO authorized_users
                       (user_id,chat_id,role,display_name,paired_at)
                       VALUES (10,10,'owner','Pessoa Teste','2026-01-01T00:00:00Z')"""
                )
            first = {
                "message": {
                    "message_id": 20,
                    "chat": {"id": 10, "type": "private"},
                    "from": {"id": 10},
                    "text": "primeiro pedido",
                }
            }
            second = {
                "message": {
                    "message_id": 21,
                    "chat": {"id": 10, "type": "private"},
                    "from": {"id": 10},
                    "text": "segundo pedido",
                }
            }

            gateway._handle_update(1, first)
            gateway._handle_update(2, second)
            sent = list(gateway.api.sent)
            gateway.close()

        self.assertEqual((10, "Agora", 20), sent[0])
        self.assertEqual((10, "Fila", 21), sent[1])

    def test_progress_mode_is_snapshotted_when_request_enters_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            gateway = self._gateway(Path(temporary))
            with gateway.state.connection:
                gateway.state.connection.execute(
                    """INSERT INTO authorized_users
                       (user_id,chat_id,role,display_name,paired_at)
                       VALUES (10,10,'owner','Pessoa Teste','2026-01-01T00:00:00Z')"""
                )
            gateway.state.set_codex_preference(10, "progress_mode", "compact")
            gateway._handle_update(
                1,
                {
                    "message": {
                        "message_id": 20,
                        "chat": {"id": 10, "type": "private"},
                        "from": {"id": 10},
                        "text": "pedido",
                    }
                },
            )
            queued = gateway.work.get_nowait()
            gateway.state.set_codex_preference(10, "progress_mode", "off")
            gateway.close()

        self.assertIsNotNone(queued)
        self.assertEqual("compact", queued.progress_mode)

    def test_typing_is_renewed_until_work_stops(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            gateway = self._gateway(Path(temporary))
            gateway.config = replace(
                gateway.config,
                feedback=FeedbackConfig(("Agora",), ("Fila",), 0.01),
            )
            stop = threading.Event()
            thread = threading.Thread(target=gateway._typing_loop, args=(10, stop))
            thread.start()
            time.sleep(0.035)
            stop.set()
            thread.join(timeout=1)
            typing = list(gateway.api.typing)
            gateway.close()

        self.assertGreaterEqual(len(typing), 2)

    def test_reply_context_recovers_thread_and_turn_from_database(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            gateway = self._gateway(Path(temporary))
            gateway.state.record_message(
                None, 10, 20, "out", "resposta", "sent",
                thread_id="thread-1", turn_id="turn-1",
            )
            context = gateway._reply_context(
                10,
                {
                    "quote": {"text": "trecho"},
                    "reply_to_message": {
                        "message_id": 20,
                        "chat": {"id": 10},
                        "from": {"is_bot": True},
                        "text": "resposta",
                    },
                },
            )
            gateway.close()

        self.assertEqual("thread-1", context.thread_id)
        self.assertEqual("turn-1", context.turn_id)
        self.assertEqual("trecho", context.quote)

    def test_resume_requires_referenced_bot_thread(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            gateway = self._gateway(Path(temporary))
            gateway.state.update_seen(1)
            context = ReplyContext(
                20, TEST_IDENTITY.display_name, thread_id="thread-1", turn_id="turn-1"
            )
            gateway._handle_command(1, 10, "/resume", "", context)
            active = gateway.state.session(10)
            gateway.close()

        self.assertEqual("thread-1", active)

    def test_authorized_callback_updates_speed_and_edits_panel(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            gateway = self._gateway(Path(temporary))
            with gateway.state.connection:
                gateway.state.connection.execute(
                    """INSERT INTO authorized_users
                       (user_id,chat_id,role,display_name,paired_at)
                       VALUES (10,10,'owner','Pessoa Teste','2026-01-01T00:00:00Z')"""
                )
            model = CodexModel("gpt-test", "GPT Test", "low", ("low",), True, True)
            with patch.object(gateway.codex, "models", return_value=(model,)):
                gateway._handle_update(
                    1,
                    {
                        "callback_query": {
                            "id": "callback-1",
                            "from": {"id": 10},
                            "data": "cx:ss:standard",
                            "message": {
                                "message_id": 20,
                                "date": int(time.time()),
                                "chat": {"id": 10, "type": "private"},
                            },
                        }
                    },
                )
            preferences = gateway.state.codex_preferences(10)
            callbacks = list(gateway.api.callbacks)
            edited = list(gateway.api.edited)
            gateway.close()

        self.assertEqual("standard", preferences.speed)
        self.assertEqual(("callback-1", None, False), callbacks[0])
        self.assertEqual((10, 20), edited[0][:2])

    def test_progress_command_and_callback_persist_validated_modes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            gateway = self._gateway(Path(temporary))
            gateway.state.update_seen(1)
            gateway._handle_codex_command(1, 10, "/progress", "compact")
            self.assertEqual("compact", gateway.state.codex_preferences(10).progress_mode)

            with gateway.state.connection:
                gateway.state.connection.execute(
                    """INSERT INTO authorized_users
                       (user_id,chat_id,role,display_name,paired_at)
                       VALUES (10,10,'owner','Pessoa Teste','2026-01-01T00:00:00Z')"""
                )
            gateway._handle_update(
                2,
                {
                    "callback_query": {
                        "id": "callback-progress",
                        "from": {"id": 10},
                        "data": "cx:ps:detailed",
                        "message": {
                            "message_id": 20,
                            "date": int(time.time()),
                            "chat": {"id": 10, "type": "private"},
                        },
                    }
                },
            )
            mode = gateway.state.codex_preferences(10).progress_mode
            gateway.close()

        self.assertEqual("detailed", mode)

    def test_expired_callback_does_not_change_preferences(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            gateway = self._gateway(Path(temporary))
            with gateway.state.connection:
                gateway.state.connection.execute(
                    """INSERT INTO authorized_users
                       (user_id,chat_id,role,display_name,paired_at)
                       VALUES (10,10,'owner','Pessoa Teste','2026-01-01T00:00:00Z')"""
                )
            gateway._handle_update(
                1,
                {
                    "callback_query": {
                        "id": "callback-expired",
                        "from": {"id": 10},
                        "data": "cx:ss:fast",
                        "message": {
                            "message_id": 20,
                            "date": int(time.time()) - 3600,
                            "chat": {"id": 10, "type": "private"},
                        },
                    }
                },
            )
            preferences = gateway.state.codex_preferences(10)
            callback = gateway.api.callbacks[0]
            gateway.close()

        self.assertIsNone(preferences.speed)
        self.assertTrue(callback[2])

    def test_fast_mode_is_refused_when_model_does_not_advertise_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            gateway = self._gateway(Path(temporary))
            model = CodexModel("gpt-mini", "GPT Mini", "low", ("low",), True, False)
            gateway.state.update_seen(1)
            with patch.object(gateway.codex, "models", return_value=(model,)):
                gateway._handle_codex_command(1, 10, "/speed", "fast")
            preferences = gateway.state.codex_preferences(10)
            sent = list(gateway.api.sent)
            gateway.close()

        self.assertIsNone(preferences.speed)
        self.assertIn("não oferece Fast mode", sent[-1][1])

    def test_secret_capture_never_persists_or_queues_plaintext(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            gateway = self._gateway(Path(temporary))
            with gateway.state.connection:
                gateway.state.connection.execute(
                    """INSERT INTO authorized_users
                       (user_id,chat_id,role,display_name,paired_at)
                       VALUES (10,10,'owner','Pessoa Teste','2026-01-01T00:00:00Z')"""
                )
            command = {
                "message": {
                    "message_id": 20,
                    "chat": {"id": 10, "type": "private"},
                    "from": {"id": 10},
                    "text": "/secret Todoist",
                }
            }
            secret = {
                "message": {
                    "message_id": 21,
                    "chat": {"id": 10, "type": "private"},
                    "from": {"id": 10},
                    "text": "token-secreto",
                }
            }
            with patch("interfaces.telegram.gateway.write_entry_secret") as write:
                gateway._handle_update(1, command)
                gateway._handle_update(2, secret)

            stored = gateway.state.message(10, 21)
            sent = str(gateway.api.sent)
            deleted = list(gateway.api.deleted)
            queued = gateway.work.qsize()
            gateway.close()

        write.assert_called_once_with("APIs/Todoist", "token-secreto")
        self.assertEqual("[Censurado por segurança]", stored["text"])
        self.assertNotIn("token-secreto", str(stored))
        self.assertNotIn("token-secreto", sent)
        self.assertEqual([(10, 21)], deleted)
        self.assertEqual(0, queued)

    def test_codex_broker_captures_username_and_password_without_plaintext(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            gateway = self._gateway(root)
            with gateway.state.connection:
                gateway.state.connection.execute(
                    """INSERT INTO authorized_users
                       (user_id,chat_id,role,display_name,paired_at)
                       VALUES (10,10,'owner','Pessoa Teste','2026-01-01T00:00:00Z')"""
                )
            gateway.state.update_seen(1)
            job_id = gateway.state.create_job(1, 10)
            gateway.state.update_job(job_id, "running", pid=123)
            workspace = JobWorkspace.create(root / "jobs", job_id)
            with patch.dict(
                os.environ,
                {
                    "COWORKER_JOB_OUTPUT": str(workspace.output_dir),
                    "COWORKER_CHAT_ID": "10",
                },
                clear=False,
            ):
                request = create_request(
                    "APIs/Omie",
                    "Ative a integração Omie.",
                    [
                        parse_field_spec("username:App Key"),
                        parse_field_spec("password:App Secret"),
                    ],
                    600,
                )
            gateway._scan_credential_requests()
            username = {
                "message": {
                    "message_id": 21,
                    "chat": {"id": 10, "type": "private"},
                    "from": {"id": 10},
                    "text": "app-key-protegida",
                }
            }
            password = {
                "message": {
                    "message_id": 22,
                    "chat": {"id": 10, "type": "private"},
                    "from": {"id": 10},
                    "text": "app-secret-protegido",
                }
            }
            with patch(
                "interfaces.telegram.gateway.write_entry_credentials"
            ) as write:
                gateway._handle_update(2, username)
                gateway._handle_update(3, password)

            response = json.loads(request.response_path.read_text(encoding="utf-8"))
            stored_username = gateway.state.message(10, 21)
            stored_password = gateway.state.message(10, 22)
            sent = str(gateway.api.sent)
            deleted = list(gateway.api.deleted)
            gateway.close()

        write.assert_called_once_with(
            "APIs/Omie", "app-key-protegida", "app-secret-protegido"
        )
        self.assertTrue(response["ok"])
        self.assertFalse(response["secret_exposed"])
        self.assertEqual("[Censurado por segurança]", stored_username["text"])
        self.assertEqual("[Censurado por segurança]", stored_password["text"])
        self.assertNotIn("app-key-protegida", sent)
        self.assertNotIn("app-secret-protegido", sent)
        self.assertEqual([(10, 21), (10, 22)], deleted)

    def test_credential_prompt_retries_after_transient_telegram_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            gateway = self._gateway(root)
            with gateway.state.connection:
                gateway.state.connection.execute(
                    """INSERT INTO authorized_users
                       (user_id,chat_id,role,display_name,paired_at)
                       VALUES (10,10,'owner','Pessoa Teste','2026-01-01T00:00:00Z')"""
                )
            gateway.state.update_seen(1)
            job_id = gateway.state.create_job(1, 10)
            gateway.state.update_job(job_id, "running", pid=123)
            workspace = JobWorkspace.create(root / "jobs", job_id)
            with patch.dict(
                os.environ,
                {
                    "COWORKER_JOB_OUTPUT": str(workspace.output_dir),
                    "COWORKER_CHAT_ID": "10",
                },
                clear=False,
            ):
                request = create_request(
                    "APIs/SSH",
                    "Configure o acesso protegido.",
                    [
                        parse_field_spec("username:Usuário"),
                        parse_field_spec("attachment:Chave privada"),
                    ],
                    600,
                )

            original_send = gateway.api.send_text
            attempts = 0

            def send_with_one_transient_failure(*args, **kwargs):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise TelegramApiError("falha transitória")
                return original_send(*args, **kwargs)

            with patch.object(gateway.api, "send_text", side_effect=send_with_one_transient_failure):
                # The first attempt must not create a terminal response.
                gateway._scan_credential_requests()
                self.assertFalse(request.response_path.exists())
                self.assertNotIn(request.request_id, gateway.credential_requests_seen)
                self.assertNotIn(10, gateway.credential_captures)

                # Simulate the retry interval having elapsed.
                gateway.credential_request_retry_at[request.request_id] = 0
                gateway._scan_credential_requests()

            self.assertIn(request.request_id, gateway.credential_requests_seen)
            self.assertIn(10, gateway.credential_captures)
            self.assertTrue(any("Envie agora: Usuário" in item[1] for item in gateway.api.sent))
            gateway.close()

    def test_reference_without_persisted_content_uses_update_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            gateway = self._gateway(Path(temporary))
            context = gateway._reply_context(
                10,
                {
                    "reply_to_message": {
                        "message_id": 404,
                        "chat": {"id": 10},
                        "from": {"is_bot": False},
                    }
                },
            )
            gateway.close()

        self.assertEqual("update", context.source)
        self.assertIsNone(context.text)
        self.assertIsNone(context.thread_id)

    def test_resume_without_valid_reference_does_not_change_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            gateway = self._gateway(Path(temporary))
            gateway.state.update_seen(1)
            gateway._handle_command(1, 10, "/resume", "", None)
            active = gateway.state.session(10)
            sent = gateway.api.sent[-1][1]
            gateway.close()

        self.assertIsNone(active)
        self.assertIn("Responda com /resume", sent)

    def test_album_updates_are_combined_into_one_work_item(self) -> None:
        class Timer:
            def __init__(self, _seconds, callback, args=()):
                self.callback = callback
                self.args = args
                self.daemon = False

            def start(self):
                pass

            def cancel(self):
                pass

        with tempfile.TemporaryDirectory() as temporary:
            gateway = self._gateway(Path(temporary))
            gateway.state.update_seen(1)
            gateway.state.update_seen(2)
            first_record = gateway.state.record_message(1, 10, 11, "in", "legenda", "received")
            second_record = gateway.state.record_message(2, 10, 12, "in", None, "received")
            first = InboundMessage((1,), 10, 10, (11,), "legenda", "album", (Attachment("current", "f1"),))
            second = InboundMessage((2,), 10, 10, (12,), "", "album", (Attachment("current", "f2"),))
            with patch("interfaces.telegram.gateway.threading.Timer", Timer):
                gateway._queue_album(first, {"message_id": 11}, first_record)
                gateway._queue_album(second, {"message_id": 12}, second_record)
            gateway._flush_album((10, "album"))
            work = gateway.work.get_nowait()
            gateway.close()

        self.assertEqual((1, 2), work.inbound.update_ids)
        self.assertEqual(2, len(work.inbound.attachments))


if __name__ == "__main__":
    unittest.main()
