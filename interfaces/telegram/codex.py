"""Adaptador de processo para execuções não interativas do Codex CLI."""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from interfaces.telegram.config import CodexConfig


RULES_TEMPLATE = Path(__file__).resolve().parents[2] / "config" / "codex-botina.rules"


class CodexExecutionError(RuntimeError):
    """Representa uma execução malsucedida sem expor saída potencialmente sensível."""


class CodexCancelledError(CodexExecutionError):
    """Indica cancelamento solicitado explicitamente pela conversa autorizada."""


@dataclass(frozen=True)
class CodexResult:
    thread_id: str | None
    final_message: str
    turn_id: str | None = None
    status: str = "completed"


class ProcessRegistry:
    """Permite cancelar o processo ativo enquanto o polling continua responsivo."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._processes: dict[int, subprocess.Popen[str]] = {}
        self._interrupts: dict[int, Callable[[], None]] = {}
        self._cancelled: set[int] = set()

    def register(self, chat_id: int, process: subprocess.Popen[str]) -> None:
        with self._lock:
            self._cancelled.discard(chat_id)
            self._processes[chat_id] = process

    def unregister(self, chat_id: int, process: subprocess.Popen[str]) -> None:
        with self._lock:
            if self._processes.get(chat_id) is process:
                self._processes.pop(chat_id, None)
                self._interrupts.pop(chat_id, None)

    def register_interrupt(self, chat_id: int, callback: Callable[[], None]) -> None:
        with self._lock:
            self._interrupts[chat_id] = callback

    def cancel(self, chat_id: int) -> bool:
        with self._lock:
            process = self._processes.get(chat_id)
            interrupt = self._interrupts.get(chat_id)
        if (not process or process.poll() is not None) and interrupt is None:
            return False
        with self._lock:
            self._cancelled.add(chat_id)
        if interrupt is not None:
            try:
                interrupt()
            except OSError:
                pass
        elif process is not None:
            process.terminate()
        return True

    def cancel_all(self) -> int:
        with self._lock:
            chat_ids = list(self._processes)
        return sum(1 for chat_id in chat_ids if self.cancel(chat_id))

    def consume_cancelled(self, chat_id: int) -> bool:
        with self._lock:
            if chat_id not in self._cancelled:
                return False
            self._cancelled.remove(chat_id)
            return True


class CodexAdapter:
    def __init__(self, config: CodexConfig, project_root: Path, registry: ProcessRegistry):
        self.config = config
        self.project_root = project_root
        self.registry = registry

    def _environment(self) -> dict[str, str]:
        self.config.home_dir.mkdir(parents=True, exist_ok=True)
        environment = os.environ.copy()
        environment["CODEX_HOME"] = str(self.config.home_dir)
        return environment

    @property
    def rules_destination(self) -> Path:
        return self.config.home_dir / "rules" / "botina.rules"

    def rules_status(self) -> dict[str, Any]:
        try:
            synchronized = (
                self.rules_destination.is_file()
                and self.rules_destination.read_bytes() == RULES_TEMPLATE.read_bytes()
            )
        except OSError as exc:
            raise CodexExecutionError("Não foi possível verificar as regras do Codex.") from exc
        return {
            "template": str(RULES_TEMPLATE),
            "destination": str(self.rules_destination),
            "synchronized": synchronized,
        }

    def sync_rules(self) -> dict[str, Any]:
        try:
            if not RULES_TEMPLATE.is_file():
                raise CodexExecutionError(
                    f"Modelo de regras do Codex ausente em '{RULES_TEMPLATE}'."
                )
            self.rules_destination.parent.mkdir(parents=True, exist_ok=True)
            changed = (
                not self.rules_destination.is_file()
                or self.rules_destination.read_bytes() != RULES_TEMPLATE.read_bytes()
            )
            if changed:
                shutil.copyfile(RULES_TEMPLATE, self.rules_destination)
        except CodexExecutionError:
            raise
        except OSError as exc:
            raise CodexExecutionError("Não foi possível sincronizar as regras do Codex.") from exc
        return {**self.rules_status(), "changed": changed}

    def doctor(self) -> dict[str, Any]:
        environment = self._environment()
        completed = subprocess.run(
            [str(self.config.executable), "--version"],
            cwd=self.project_root,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            shell=False,
            env=environment,
        )
        if completed.returncode != 0:
            raise CodexExecutionError("O executável configurado do Codex não pôde ser iniciado.")
        login = subprocess.run(
            [str(self.config.executable), "login", "status"],
            cwd=self.project_root,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            shell=False,
            env=environment,
        )
        if login.returncode != 0:
            raise CodexExecutionError(
                "O Codex CLI isolado ainda não está autenticado. Execute 'codex login' "
                "com o CODEX_HOME configurado para a BOTina."
            )
        return {
            "executable": str(self.config.executable),
            "version": completed.stdout.strip() or completed.stderr.strip(),
            "project_root": str(self.project_root),
            "home_dir": str(self.config.home_dir),
            "sandbox": self.config.sandbox,
            "network_access": self.config.network_access,
            "approval_policy": self.config.approval_policy,
            "backend": self.config.backend,
            "authenticated": True,
            "exec_rules": self.rules_status(),
            "app_server": self._app_server_diagnostic(),
        }

    def _app_server_diagnostic(self) -> str:
        try:
            return self._app_server_probe()
        except CodexExecutionError:
            return "unavailable"

    def _app_server_probe(self) -> str:
        process, responses = self._start_app_server("botina-codex-doctor")
        try:
            self._initialize_app_server(process, responses, time.monotonic() + 15)
            return "initialized"
        finally:
            self._stop_process(process)

    def rate_limits(self) -> dict[str, Any]:
        """Consulta limites da conta autenticada pela API estável do App Server."""
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            process = subprocess.Popen(
                [str(self.config.executable), "app-server", "--listen", "stdio://"],
                cwd=self.project_root,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                creationflags=flags,
                env=self._environment(),
            )
        except OSError as exc:
            raise CodexExecutionError("Não foi possível iniciar o Codex App Server.") from exc
        assert process.stdin and process.stdout
        responses: queue.Queue[str | None] = queue.Queue()

        def read_stdout() -> None:
            try:
                for line in process.stdout:
                    responses.put(line)
            finally:
                responses.put(None)

        reader = threading.Thread(target=read_stdout, name="botina-codex-account", daemon=True)
        reader.start()
        deadline = time.monotonic() + 30
        try:
            self._send_app_server_message(
                process,
                {
                    "method": "initialize",
                    "id": 1,
                    "params": {
                        "clientInfo": {
                            "name": "botina_telegram",
                            "title": "BOTina Telegram",
                            "version": "0.1.0",
                        }
                    },
                },
            )
            self._wait_app_server_response(responses, 1, deadline)
            self._send_app_server_message(process, {"method": "initialized", "params": {}})
            self._send_app_server_message(
                process, {"method": "account/rateLimits/read", "id": 2}
            )
            return self._wait_app_server_response(responses, 2, deadline)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()

    @staticmethod
    def _send_app_server_message(
        process: subprocess.Popen[str], message: dict[str, Any]
    ) -> None:
        assert process.stdin
        try:
            process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            process.stdin.flush()
        except OSError as exc:
            raise CodexExecutionError("O Codex App Server encerrou prematuramente.") from exc

    @staticmethod
    def _wait_app_server_response(
        responses: queue.Queue[str | None], request_id: int, deadline: float
    ) -> dict[str, Any]:
        while time.monotonic() < deadline:
            remaining = max(0.01, min(0.25, deadline - time.monotonic()))
            try:
                line = responses.get(timeout=remaining)
            except queue.Empty:
                continue
            if line is None:
                raise CodexExecutionError("O Codex App Server encerrou sem responder.")
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") != request_id:
                continue
            if isinstance(message.get("error"), dict):
                raise CodexExecutionError("O Codex recusou a consulta dos limites da conta.")
            result = message.get("result")
            if not isinstance(result, dict):
                raise CodexExecutionError("O Codex devolveu limites em formato inválido.")
            return result
        raise CodexExecutionError("A consulta dos limites do Codex excedeu o tempo esperado.")

    def build_command(
        self,
        thread_id: str | None,
        images: list[Path],
        output_schema: Path | None = None,
    ) -> list[str]:
        """Monta argumentos compatíveis com o modo não interativo do CLI."""
        command = [
            str(self.config.executable),
            "exec",
            "--json",
            "--cd",
            str(self.project_root),
        ]
        for override in self.permission_overrides():
            command.extend(["--config", override])
        command.extend(
            ["--config", f'approval_policy="{self.config.approval_policy}"']
        )
        for directory in self.config.additional_directories:
            command.extend(["--add-dir", str(directory)])
        if output_schema is not None:
            command.extend(["--output-schema", str(output_schema)])
        if thread_id:
            command.extend(["resume", thread_id])
            for image in images:
                command.extend(["--image", str(image)])
            command.append("-")
        else:
            for image in images:
                command.extend(["--image", str(image)])
            command.append("-")
        return command

    def permission_overrides(self) -> tuple[str, ...]:
        """Traduz a política pública para os perfis atuais de permissão do Codex."""
        if self.config.sandbox == "danger-full-access":
            return ('default_permissions=":danger-full-access"',)
        workspace_access = "write" if self.config.sandbox == "workspace-write" else "read"
        network = str(self.config.network_access).lower()
        direct_paths = ""
        for directory in self.config.additional_directories:
            additional = directory.as_posix().replace('"', '\\"')
            direct_paths += f', "{additional}" = "{workspace_access}"'
        if self.config.generated_images_dir is not None:
            generated = self.config.generated_images_dir.as_posix().replace('"', '\\"')
            direct_paths += f', "{generated}" = "read"'
        return (
            'default_permissions="botina_gateway"',
            "permissions.botina_gateway.filesystem="
            f'{{ ":minimal" = "read", ":workspace_roots" = {{ "." = "{workspace_access}" }}{direct_paths} }}',
            f"permissions.botina_gateway.network.enabled={network}",
        )

    def run(
        self,
        chat_id: int,
        prompt: str,
        thread_id: str | None,
        images: list[Path],
        on_started: Callable[[int], None] | None = None,
        output_schema: Path | None = None,
        job_output: Path | None = None,
    ) -> CodexResult:
        if self.config.backend == "app-server":
            return self._run_app_server(
                chat_id, prompt, thread_id, images, on_started, output_schema, job_output
            )
        return self._run_exec(
            chat_id, prompt, thread_id, images, on_started, output_schema, job_output
        )

    def _run_exec(
        self,
        chat_id: int,
        prompt: str,
        thread_id: str | None,
        images: list[Path],
        on_started: Callable[[int], None] | None,
        output_schema: Path | None,
        job_output: Path | None,
    ) -> CodexResult:
        command = self.build_command(thread_id, images, output_schema)
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            process = subprocess.Popen(
                command,
                cwd=self.project_root,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                creationflags=flags,
                env=self._job_environment(job_output),
            )
        except OSError as exc:
            raise CodexExecutionError("Não foi possível iniciar o Codex CLI configurado.") from exc
        self.registry.register(chat_id, process)
        try:
            if on_started:
                on_started(process.pid)
        except Exception:
            self.registry.unregister(chat_id, process)
            process.terminate()
            raise
        assert process.stdin and process.stdout and process.stderr
        try:
            process.stdin.write(prompt)
            process.stdin.close()
        except OSError as exc:
            self.registry.unregister(chat_id, process)
            process.terminate()
            raise CodexExecutionError("O Codex encerrou antes de receber a solicitação.") from exc
        events: queue.Queue[tuple[str, str | None]] = queue.Queue()

        def read_stream(name: str, stream: Any) -> None:
            try:
                for line in stream:
                    events.put((name, line))
            finally:
                events.put((name, None))

        stdout_thread = threading.Thread(target=read_stream, args=("stdout", process.stdout), daemon=True)
        stderr_thread = threading.Thread(target=read_stream, args=("stderr", process.stderr), daemon=True)
        stdout_thread.start()
        stderr_thread.start()
        deadline = time.monotonic() + self.config.timeout_seconds
        finished_streams: set[str] = set()
        final_messages: list[str] = []
        discovered_thread = thread_id
        malformed_events = 0
        try:
            while len(finished_streams) < 2 or process.poll() is None:
                if time.monotonic() >= deadline:
                    process.terminate()
                    raise CodexExecutionError("A execução do Codex excedeu o tempo configurado.")
                try:
                    source, line = events.get(timeout=0.25)
                except queue.Empty:
                    continue
                if line is None:
                    finished_streams.add(source)
                    continue
                if source != "stdout":
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    malformed_events += 1
                    continue
                if event.get("type") == "thread.started" and event.get("thread_id"):
                    discovered_thread = str(event["thread_id"])
                item = event.get("item")
                if (
                    event.get("type") == "item.completed"
                    and isinstance(item, dict)
                    and item.get("type") == "agent_message"
                    and item.get("text")
                ):
                    final_messages.append(str(item["text"]))
            return_code = process.wait(timeout=5)
        finally:
            self.registry.unregister(chat_id, process)
            if process.poll() is None:
                process.kill()
        if return_code != 0:
            if self.registry.consume_cancelled(chat_id):
                raise CodexCancelledError("A execução foi cancelada pelo usuário.")
            if return_code in {-15, 1} and not final_messages:
                raise CodexExecutionError("A execução do Codex foi interrompida ou recusada.")
            raise CodexExecutionError(f"O Codex encerrou com código {return_code}.")
        if malformed_events and not final_messages:
            raise CodexExecutionError("O Codex devolveu uma sequência JSONL inválida.")
        if not final_messages:
            raise CodexExecutionError("O Codex concluiu sem devolver uma resposta final.")
        return CodexResult(discovered_thread, final_messages[-1])

    def _job_environment(self, job_output: Path | None) -> dict[str, str]:
        environment = self._environment()
        if job_output is not None:
            environment["BOTINA_JOB_OUTPUT"] = str(job_output)
        return environment

    def _start_app_server(
        self, reader_name: str, job_output: Path | None = None
    ) -> tuple[subprocess.Popen[str], queue.Queue[str | None]]:
        command = [str(self.config.executable), "app-server", "--listen", "stdio://"]
        for override in self.permission_overrides():
            command.extend(["--config", override])
        command.extend(["--config", f'approval_policy="{self.config.approval_policy}"'])
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            process = subprocess.Popen(
                command,
                cwd=self.project_root,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                creationflags=flags,
                env=self._job_environment(job_output),
            )
        except OSError as exc:
            raise CodexExecutionError("Não foi possível iniciar o Codex App Server.") from exc
        assert process.stdout
        responses: queue.Queue[str | None] = queue.Queue()

        def read_stdout() -> None:
            try:
                for line in process.stdout:
                    responses.put(line)
            finally:
                responses.put(None)

        threading.Thread(target=read_stdout, name=reader_name, daemon=True).start()
        return process, responses

    def _initialize_app_server(
        self,
        process: subprocess.Popen[str],
        responses: queue.Queue[str | None],
        deadline: float,
    ) -> None:
        self._send_app_server_message(
            process,
            {
                "method": "initialize",
                "id": 1,
                "params": {
                    "clientInfo": {
                        "name": "botina_telegram",
                        "title": "BOTina Telegram",
                        "version": "0.2.0",
                    }
                },
            },
        )
        self._wait_app_server_response(responses, 1, deadline)
        self._send_app_server_message(process, {"method": "initialized", "params": {}})

    def _run_app_server(
        self,
        chat_id: int,
        prompt: str,
        thread_id: str | None,
        images: list[Path],
        on_started: Callable[[int], None] | None,
        output_schema: Path | None,
        job_output: Path | None,
    ) -> CodexResult:
        process, responses = self._start_app_server("botina-codex-turn", job_output)
        self.registry.register(chat_id, process)
        deadline = time.monotonic() + self.config.timeout_seconds
        discovered_thread = thread_id
        turn_id: str | None = None
        final_messages: list[str] = []
        status = "failed"
        try:
            if on_started:
                on_started(process.pid)
            self._initialize_app_server(process, responses, deadline)
            if thread_id:
                method = "thread/resume"
                params: dict[str, Any] = {"threadId": thread_id}
            else:
                method = "thread/start"
                params = {
                    "cwd": str(self.project_root),
                    "approvalPolicy": self.config.approval_policy,
                    "sandbox": self.config.sandbox,
                }
            self._send_app_server_message(process, {"method": method, "id": 2, "params": params})
            thread_result = self._wait_app_server_response(responses, 2, deadline)
            thread = thread_result.get("thread")
            if not isinstance(thread, dict) or not thread.get("id"):
                raise CodexExecutionError("O App Server não devolveu uma thread válida.")
            discovered_thread = str(thread["id"])
            inputs: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
            inputs.extend({"type": "localImage", "path": str(path)} for path in images)
            turn_params: dict[str, Any] = {
                "threadId": discovered_thread,
                "input": inputs,
                "cwd": str(self.project_root),
                "approvalPolicy": self.config.approval_policy,
                "sandboxPolicy": self._app_server_sandbox(),
            }
            if output_schema is not None:
                turn_params["outputSchema"] = json.loads(output_schema.read_text(encoding="utf-8"))
            self._send_app_server_message(
                process, {"method": "turn/start", "id": 3, "params": turn_params}
            )
            started = self._wait_app_server_response(responses, 3, deadline)
            turn = started.get("turn")
            if not isinstance(turn, dict) or not turn.get("id"):
                raise CodexExecutionError("O App Server não devolveu um turno válido.")
            turn_id = str(turn["id"])
            self.registry.register_interrupt(
                chat_id,
                lambda: self._send_app_server_message(
                    process,
                    {
                        "method": "turn/interrupt",
                        "id": 99,
                        "params": {"threadId": discovered_thread, "turnId": turn_id},
                    },
                ),
            )
            while time.monotonic() < deadline:
                try:
                    line = responses.get(timeout=0.25)
                except queue.Empty:
                    continue
                if line is None:
                    raise CodexExecutionError("O App Server encerrou durante o turno.")
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                method_name = message.get("method")
                params_value = message.get("params")
                if method_name == "item/completed" and isinstance(params_value, dict):
                    item = params_value.get("item")
                    if isinstance(item, dict) and item.get("type") == "agentMessage" and item.get("text"):
                        final_messages.append(str(item["text"]))
                if method_name == "turn/completed" and isinstance(params_value, dict):
                    completed = params_value.get("turn")
                    status = str(completed.get("status", "failed")) if isinstance(completed, dict) else "failed"
                    break
            else:
                try:
                    self._send_app_server_message(
                        process,
                        {"method": "turn/interrupt", "id": 100, "params": {"threadId": discovered_thread, "turnId": turn_id}},
                    )
                finally:
                    raise CodexExecutionError("A execução do Codex excedeu o tempo configurado.")
        finally:
            self.registry.unregister(chat_id, process)
            self._stop_process(process)
        if status == "interrupted" or self.registry.consume_cancelled(chat_id):
            raise CodexCancelledError("A execução foi cancelada pelo usuário.")
        if status != "completed" or not final_messages:
            raise CodexExecutionError("O App Server não concluiu com uma resposta final.")
        return CodexResult(discovered_thread, final_messages[-1], turn_id, status)

    def _app_server_sandbox(self) -> dict[str, Any]:
        if self.config.sandbox == "danger-full-access":
            return {"type": "dangerFullAccess"}
        if self.config.sandbox == "read-only":
            return {"type": "readOnly", "networkAccess": self.config.network_access}
        return {
            "type": "workspaceWrite",
            "writableRoots": [
                str(self.project_root),
                *(str(path) for path in self.config.additional_directories),
            ],
            "networkAccess": self.config.network_access,
        }

    @staticmethod
    def _stop_process(process: subprocess.Popen[str]) -> None:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
