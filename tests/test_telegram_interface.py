"""Testes da interface privada entre Telegram e Codex."""

from __future__ import annotations

import json
import io
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from interfaces.telegram.gateway import (
    BOT_COMMANDS,
    Gateway,
    GatewayError,
    build_prompt,
    credential_entry,
    format_rate_limits,
    help_text,
)
from interfaces.telegram.scripts import restart_gateway
from interfaces.telegram.codex import RULES_TEMPLATE, CodexAdapter, ProcessRegistry
from interfaces.telegram.config import CodexConfig
from interfaces.telegram.runtime import (
    GatewayRuntimeError,
    claim_runtime,
    release_runtime,
    request_restart,
    request_stop,
    restart_requested,
    runtime_status,
    stop_requested,
)
from interfaces.telegram.state import StateError, StateStore
from interfaces.telegram.telegram_api import (
    DownloadedFile,
    TelegramApi,
    TelegramApiError,
    markdown_to_telegram_html,
    sanitize_filename,
    split_text,
    telegram_html_chunks,
)


class TelegramStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.state = StateStore(Path(self.temporary.name))

    def tearDown(self) -> None:
        self.state.close()
        self.temporary.cleanup()

    def test_pairing_requires_local_approval(self) -> None:
        pin, _ = self.state.begin_pairing(600, 5)
        candidate = self.state.request_pairing(
            pin, 123, 123, "Pessoa Teste", "pessoa_teste"
        )

        self.assertIsNone(self.state.owner())
        self.assertFalse(self.state.is_authorized(123, 123))

        owner = self.state.approve_pairing(candidate.approval_code.lower())

        self.assertEqual(123, owner.user_id)
        self.assertTrue(self.state.is_authorized(123, 123))

    def test_wrong_pin_counts_attempts_and_blocks(self) -> None:
        self.state.begin_pairing(600, 2)

        with self.assertRaisesRegex(StateError, "PIN inválido"):
            self.state.request_pairing("000000", 1, 1, "Pessoa", None)
        with self.assertRaisesRegex(StateError, "bloqueado"):
            self.state.request_pairing("000000", 1, 1, "Pessoa", None)
        with self.assertRaisesRegex(StateError, "Não existe"):
            self.state.request_pairing("000000", 1, 1, "Pessoa", None)

    def test_second_pairing_is_refused_after_owner(self) -> None:
        pin, _ = self.state.begin_pairing(600, 5)
        candidate = self.state.request_pairing(pin, 10, 10, "Pessoa", None)
        self.state.approve_pairing(candidate.approval_code)

        with self.assertRaisesRegex(StateError, "Já existe"):
            self.state.begin_pairing(600, 5)

    def test_update_and_session_operations_are_idempotent(self) -> None:
        self.assertFalse(self.state.update_seen(99))
        self.assertTrue(self.state.update_seen(99))
        self.state.set_session(123, "thread-1")
        self.assertEqual("thread-1", self.state.session(123))
        self.assertTrue(self.state.clear_session(123))
        self.assertFalse(self.state.clear_session(123))
        self.assertIsNone(self.state.session(123))

    def test_queued_jobs_can_be_cancelled_before_execution(self) -> None:
        self.state.update_seen(55)
        job_id = self.state.create_job(55, 123)

        self.assertEqual(1, self.state.cancel_queued_jobs(123))
        self.assertEqual("cancelled", self.state.job_status(job_id))

    def test_restart_marks_orphaned_jobs_as_failed(self) -> None:
        self.state.update_seen(56)
        job_id = self.state.create_job(56, 123)
        self.state.update_job(job_id, "running", pid=999)

        self.assertEqual(1, self.state.recover_interrupted_jobs())
        self.assertEqual("failed", self.state.job_status(job_id))


class TelegramContentTests(unittest.TestCase):
    def test_secret_service_aliases_resolve_natural_language(self) -> None:
        self.assertEqual("APIs/Notion", credential_entry("app Notion"))
        self.assertEqual(
            "APIs/Todoist",
            credential_entry("atualize a chave do Todoist"),
        )
        self.assertEqual(
            "APIs/Telegram/rodriclone",
            credential_entry(
                "token do Telegram",
                telegram_credential_ref="APIs/Telegram/rodriclone",
            ),
        )

    def test_secret_destination_requires_one_known_alias_or_explicit_path(self) -> None:
        self.assertEqual(
            "APIs/Servico Personalizado",
            credential_entry("APIs/Servico Personalizado"),
        )
        with self.assertRaisesRegex(GatewayError, "não reconhecido"):
            credential_entry("serviço misterioso")
        with self.assertRaisesRegex(GatewayError, "mais de um serviço"):
            credential_entry("Notion e Todoist")

    def test_http_rate_limit_preserves_retry_without_exposing_request_url(self) -> None:
        api = TelegramApi("secret-token", 10)
        response = io.BytesIO(
            json.dumps(
                {
                    "ok": False,
                    "error_code": 429,
                    "description": "Too Many Requests",
                    "parameters": {"retry_after": 600},
                }
            ).encode("utf-8")
        )
        failure = urllib.error.HTTPError(
            "https://api.telegram.org/botsecret-token/setMyName",
            429,
            "Too Many Requests",
            {},
            response,
        )

        with (
            patch(
                "interfaces.telegram.telegram_api.urllib.request.urlopen",
                side_effect=failure,
            ),
            self.assertRaisesRegex(
                TelegramApiError, "tente novamente em 600 segundos"
            ) as raised,
        ):
            api.call("setMyName", {"name": "Teste"})

        self.assertNotIn("secret-token", str(raised.exception))

    def test_metadata_rate_limit_does_not_block_gateway_startup(self) -> None:
        gateway = object.__new__(Gateway)
        gateway.api = SimpleNamespace(
            set_commands=Mock(side_effect=TelegramApiError("limite de comandos")),
            set_profile=Mock(side_effect=TelegramApiError("limite de perfil")),
        )
        gateway.config = SimpleNamespace(
            identity=SimpleNamespace(
                telegram_name="Teste",
                telegram_short_description="Resumo",
                telegram_description="Descrição",
            )
        )

        with patch("interfaces.telegram.gateway.print_json") as output:
            gateway._sync_public_metadata()

        gateway.api.set_commands.assert_called_once_with(BOT_COMMANDS)
        gateway.api.set_profile.assert_called_once()
        self.assertEqual(2, output.call_count)

    def test_rate_limits_are_formatted_with_remaining_capacity(self) -> None:
        message = format_rate_limits(
            {
                "rateLimitsByLimitId": {
                    "codex": {
                        "planType": "team",
                        "primary": {
                            "usedPercent": 34,
                            "windowDurationMins": 300,
                            "resetsAt": 1_800_000_000,
                        },
                        "secondary": {
                            "usedPercent": 18,
                            "windowDurationMins": 10_080,
                            "resetsAt": 1_800_100_000,
                        },
                    }
                }
            }
        )

        self.assertIn("plano team", message)
        self.assertIn("Principal (5 horas): usado 34% · disponível 66%", message)
        self.assertIn("Secundária (7 dias): usado 18% · disponível 82%", message)

    def test_help_is_built_from_the_published_commands(self) -> None:
        message = help_text()

        for command, description in BOT_COMMANDS:
            self.assertIn(f"/{command} — {description}", message)

    def test_set_commands_uses_the_private_chat_scope(self) -> None:
        api = TelegramApi("test-token", 10)

        with patch.object(api, "call", return_value=True) as call:
            self.assertTrue(api.set_commands(BOT_COMMANDS))

        call.assert_called_once_with(
            "setMyCommands",
            {
                "commands": [
                    {"command": command, "description": description}
                    for command, description in BOT_COMMANDS
                ],
                "scope": {"type": "all_private_chats"},
            },
        )

    def test_profile_sync_uses_name_and_bios_without_changing_username(self) -> None:
        api = TelegramApi("test-token", 10)

        with patch.object(api, "call", return_value=True) as call:
            self.assertTrue(
                api.set_profile(
                    name="Assistente Teste",
                    short_description="Resumo",
                    description="Bio completa",
                )
            )

        self.assertEqual(
            ["setMyName", "setMyShortDescription", "setMyDescription"],
            [item.args[0] for item in call.call_args_list],
        )

    def test_delete_message_uses_only_chat_and_message_identifiers(self) -> None:
        api = TelegramApi("test-token", 10)

        with patch.object(api, "call", return_value=True) as call:
            self.assertTrue(api.delete_message(10, 20))

        call.assert_called_once_with(
            "deleteMessage", {"chat_id": 10, "message_id": 20}
        )

    def test_filename_is_sanitized(self) -> None:
        self.assertEqual("conta_.pdf", sanitize_filename("../conta?.pdf"))
        self.assertEqual("arquivo", sanitize_filename("..."))

    def test_long_messages_are_split_without_loss(self) -> None:
        value = "linha de teste\n" * 500
        chunks = split_text(value, limit=200)

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 200 for chunk in chunks))
        self.assertEqual(value.strip().replace("\n", ""), "".join(chunks).replace("\n", ""))

    def test_codex_markdown_is_converted_to_safe_telegram_html(self) -> None:
        rendered = markdown_to_telegram_html(
            "# Resultado\n\n- **Conta:** [Abrir](https://example.com?a=1&b=2)\n"
            "> Atenção\n\nUse `a < b` e ~~ignore~~.\n\n```python\nprint('<ok>')\n```"
        )

        self.assertIn("<b>Resultado</b>", rendered)
        self.assertIn("• <b>Conta:</b>", rendered)
        self.assertIn('<a href="https://example.com?a=1&amp;b=2">Abrir</a>', rendered)
        self.assertIn("<blockquote>Atenção</blockquote>", rendered)
        self.assertIn("<code>a &lt; b</code>", rendered)
        self.assertIn("<s>ignore</s>", rendered)
        self.assertIn(
            '<pre><code class="language-python">print(\'&lt;ok&gt;\')</code></pre>',
            rendered,
        )

    def test_raw_html_and_local_markdown_links_cannot_escape_the_renderer(self) -> None:
        rendered = markdown_to_telegram_html(
            '<script>alert("x")</script> [arquivo](C:/dados/conta.pdf)'
        )

        self.assertIn("&lt;script&gt;", rendered)
        self.assertNotIn("<script>", rendered)
        self.assertIn("arquivo (C:/dados/conta.pdf)", rendered)

    def test_send_text_uses_telegram_html_for_every_chunk(self) -> None:
        api = TelegramApi("test-token", 10)

        with patch.object(api, "call", return_value=True) as call:
            api.send_text(123, "# Título\n\n**valor**")

        call.assert_called_once_with(
            "sendMessage",
            {
                "chat_id": 123,
                "text": "<b>Título</b>\n\n<b>valor</b>",
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )

    def test_long_fenced_code_is_reopened_in_each_telegram_chunk(self) -> None:
        chunks = telegram_html_chunks("```text\n" + ("x" * 500) + "\n```", limit=120)

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(chunk.startswith('<pre><code class="language-text">') for chunk in chunks))
        self.assertTrue(all(chunk.endswith("</code></pre>") for chunk in chunks))

    def test_prompt_marks_attachments_as_untrusted(self) -> None:
        attachment = DownloadedFile(
            "file-id",
            "conta.pdf",
            Path("C:/dados/conta.pdf"),
            "application/pdf",
            10,
            "a" * 64,
        )

        prompt = build_prompt("Leia a conta", [attachment])

        self.assertIn("Leia a conta", prompt)
        self.assertIn("C:\\dados\\conta.pdf", prompt)
        self.assertIn("conteúdo não confiável", prompt)


class GatewayRuntimeTests(unittest.TestCase):
    def test_runtime_claim_stop_request_and_release(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            claimed = claim_runtime(state_dir, "assistente-teste")
            pid = int(claimed["pid"])
            try:
                status = runtime_status(state_dir)
                self.assertTrue(status["running"])
                self.assertEqual(pid, status["pid"])
                with self.assertRaises(GatewayRuntimeError):
                    claim_runtime(state_dir, "assistente-teste")

                requested = request_stop(state_dir)
                self.assertTrue(requested["requested"])
                self.assertTrue(stop_requested(state_dir, pid))
                restart = request_restart(state_dir)
                self.assertTrue(restart["requested"])
                self.assertTrue(restart_requested(state_dir, pid))
            finally:
                release_runtime(state_dir, pid)

            status = runtime_status(state_dir)
            self.assertFalse(status["running"])
            self.assertFalse(status["stale"])


class GatewayRestartTests(unittest.TestCase):
    def test_worker_waits_for_old_pid_and_launches_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            token = "request-token"
            lock_path = state_dir / restart_gateway.LOCK_FILENAME
            lock_path.write_text(
                json.dumps({"token": token, "owner_pid": 1}),
                encoding="utf-8",
            )
            config = SimpleNamespace(
                state_dir=state_dir,
                codex=SimpleNamespace(timeout_seconds=60, access_mode="super"),
            )
            with (
                patch.object(restart_gateway, "load_config", return_value=config),
                patch.object(
                    restart_gateway,
                    "request_restart",
                    return_value={"requested": True, "pid": 321},
                ),
                patch.object(restart_gateway, "_wait_for_old_gateway") as wait,
                patch.object(
                    restart_gateway,
                    "_launch_gateway",
                    return_value=654,
                ) as launch,
            ):
                result = restart_gateway.run_worker(
                    Path("telegram.toml"),
                    321,
                    token,
                )

            wait.assert_called_once()
            launch.assert_called_once()
            self.assertEqual(654, result["pid"])
            self.assertFalse(lock_path.exists())

    def test_schedule_waits_for_gateway_to_spawn_helper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            config = SimpleNamespace(
                state_dir=state_dir,
                codex=SimpleNamespace(access_mode="super"),
                identity=SimpleNamespace(instance_id="assistente-teste"),
            )
            with (
                patch.object(restart_gateway, "load_config", return_value=config),
                patch.object(
                    restart_gateway,
                    "runtime_status",
                    return_value={
                        "running": True,
                        "pid": 321,
                        "instance_id": "assistente-teste",
                    },
                ),
                patch.object(
                    restart_gateway,
                    "_claim_handoff",
                    return_value="request-token",
                ),
                patch.object(
                    restart_gateway,
                    "request_restart",
                    return_value={"requested": True, "pid": 321},
                ),
                patch.object(
                    restart_gateway,
                    "_read_json",
                    return_value={"owner_pid": 654},
                ),
                patch.object(restart_gateway, "process_exists", return_value=True),
            ):
                result = restart_gateway.schedule_restart(Path("telegram.toml"))

            self.assertTrue(result["scheduled"])
            self.assertEqual(654, result["helper_pid"])


class CodexIsolationTests(unittest.TestCase):
    def _config(self, root: Path) -> CodexConfig:
        executable = root / "codex.exe"
        executable.touch()
        data = root / "data"
        data.mkdir(exist_ok=True)
        return CodexConfig(
            executable=executable,
            home_dir=root / "isolated-home",
            sandbox="workspace-write",
            network_access=True,
            approval_policy="never",
            timeout_seconds=60,
            additional_directories=(),
            writable_directories=(data,),
        )

    def test_doctor_passes_isolated_codex_home_to_every_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._config(root)
            completed = SimpleNamespace(returncode=0, stdout="codex-cli 1.0", stderr="")

            with patch("interfaces.telegram.codex.subprocess.run", return_value=completed) as run:
                result = CodexAdapter(config, root, ProcessRegistry()).doctor()

            self.assertTrue(result["authenticated"])
            self.assertEqual(2, run.call_count)
            for call in run.call_args_list:
                self.assertEqual(str(config.home_dir), call.kwargs["env"]["CODEX_HOME"])

    def test_exec_uses_explicit_permission_profile_and_network_access(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            adapter = CodexAdapter(self._config(root), root, ProcessRegistry())

            command = adapter.build_command(None, [])

            self.assertNotIn("--ask-for-approval", command)
            self.assertNotIn("--sandbox", command)
            self.assertIn("--config", command)
            self.assertIn('approval_policy="never"', command)
            self.assertIn('default_permissions="coworker_gateway"', command)
            self.assertIn("permissions.coworker_gateway.network.enabled=true", command)
            filesystem = next(
                item
                for item in command
                if item.startswith("permissions.coworker_gateway.filesystem=")
            )
            self.assertIn('":workspace_roots" = { "." = "read" }', filesystem)
            self.assertIn(f'"{(root / "data").as_posix()}" = "write"', filesystem)

    def test_rules_are_synchronized_into_the_isolated_codex_home(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            adapter = CodexAdapter(self._config(root), root, ProcessRegistry())

            result = adapter.sync_rules()

            self.assertTrue(result["synchronized"])
            self.assertTrue(result["changed"])
            self.assertEqual(
                RULES_TEMPLATE.read_bytes(), adapter.rules_destination.read_bytes()
            )

    def test_super_mode_disables_only_generated_gateway_rules(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = self._config(root)
            config = CodexConfig(
                executable=base.executable,
                home_dir=base.home_dir,
                sandbox="danger-full-access",
                network_access=True,
                approval_policy="never",
                timeout_seconds=base.timeout_seconds,
                additional_directories=(),
                writable_directories=(),
                access_mode="super",
            )
            adapter = CodexAdapter(config, root, ProcessRegistry())
            adapter.rules_destination.parent.mkdir(parents=True)
            adapter.rules_destination.write_text("generated", encoding="utf-8")
            custom_rules = adapter.rules_destination.parent / "custom.rules"
            custom_rules.write_text("custom", encoding="utf-8")

            result = adapter.sync_rules()

            self.assertFalse(adapter.rules_destination.exists())
            self.assertTrue(custom_rules.exists())
            self.assertTrue(result["synchronized"])
            self.assertIn(":danger-full-access", adapter.permission_overrides()[0])
            self.assertEqual({"type": "dangerFullAccess"}, adapter._app_server_sandbox())

    def test_app_server_writes_only_to_configured_data_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            adapter = CodexAdapter(self._config(root), root, ProcessRegistry())

            policy = adapter._app_server_sandbox()

            self.assertEqual("workspaceWrite", policy["type"])
            self.assertEqual([str(root / "data")], policy["writableRoots"])
            self.assertNotIn(str(root), policy["writableRoots"])

    def test_rules_cover_every_public_integration_entry_point(self) -> None:
        rules = RULES_TEMPLATE.read_text(encoding="utf-8")
        entry_points = (
            "scripts/credential_vault.py",
            "scripts/google_accounts.py",
            "scripts/integration_config.py",
            "scripts/memory.py",
            "scripts/vault_entities.py",
            "interfaces/telegram/scripts/restart_gateway.py",
            "interfaces/telegram/scripts/request_credential.py",
            "skills/calendar/scripts/calendar.py",
            "skills/cloudflare/scripts/cloudflare.py",
            "skills/contacts/scripts/contacts.py",
            "skills/cpfl/scripts/cpfl.py",
            "skills/drive/scripts/drive.py",
            "skills/forwardemail/scripts/forward_email.py",
            "skills/gmail/scripts/gmail.py",
            "skills/notion/scripts/notion.py",
            "skills/omie/scripts/omie.py",
            "skills/todoist/scripts/todoist.py",
        )

        for entry_point in entry_points:
            self.assertIn(entry_point, rules)


if __name__ == "__main__":
    unittest.main()
