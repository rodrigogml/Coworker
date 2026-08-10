"""Síntese de voz EccoVox, isolada do processamento de entrada."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass

from interfaces.telegram.config import SpeechConfig


class SpeechError(RuntimeError):
    pass


@dataclass(frozen=True)
class SpeechResult:
    path: Path
    mime_type: str = "audio/ogg"


def normalize_for_speech(text: str) -> str:
    text = re.sub(r"```(?:[\w+-]+)?\s*([\s\S]*?)```", r"\1", text)
    text = re.sub(r"https?://\S+|www\.\S+", "", text)
    text = re.sub(r"!\[([^]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"^\s{0,3}(#{1,6})\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*[-*+]\s+", "• ", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*\d+[.)]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"[*_~`]+", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def speech_segments(text: str, limit: int) -> list[str]:
    clean = normalize_for_speech(text)
    if not clean:
        return []
    paragraphs = re.split(r"\n\s*\n", clean)
    segments: list[str] = []
    for paragraph in paragraphs:
        paragraph = re.sub(r"\s+", " ", paragraph).strip()
        while len(paragraph) > limit:
            cut = max((m.start() for m in re.finditer(r"[.!?;:]\s+", paragraph[:limit])), default=0)
            if cut < limit // 3:
                cut = paragraph.rfind(" ", 0, limit)
            if cut <= 0:
                cut = limit
            segments.append(paragraph[:cut].strip())
            paragraph = paragraph[cut:].strip()
        if paragraph:
            segments.append(paragraph)
    return segments


class EccoVoxSpeechClient:
    def __init__(self, config: SpeechConfig):
        self.config = config
        self._http = urllib.request.build_opener(_NoRedirectHandler())

    def doctor(self) -> dict[str, str | bool]:
        if not self.config.enabled:
            return {"available": False, "enabled": False, "provider": "eccovox"}
        if self.config.backend == "cli":
            available = bool(self.config.python_executable and self.config.project_dir)
        else:
            available = self._probe_http()
        return {"available": available, "enabled": True, "provider": f"eccovox-{self.config.backend}"}

    def _probe_http(self) -> bool:
        try:
            request = urllib.request.Request(f"{self.config.endpoint}/health", method="GET")
            with self._http.open(request, timeout=min(self.config.timeout_seconds, 5)):
                return True
        except (OSError, urllib.error.URLError):
            return False

    def synthesize(self, text: str, output_dir: Path, *, voice: str, language: str, speed: float) -> SpeechResult:
        if not self.config.enabled:
            raise SpeechError("A síntese de fala está desabilitada.")
        if voice not in self.config.voices or language not in self.config.languages or not 0.25 <= speed <= 4:
            raise SpeechError("Preferência de fala não permitida.")
        output_dir.mkdir(parents=True, exist_ok=True)
        target = (output_dir / f"speech-{uuid.uuid4().hex}.ogg").resolve()
        root = output_dir.resolve()
        target.relative_to(root)
        try:
            if self.config.backend == "cli":
                self._cli(text, target, voice, language, speed)
            else:
                self._http_synthesize(text, target, voice, language, speed)
        except SpeechError:
            target.unlink(missing_ok=True)
            raise
        if not target.is_file() or target.stat().st_size == 0:
            target.unlink(missing_ok=True)
            raise SpeechError("O EccoVox não retornou áudio válido.")
        return SpeechResult(target)

    def _cli(self, text: str, target: Path, voice: str, language: str, speed: float) -> None:
        if not self.config.python_executable or not self.config.project_dir:
            raise SpeechError("O CLI do EccoVox não está configurado.")
        command = [str(self.config.python_executable), "-m", "eccovox.cli", "synthesize", "--text", text, "--voice", voice, "--language", language, "--speed", str(speed), "--format", "opus", "--output", str(target)]
        env = os.environ.copy(); env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
        try:
            result = subprocess.run(command, cwd=self.config.project_dir, env=env, capture_output=True, timeout=self.config.timeout_seconds, check=False, shell=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SpeechError("O EccoVox CLI não pôde sintetizar a fala.") from exc
        if result.returncode != 0:
            raise SpeechError("O EccoVox CLI recusou a síntese.")

    def _http_synthesize(self, text: str, target: Path, voice: str, language: str, speed: float) -> None:
        payload = json.dumps({"input": text, "voice": voice, "language": language, "speed": speed, "format": "opus"}, ensure_ascii=False).encode()
        request = urllib.request.Request(f"{self.config.endpoint}/v1/audio/speech", data=payload, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with self._http.open(request, timeout=self.config.timeout_seconds) as response:
                data = response.read()
                content_type = response.headers.get("Content-Type", "")
        except (OSError, urllib.error.URLError) as exc:
            raise SpeechError("O EccoVox HTTP não pôde sintetizar a fala.") from exc
        if "json" in content_type:
            try:
                value = json.loads(data.decode())
                data = base64.b64decode(value.get("audio") or value.get("data"), validate=True)
            except (ValueError, TypeError, KeyError, base64.binascii.Error) as exc:
                raise SpeechError("O EccoVox HTTP retornou áudio inválido.") from exc
        target.write_bytes(data)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None
