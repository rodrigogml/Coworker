"""Cliente local e seguro para transcrição de áudio pelo EccoVox."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from interfaces.telegram.config import TranscriptionConfig


class TranscriptionError(RuntimeError):
    """Indica falha segura ao obter uma transcrição local."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    raw_text: str | None
    confidence: float | None
    language: str | None
    duration_millis: int | None
    model: str | None
    normalization_change_count: int


class EccoVoxClient:
    """Acessa o EccoVox por CLI isolado ou HTTP exclusivamente local."""

    def __init__(self, config: TranscriptionConfig):
        self.config = config
        self._http = urllib.request.build_opener(_NoRedirectHandler())
        self._process: subprocess.Popen[str] | None = None

    def start(self) -> bool:
        """Inicia somente o servidor HTTP local configurado e ainda indisponível."""

        if not self.config.enabled:
            return False
        if self.config.backend != "http" or not self.config.auto_start:
            return bool(self.doctor().get("available"))
        if self.doctor().get("available"):
            return True
        executable = self.config.python_executable
        project_dir = self.config.project_dir
        if executable is None or project_dir is None:
            return False
        endpoint = urllib.parse.urlparse(self.config.endpoint)
        port = endpoint.port or 80
        command = [
            str(executable), "-m", "eccovox.cli", "serve",
            "--host", endpoint.hostname or "127.0.0.1", "--port", str(port),
        ]
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self._process = subprocess.Popen(
                command,
                cwd=project_dir,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                shell=False,
                creationflags=creation_flags,
            )
        except OSError:
            self._process = None
            return False
        deadline = time.monotonic() + min(self.config.timeout_seconds, 30)
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                break
            if self.doctor().get("available"):
                return True
            time.sleep(0.25)
        self.close()
        return False

    def close(self) -> None:
        """Encerra apenas o servidor iniciado por esta instância do cliente."""

        process = self._process
        self._process = None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def doctor(self) -> dict[str, str | bool]:
        if not self.config.enabled:
            return {"available": False, "provider": "eccovox", "reason": "disabled"}
        if self.config.backend == "cli":
            available = bool(
                self.config.python_executable
                and self.config.python_executable.is_file()
                and self.config.project_dir
                and self.config.project_dir.is_dir()
            )
            return {"available": available, "provider": "eccovox-cli"}
        try:
            with self._http.open(f"{self.config.endpoint}/v1/health", timeout=2) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, dict):
                return {"available": False, "provider": "eccovox-http"}
            stt = payload.get("capabilities", {}).get("stt", {})
            return {
                "available": response.status == 200 and stt.get("status") == "ready",
                "provider": "eccovox-http",
            }
        except (OSError, ValueError, urllib.error.URLError):
            return {"available": False, "provider": "eccovox-http"}

    def transcribe(self, path: Path) -> TranscriptionResult:
        if not self.config.enabled:
            raise TranscriptionError("A transcrição local está desabilitada.")
        payload = self._transcribe_cli(path) if self.config.backend == "cli" else self._transcribe_http(path)
        return _parse_result(payload)

    def _transcribe_cli(self, path: Path) -> dict[str, object]:
        executable = self.config.python_executable
        project_dir = self.config.project_dir
        if executable is None or project_dir is None:
            raise TranscriptionError("O EccoVox CLI não está configurado.")
        command = [
            str(executable), "-m", "eccovox.cli", "transcribe", "--file", str(path),
            "--language", self.config.language,
            "--model", self.config.model,
            "--device", self.config.device,
            "--compute-type", self.config.compute_type,
        ]
        if self.config.profile:
            command.extend(("--profile", self.config.profile))
        if self.config.prompt:
            command.extend(("--prompt", self.config.prompt))
        for term in self.config.terms:
            command.extend(("--term", term))
        for source, target in self.config.aliases:
            command.extend(("--alias", f"{source}={target}"))
        environment = os.environ.copy()
        environment["PYTHONUTF8"] = "1"
        environment["PYTHONIOENCODING"] = "utf-8"
        try:
            completed = subprocess.run(
                command,
                cwd=project_dir,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=self.config.timeout_seconds,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TranscriptionError("O EccoVox CLI não pôde concluir a transcrição.") from exc
        if completed.returncode != 0:
            raise TranscriptionError("O EccoVox CLI recusou ou não concluiu a transcrição.")
        try:
            payload = json.loads(completed.stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            raise TranscriptionError("O EccoVox CLI retornou uma resposta inválida.") from exc
        if not isinstance(payload, dict):
            raise TranscriptionError("O EccoVox CLI retornou uma resposta inválida.")
        return payload

    def _transcribe_http(self, path: Path) -> dict[str, object]:
        boundary = f"eccovox-{uuid.uuid4().hex}"
        fields: list[tuple[str, str]] = [
            ("language", self.config.language),
            ("model", self.config.model),
            ("device", self.config.device),
            ("computeType", self.config.compute_type),
        ]
        if self.config.profile:
            fields.append(("profile", self.config.profile))
        if self.config.prompt:
            fields.append(("prompt", self.config.prompt))
        fields.extend(("term", term) for term in self.config.terms)
        fields.extend(("alias", f"{source}={target}") for source, target in self.config.aliases)
        body = _multipart_body(boundary, fields, path)
        request = urllib.request.Request(
            f"{self.config.endpoint}/v1/audio/transcriptions",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with self._http.open(request, timeout=self.config.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError, urllib.error.URLError) as exc:
            raise TranscriptionError("O EccoVox HTTP não pôde concluir a transcrição.") from exc
        if not isinstance(payload, dict):
            raise TranscriptionError("O EccoVox HTTP retornou uma resposta inválida.")
        return payload


def _multipart_body(boundary: str, fields: list[tuple[str, str]], path: Path) -> bytes:
    delimiter = f"--{boundary}\r\n".encode("ascii")
    body = bytearray()
    for name, value in fields:
        body.extend(delimiter)
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("ascii"))
        body.extend(value.encode("utf-8"))
        body.extend(b"\r\n")
    body.extend(delimiter)
    body.extend(
        b'Content-Disposition: form-data; name="file"; filename="audio.bin"\r\n'
    )
    body.extend(b"Content-Type: application/octet-stream\r\n\r\n")
    body.extend(path.read_bytes())
    body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode("ascii"))
    return bytes(body)


def _parse_result(payload: dict[str, object]) -> TranscriptionResult:
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise TranscriptionError("O EccoVox não retornou texto útil.")
    confidence_value = payload.get("confidence")
    confidence = float(confidence_value) if isinstance(confidence_value, (int, float)) else None
    if confidence is not None and not 0 <= confidence <= 1:
        raise TranscriptionError("O EccoVox retornou confiança inválida.")
    duration_value = payload.get("durationMillis")
    duration = int(duration_value) if isinstance(duration_value, (int, float)) else None
    metadata = payload.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    changes = payload.get("normalizationChanges")
    change_count = len(changes) if isinstance(changes, list) else 0
    raw_text = payload.get("rawText")
    language = payload.get("language")
    model = metadata.get("model")
    return TranscriptionResult(
        text=text.strip(),
        raw_text=raw_text if isinstance(raw_text, str) else None,
        confidence=confidence,
        language=language if isinstance(language, str) else None,
        duration_millis=duration,
        model=model if isinstance(model, str) else None,
        normalization_change_count=change_count,
    )
