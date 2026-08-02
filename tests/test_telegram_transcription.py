"""Testes da integração local entre o gateway Telegram e o EccoVox."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from interfaces.telegram.config import ProcessorConfig, TranscriptionConfig
from interfaces.telegram.config import TelegramConfigError, _transcription_config
from interfaces.telegram.contracts import Attachment, InboundMessage
from interfaces.telegram.gateway import build_structured_prompt
from interfaces.telegram.identity import InstanceIdentity
from interfaces.telegram.processors import PreparedContent, ProcessorRegistry
from interfaces.telegram.telegram_api import DownloadedFile
from interfaces.telegram.transcription import EccoVoxClient, TranscriptionResult
from interfaces.telegram.workspace import JobWorkspace


IDENTITY = InstanceIdentity(
    "teste", "Assistente Teste", "pt-BR", "neutral", "", "Resumo", "direto",
    "nenhum", "moderada", "conciso", "Identidade fictícia para testes.",
)


def _processor_config(transcription: TranscriptionConfig) -> ProcessorConfig:
    return ProcessorConfig(1000, 10, 1000, 10, 60, 5, transcription)


def test_remote_endpoint_requiresExplicitHttpsOptIn() -> None:
    with pytest.raises(TelegramConfigError):
        _transcription_config({"transcription": {"endpoint": "https://speech.example.com:8870"}})

    configured = _transcription_config(
        {"transcription": {"endpoint": "https://speech.example.com:8870", "allow_remote": True}}
    )

    assert configured.allow_remote is True
    assert configured.endpoint == "https://speech.example.com:8870"


def test_eccovoxClient_shouldInvokeCliWithoutShellAndParseResult(tmp_path: Path) -> None:
    executable = tmp_path / "python.exe"
    executable.touch()
    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"OggS")
    config = TranscriptionConfig(
        enabled=True,
        backend="cli",
        python_executable=executable,
        project_dir=tmp_path,
        model="medium",
        device="cuda",
        compute_type="int8_float16",
        terms=("Todoist",),
    )
    completed = subprocess.CompletedProcess(
        [],
        0,
        stdout=json.dumps(
            {
                "text": "Verifique o Todoist.",
                "rawText": None,
                "confidence": 0.82,
                "language": "pt",
                "durationMillis": 450,
                "normalizationChanges": [],
                "metadata": {"model": "medium"},
            }
        ),
        stderr="",
    )

    with patch("interfaces.telegram.transcription.subprocess.run", return_value=completed) as run:
        result = EccoVoxClient(config).transcribe(audio)

    assert result.text == "Verifique o Todoist."
    assert result.confidence == 0.82
    assert run.call_args.kwargs["shell"] is False
    assert str(audio) in run.call_args.args[0]


def test_eccovoxClient_shouldStartAndOwnLocalHttpProcess(tmp_path: Path) -> None:
    executable = tmp_path / "python.exe"
    executable.touch()
    config = TranscriptionConfig(
        enabled=True,
        backend="http",
        auto_start=True,
        python_executable=executable,
        project_dir=tmp_path,
        endpoint="http://127.0.0.1:8870",
    )
    process = SimpleNamespace(
        poll=lambda: None,
        terminate=lambda: None,
        wait=lambda timeout: 0,
        kill=lambda: None,
    )
    client = EccoVoxClient(config)

    with (
        patch.object(client, "doctor", side_effect=[{"available": False}, {"available": True}]),
        patch("interfaces.telegram.transcription.subprocess.Popen", return_value=process) as popen,
    ):
        assert client.start() is True

    assert popen.call_args.kwargs["shell"] is False
    assert "serve" in popen.call_args.args[0]
    client.close()


def test_processor_shouldPromoteConfidentVoiceToRequest(tmp_path: Path) -> None:
    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"OggS")
    registry = ProcessorRegistry(
        _processor_config(TranscriptionConfig(enabled=True, minimum_confidence=0.6))
    )
    registry.transcription = SimpleNamespace(
        transcribe=lambda _path: TranscriptionResult(
            "Verifique o Todoist.", None, 0.82, "pt", 450, "medium", 0
        )
    )

    prepared = registry.prepare(
        Attachment("current", "1", detected_mime="audio/ogg", logical_type="voice", local_path=audio)
    )

    assert prepared.processor == "eccovox"
    assert prepared.role == "request"
    assert prepared.text == "Verifique o Todoist."


def test_buildStructuredPrompt_shouldUseVoiceTranscriptAsCurrentRequest(tmp_path: Path) -> None:
    workspace = JobWorkspace.create(tmp_path / "jobs", 1)
    audio = workspace.input_dir / "voice.ogg"
    audio.write_bytes(b"OggS")
    downloaded = DownloadedFile("1", "voice.ogg", audio, "audio/ogg", 4, "0" * 64)
    inbound = InboundMessage((1,), 10, 10, (20,), "")
    prepared = PreparedContent(
        "eccovox",
        "Verifique o Todoist.",
        True,
        "Transcrição local pelo EccoVox.",
        "request",
    )

    prompt = build_structured_prompt(inbound, [downloaded], [prepared], workspace, IDENTITY)

    request_block = prompt.split("Pedido atual:\n", 1)[1].split("\n\nArquivos recebidos", 1)[0]
    assert request_block == "Verifique o Todoist."
    assert "a transcrição foi incorporada ao pedido atual como fala do usuário" in prompt
    assert "conteúdo preparado:\nVerifique o Todoist." not in prompt
