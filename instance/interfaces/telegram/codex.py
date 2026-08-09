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


RULES_TEMPLATE = Path(__file__).resolve().parents[2] / "config" / "codex.rules"


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


@dataclass(frozen=True)
class CodexProgress:
    """Atualização segura e destinada à pessoa usuária durante um turno."""

    kind: str
    text: str
    completed: bool = False


@dataclass(frozen=True)
class CodexOptions:
    model: str | None = None
    reasoning_effort: str | None = None
    speed: str = "standard"
    verbosity: str | None = None


@dataclass(frozen=True)
class CodexModel:
    model: str
    display_name: str
    default_reasoning_effort: str | None
    supported_reasoning_efforts: tuple[str, ...]
    is_default: bool = False
    supports_fast: bool = False


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
        self._models_cache: tuple[CodexModel, ...] = ()
        self._models_cached_at = 0.0

    def _environment(self) -> dict[str, str]:
        self.config.home_dir.mkdir(parents=True, exist_ok=True)
        environment = os.environ.copy()
        environment["CODEX_HOME"] = str(self.config.home_dir)
        return environment

    @staticmethod
    def _safe_app_server_error(value: Any) -> str | None:
        """Extrai uma descrição curta do erro sem retransmitir o payload bruto."""
        if isinstance(value, dict):
            parts: list[str] = []
            for key in ("code", "type", "message", "detail"):
                item = value.get(key)
                if isinstance(item, (str, int, float)) and str(item).strip():
                    parts.append(str(item).strip())
            value = ": ".join(parts)
        if not isinstance(value, str):
            return None
        compact = " ".join(value.replace("\r", " ").replace("\n", " ").split())
        if not compact:
            return None
        return compact[:400]

    @property
    def rules_destination(self) -> Path:
        return self.config.home_dir / "rules" / "gateway.rules"

    def rules_status(self) -> dict[str, Any]:
        if self.config.access_mode == "super":
            return {
                "mode": "super",
                "template": str(RULES_TEMPLATE),
                "destination": str(self.rules_destination),
                "synchronized": not self.rules_destination.exists(),
            }
        try:
            synchronized = (
                self.rules_destination.is_file()
                and self.rules_destination.read_bytes() == RULES_TEMPLATE.read_bytes()
            )
        except OSError as exc:
            raise CodexExecutionError("Não foi possível verificar as regras do Codex.") from exc
        return {
            "mode": "restricted",
            "template": str(RULES_TEMPLATE),
            "destination": str(self.rules_destination),
            "synchronized": synchronized,
        }

    def sync_rules(self) -> dict[str, Any]:
        try:
            if self.config.access_mode == "super":
                changed = self.rules_destination.exists()
                if changed:
                    self.rules_destination.unlink()
                return {**self.rules_status(), "changed": changed}
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
                "com o CODEX_HOME configurado para esta instância."
            )
        return {
            "executable": str(self.config.executable),
            "version": completed.stdout.strip() or completed.stderr.strip(),
            "project_root": str(self.project_root),
            "home_dir": str(self.config.home_dir),
            "access_mode": self.config.access_mode,
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
        process, responses = self._start_app_server("coworker-codex-doctor")
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

        reader = threading.Thread(target=read_stdout, name="coworker-codex-account", daemon=True)
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
                            "name": "coworker_telegram",
                            "title": "Coworker Telegram",
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

    def models(self, *, force: bool = False) -> tuple[CodexModel, ...]:
        """Consulta o seletor oficial de modelos usando a conta isolada da instância."""
        if (
            not force
            and self._models_cache
            and time.monotonic() - self._models_cached_at < 300
        ):
            return self._models_cache
        process, responses = self._start_app_server("coworker-codex-models")
        deadline = time.monotonic() + 30
        try:
            self._initialize_app_server(process, responses, deadline)
            self._send_app_server_message(
                process,
                {
                    "method": "model/list",
                    "id": 2,
                    "params": {"limit": 100, "includeHidden": False},
                },
            )
            result = self._wait_app_server_response(responses, 2, deadline)
        finally:
            self._stop_process(process)
        raw_models = result.get("data")
        if not isinstance(raw_models, list):
            raise CodexExecutionError("O Codex devolveu um catálogo de modelos inválido.")
        parsed: list[CodexModel] = []
        for item in raw_models:
            if not isinstance(item, dict):
                continue
            model = str(item.get("model") or item.get("id") or "").strip()
            if not model:
                continue
            efforts: list[str] = []
            for effort in item.get("supportedReasoningEfforts") or []:
                if isinstance(effort, dict) and effort.get("reasoningEffort"):
                    efforts.append(str(effort["reasoningEffort"]).casefold())
            parsed.append(
                CodexModel(
                    model=model,
                    display_name=str(item.get("displayName") or model),
                    default_reasoning_effort=(
                        str(item["defaultReasoningEffort"]).casefold()
                        if item.get("defaultReasoningEffort")
                        else None
                    ),
                    supported_reasoning_efforts=tuple(efforts),
                    is_default=bool(item.get("isDefault")),
                    supports_fast="fast" in {
                        str(tier).casefold()
                        for tier in item.get("additionalSpeedTiers") or []
                    },
                )
            )
        if not parsed:
            raise CodexExecutionError("Nenhum modelo disponível foi informado pelo Codex.")
        self._models_cache = tuple(parsed)
        self._models_cached_at = time.monotonic()
        return self._models_cache

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
                detail = CodexAdapter._safe_app_server_error(message["error"])
                suffix = f": {detail}" if detail else "."
                raise CodexExecutionError(
                    f"O Codex recusou a solicitação ao App Server{suffix}"
                )
            result = message.get("result")
            if not isinstance(result, dict):
                raise CodexExecutionError("O Codex devolveu uma resposta em formato inválido.")
            return result
        raise CodexExecutionError("A solicitação ao Codex excedeu o tempo esperado.")

    def build_command(
        self,
        thread_id: str | None,
        images: list[Path],
        output_schema: Path | None = None,
        options: CodexOptions | None = None,
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
        for override in self.option_overrides(options or CodexOptions()):
            command.extend(["--config", override])
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

    @staticmethod
    def option_overrides(options: CodexOptions) -> tuple[str, ...]:
        """Traduz somente opções de inferência previamente validadas pelo gateway."""
        overrides: list[str] = []
        if options.model:
            model = options.model.replace('"', '\\"')
            overrides.append(f'model="{model}"')
        if options.reasoning_effort:
            overrides.append(f'model_reasoning_effort="{options.reasoning_effort}"')
        if options.verbosity:
            overrides.append(f'model_verbosity="{options.verbosity}"')
        if options.speed == "fast":
            overrides.extend(('features.fast_mode=true', 'service_tier="fast"'))
        else:
            overrides.extend(('features.fast_mode=false', 'service_tier="default"'))
        return tuple(overrides)

    def permission_overrides(self) -> tuple[str, ...]:
        """Traduz a política pública para os perfis atuais de permissão do Codex."""
        if self.config.access_mode == "super":
            return ('default_permissions=":danger-full-access"',)
        network = str(self.config.network_access).lower()
        direct_paths = ""
        for directory in self.config.additional_directories:
            additional = directory.as_posix().replace('"', '\\"')
            direct_paths += f', "{additional}" = "read"'
        if self.config.sandbox == "workspace-write":
            for directory in self.config.writable_directories:
                writable = directory.as_posix().replace('"', '\\"')
                direct_paths += f', "{writable}" = "write"'
        if self.config.generated_images_dir is not None:
            generated = self.config.generated_images_dir.as_posix().replace('"', '\\"')
            direct_paths += f', "{generated}" = "read"'
        return (
            "project_root_markers=[]",
            'default_permissions="coworker_gateway"',
            "permissions.coworker_gateway.filesystem="
            f'{{ ":minimal" = "read", ":workspace_roots" = {{ "." = "read" }}{direct_paths} }}',
            f"permissions.coworker_gateway.network.enabled={network}",
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
        options: CodexOptions | None = None,
        on_progress: Callable[[CodexProgress], None] | None = None,
    ) -> CodexResult:
        effective_options = options or CodexOptions()
        if self.config.backend == "app-server":
            return self._run_app_server(
                chat_id, prompt, thread_id, images, on_started, output_schema, job_output,
                effective_options, on_progress,
            )
        return self._run_exec(
            chat_id, prompt, thread_id, images, on_started, output_schema, job_output,
            effective_options, on_progress,
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
        options: CodexOptions,
        on_progress: Callable[[CodexProgress], None] | None,
    ) -> CodexResult:
        command = self.build_command(thread_id, images, output_schema, options)
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
                env=self._job_environment(job_output, chat_id),
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
        hard_deadline = deadline + 1800
        finished_streams: set[str] = set()
        final_messages: list[str] = []
        discovered_thread = thread_id
        malformed_events = 0
        try:
            while len(finished_streams) < 2 or process.poll() is None:
                deadline = self._extend_for_credential_capture(
                    deadline, hard_deadline, job_output
                )
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
                if event.get("type") == "item.started" and isinstance(item, dict):
                    self._emit_item_progress(item, on_progress)
                if (
                    event.get("type") == "item.completed"
                    and isinstance(item, dict)
                    and item.get("type") == "agent_message"
                    and item.get("text")
                ):
                    phase = str(item.get("phase") or "")
                    if phase == "commentary":
                        self._emit_progress(
                            on_progress,
                            CodexProgress("commentary", str(item["text"]), True),
                        )
                    elif phase != "reasoning":
                        final_messages.append(str(item["text"]))
                if event.get("type") == "turn.plan.updated":
                    self._emit_progress(
                        on_progress,
                        CodexProgress("milestone", "Atualizando o plano de trabalho."),
                    )
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

    def _job_environment(
        self,
        job_output: Path | None,
        chat_id: int | None = None,
    ) -> dict[str, str]:
        environment = self._environment()
        if job_output is not None:
            environment["COWORKER_JOB_INPUT"] = str(job_output.parent / "input")
            environment["COWORKER_JOB_OUTPUT"] = str(job_output)
            environment["COWORKER_JOB_DERIVED"] = str(job_output.parent / "derived")
        if chat_id is not None:
            environment["COWORKER_CHAT_ID"] = str(chat_id)
        return environment

    @staticmethod
    def _extend_for_credential_capture(
        deadline: float,
        hard_deadline: float,
        job_output: Path | None,
    ) -> float:
        if job_output is None:
            return deadline
        request_path = job_output.parent / "credential-request.json"
        if not request_path.is_file():
            return deadline
        return min(hard_deadline, max(deadline, time.monotonic() + 5))

    def _start_app_server(
        self,
        reader_name: str,
        job_output: Path | None = None,
        chat_id: int | None = None,
        options: CodexOptions | None = None,
    ) -> tuple[subprocess.Popen[str], queue.Queue[str | None]]:
        command = [str(self.config.executable), "app-server", "--listen", "stdio://"]
        for override in self.permission_overrides():
            command.extend(["--config", override])
        command.extend(["--config", f'approval_policy="{self.config.approval_policy}"'])
        if options is not None:
            for override in self.option_overrides(options):
                command.extend(["--config", override])
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
                env=self._job_environment(job_output, chat_id),
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
                        "name": "coworker_telegram",
                        "title": "Coworker Telegram",
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
        options: CodexOptions,
        on_progress: Callable[[CodexProgress], None] | None,
    ) -> CodexResult:
        process, responses = self._start_app_server(
            "coworker-codex-turn", job_output, chat_id, options
        )
        self.registry.register(chat_id, process)
        deadline = time.monotonic() + self.config.timeout_seconds
        hard_deadline = deadline + 1800
        discovered_thread = thread_id
        turn_id: str | None = None
        final_messages: list[str] = []
        agent_phases: dict[str, str] = {}
        agent_buffers: dict[str, str] = {}
        status = "failed"
        turn_error: str | None = None
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
            if options.model:
                turn_params["model"] = options.model
            if options.reasoning_effort:
                turn_params["effort"] = options.reasoning_effort
            turn_params["serviceTier"] = (
                "fast" if options.speed == "fast" else "default"
            )
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
            while True:
                deadline = self._extend_for_credential_capture(
                    deadline, hard_deadline, job_output
                )
                if time.monotonic() >= deadline:
                    try:
                        self._send_app_server_message(
                            process,
                            {
                                "method": "turn/interrupt",
                                "id": 100,
                                "params": {
                                    "threadId": discovered_thread,
                                    "turnId": turn_id,
                                },
                            },
                        )
                    finally:
                        raise CodexExecutionError(
                            "A execução do Codex excedeu o tempo configurado."
                        )
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
                if method_name == "item/started" and isinstance(params_value, dict):
                    item = params_value.get("item")
                    if isinstance(item, dict):
                        if item.get("type") == "agentMessage" and item.get("id"):
                            item_id = str(item["id"])
                            agent_phases[item_id] = str(item.get("phase") or "")
                            agent_buffers[item_id] = str(item.get("text") or "")
                        else:
                            self._emit_item_progress(item, on_progress)
                if method_name == "item/agentMessage/delta" and isinstance(params_value, dict):
                    item_id = str(params_value.get("itemId") or "")
                    delta = params_value.get("delta")
                    if item_id and isinstance(delta, str):
                        agent_buffers[item_id] = agent_buffers.get(item_id, "") + delta
                        if agent_phases.get(item_id) == "commentary":
                            self._emit_progress(
                                on_progress,
                                CodexProgress("commentary", agent_buffers[item_id]),
                            )
                if method_name == "item/completed" and isinstance(params_value, dict):
                    item = params_value.get("item")
                    if isinstance(item, dict) and item.get("type") == "agentMessage" and item.get("text"):
                        item_id = str(item.get("id") or "")
                        phase = str(item.get("phase") or agent_phases.get(item_id) or "")
                        text = str(item["text"])
                        if phase == "commentary":
                            self._emit_progress(
                                on_progress, CodexProgress("commentary", text, True)
                            )
                        elif phase != "reasoning":
                            final_messages.append(text)
                if method_name == "turn/plan/updated":
                    self._emit_progress(
                        on_progress,
                        CodexProgress("milestone", "Atualizando o plano de trabalho."),
                    )
                if method_name == "turn/completed" and isinstance(params_value, dict):
                    completed = params_value.get("turn")
                    if isinstance(completed, dict):
                        status = str(completed.get("status", "failed"))
                        turn_error = self._safe_app_server_error(completed.get("error"))
                    else:
                        status = "failed"
                    break
        finally:
            self.registry.unregister(chat_id, process)
            self._stop_process(process)
        if status == "interrupted" or self.registry.consume_cancelled(chat_id):
            raise CodexCancelledError("A execução foi cancelada pelo usuário.")
        if status != "completed" or not final_messages:
            if turn_error:
                raise CodexExecutionError(
                    f"O App Server concluiu o turno com status '{status}': {turn_error}"
                )
            if status != "completed":
                raise CodexExecutionError(
                    f"O App Server concluiu o turno com status '{status}', sem resposta final."
                )
            raise CodexExecutionError("O App Server não concluiu com uma resposta final.")
        return CodexResult(discovered_thread, final_messages[-1], turn_id, status)

    @staticmethod
    def _emit_progress(
        callback: Callable[[CodexProgress], None] | None,
        progress: CodexProgress,
    ) -> None:
        if callback is None:
            return
        try:
            callback(progress)
        except Exception:
            # A telemetria visual nunca deve interromper o trabalho principal.
            return

    def _emit_item_progress(
        self,
        item: dict[str, Any],
        callback: Callable[[CodexProgress], None] | None,
    ) -> None:
        item_type = str(item.get("type") or "")
        descriptions = {
            "commandExecution": "Executando uma ferramenta local.",
            "command_execution": "Executando uma ferramenta local.",
            "fileChange": "Preparando alterações em arquivos.",
            "file_change": "Preparando alterações em arquivos.",
            "mcpToolCall": "Consultando uma integração.",
            "mcp_tool_call": "Consultando uma integração.",
            "dynamicToolCall": "Executando uma capacidade especializada.",
            "dynamic_tool_call": "Executando uma capacidade especializada.",
            "webSearch": "Pesquisando informações.",
            "web_search": "Pesquisando informações.",
            "imageView": "Analisando uma imagem.",
            "image_view": "Analisando uma imagem.",
            "contextCompaction": "Organizando o contexto da conversa.",
            "context_compaction": "Organizando o contexto da conversa.",
            "collabToolCall": "Coordenando uma etapa auxiliar.",
            "collab_tool_call": "Coordenando uma etapa auxiliar.",
        }
        description = descriptions.get(item_type)
        if description:
            self._emit_progress(callback, CodexProgress("milestone", description))

    def _app_server_sandbox(self) -> dict[str, Any]:
        if self.config.access_mode == "super":
            return {"type": "dangerFullAccess"}
        if self.config.sandbox == "read-only":
            return {"type": "readOnly", "networkAccess": self.config.network_access}
        return {
            "type": "workspaceWrite",
            "writableRoots": [
                *(str(path) for path in self.config.writable_directories),
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
