"""Testes dos contratos estruturados e da entrega segura pelo Telegram."""

from __future__ import annotations

import hashlib
import json
import queue
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from interfaces.telegram.codex import CodexAdapter, CodexCancelledError, ProcessRegistry
from interfaces.telegram.gateway import Gateway
from interfaces.telegram.config import (
    CodexConfig,
    MediaConfig,
    PairingConfig,
    ProcessorConfig,
    TelegramConfig,
    WebhookConfig,
    load_config,
)
from interfaces.telegram.contracts import Attachment, InboundMessage, ReplyContext, TelegramReceipt
from interfaces.telegram.identity import InstanceIdentity
from interfaces.telegram.processors import ProcessorError, ProcessorRegistry
from interfaces.telegram.state import StateStore
from interfaces.telegram.telegram_api import TelegramApi
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
    def test_database_created_with_001_is_upgraded_by_002(self) -> None:
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
                    for row in state.connection.execute("PRAGMA table_info(messages)")
                }
            finally:
                state.close()

        self.assertIn("001_initial.sql", versions)
        self.assertIn("002_structured_delivery.sql", versions)
        self.assertIn("reply_to_message_id", columns)
        self.assertIn("turn_id", columns)

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
                f'''transport = "polling"\ncredential_ref = "entry"\nproject_root = "{normalized}"\nstate_dir = "{normalized}/state"\n'''
                "poll_timeout_seconds = 10\nrequest_timeout_seconds = 10\n"
                "[pairing]\nttl_seconds = 600\nmax_attempts = 5\n"
                "[codex]\nexecutable = \"\"\nhome_dir = \"\"\nsandbox = \"workspace-write\"\nnetwork_access = false\napproval_policy = \"never\"\ntimeout_seconds = 60\nadditional_directories = []\n"
                f'''[media]\ninbox_dir = "{normalized}/inbox"\nmax_download_bytes = 1000\n'''
                "[webhook]\npublic_url = \"\"\nsecret_credential_ref = \"\"\nlisten_host = \"127.0.0.1\"\nlisten_port = 8787\n",
                encoding="utf-8",
            )
            before = config_path.read_bytes()
            config = load_config(config_path, require_codex=False)
            after = config_path.read_bytes()

        self.assertEqual("exec", config.codex.backend)
        self.assertEqual(
            Path(__file__).resolve().parents[1] / "data" / "telegram" / "jobs",
            config.media.jobs_dir,
        )
        self.assertEqual(20 * 1024 * 1024, config.media.max_upload_bytes)
        self.assertEqual(before, after)


class WorkspaceTests(unittest.TestCase):
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
            script = Path(__file__).resolve().parents[1] / "interfaces" / "telegram" / "scripts" / "publish_artifact.py"
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
                result = adapter.run(10, "pedido", None, [image], output_schema=schema)

        turn_start = next(
            call.args[1]
            for call in send.call_args_list
            if call.args[1].get("method") == "turn/start"
        )
        self.assertIn({"type": "localImage", "path": str(image)}, turn_start["params"]["input"])
        self.assertEqual({"type": "object"}, turn_start["params"]["outputSchema"])
        self.assertEqual("thread-1", result.thread_id)
        self.assertEqual("turn-1", result.turn_id)

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


class _FakeTelegramApi:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, int | None]] = []
        self.deleted: list[tuple[int, int]] = []

    def send_text(self, chat_id: int, text: str, *, reply_to_message_id: int | None = None):
        self.sent.append((chat_id, text, reply_to_message_id))
        return [TelegramReceipt(100 + len(self.sent))]

    def close(self) -> None:
        pass

    def delete_message(self, chat_id: int, message_id: int) -> bool:
        self.deleted.append((chat_id, message_id))
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
