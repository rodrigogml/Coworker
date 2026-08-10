from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import mock

from interfaces.telegram.config import SpeechConfig
from interfaces.telegram.speech import EccoVoxSpeechClient, normalize_for_speech, speech_segments
from interfaces.telegram.state import StateStore


def test_normalization_and_segmentation_remove_structure_without_losing_text():
    text = "# Título\n\n- primeiro item\n- segundo [item](https://example.test)\n\n```py\nprint('x')\n```"
    clean = normalize_for_speech(text)
    assert "Título" in clean and "primeiro item" in clean and "https://" not in clean
    assert all(len(item) <= 20 for item in speech_segments(clean, 20))


def test_audio_preferences_are_separate_and_persistent():
    with tempfile.TemporaryDirectory() as temporary:
        store = StateStore(Path(temporary))
        assert not store.audio_preferences(7).audio_enabled
        store.set_audio_preference(7, "audio_enabled", True)
        store.set_audio_preference(7, "voice", "v1")
        assert store.audio_preferences(7).voice == "v1"
        assert store.audio_preferences(8).voice is None
        store.close()


def test_cli_speech_has_no_shell_and_writes_to_job_output():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        python = root / "python.exe"; python.touch()
        project = root / "eccovox"; project.mkdir()
        config = SpeechConfig(True, "cli", python, project, voices=("v1",), languages=("pt-BR",), default_voice="v1")
        client = EccoVoxSpeechClient(config)
        with mock.patch("subprocess.run") as run:
            run.return_value.returncode = 0
            output = root / "job" / "derived"
            output.mkdir(parents=True)
            def fake_run(*args, **kwargs):
                Path(args[0][-1]).write_bytes(b"OggS")
                return run.return_value
            run.side_effect = fake_run
            result = client.synthesize("Olá", output, voice="v1", language="pt-BR", speed=1)
        assert result.path.parent == output.resolve()
        assert run.call_args.kwargs["shell"] is False
