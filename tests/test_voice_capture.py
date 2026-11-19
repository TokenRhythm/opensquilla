"""Unit tests for terminal voice capture + STT-chain transcription (PR-4).

Offline: ffmpeg subprocess and the STT chain are faked. Covers the chain
fallback semantics (empty text is a miss), device-list parsing for both ffmpeg
output formats, recorder command construction, and the /voice helper guards.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.audio import voice_capture as vc
from opensquilla.audio.stt_types import SttAudio, SttResult


def _audio() -> SttAudio:
    return SttAudio(data=b"RIFFxxxx", filename="voice.wav", mime_type="audio/wav")


class _FakeProvider:
    def __init__(self, provider_id: str, *, text: str = "", error: str = "") -> None:
        self.provider_id = provider_id
        self.accepts_request_model = False
        self._text = text
        self._error = error
        self.calls: list[Any] = []

    async def transcribe(self, audio, *, model_id=None, language=None):
        self.calls.append((audio, model_id, language))
        if self._error:
            raise RuntimeError(self._error)
        return SttResult(text=self._text, provider=self.provider_id, model="m")


def _config(language: str = "") -> Any:
    return SimpleNamespace(audio=SimpleNamespace(stt=SimpleNamespace(language=language)))


@pytest.mark.asyncio
async def test_transcribe_first_non_empty_wins(monkeypatch) -> None:
    p1 = _FakeProvider("local", text="")  # empty → miss
    p2 = _FakeProvider("cloud", text="你好")
    monkeypatch.setattr(vc, "build_stt_chain", lambda cfg: [p1, p2])
    result = await vc.transcribe_audio(_config(), _audio())
    assert result is not None and result.text == "你好"
    assert len(p1.calls) == 1 and len(p2.calls) == 1


@pytest.mark.asyncio
async def test_transcribe_all_empty_returns_none(monkeypatch) -> None:
    p1 = _FakeProvider("local", text="")
    p2 = _FakeProvider("cloud", text="   ")
    monkeypatch.setattr(vc, "build_stt_chain", lambda cfg: [p1, p2])
    assert await vc.transcribe_audio(_config(), _audio()) is None


@pytest.mark.asyncio
async def test_transcribe_error_then_success(monkeypatch) -> None:
    p1 = _FakeProvider("local", error="boom")
    p2 = _FakeProvider("cloud", text="ok")
    monkeypatch.setattr(vc, "build_stt_chain", lambda cfg: [p1, p2])
    result = await vc.transcribe_audio(_config(), _audio())
    assert result is not None and result.text == "ok"


@pytest.mark.asyncio
async def test_transcribe_all_errors_raises(monkeypatch) -> None:
    p1 = _FakeProvider("local", error="boom")
    monkeypatch.setattr(vc, "build_stt_chain", lambda cfg: [p1])
    with pytest.raises(vc.VoiceCaptureError):
        await vc.transcribe_audio(_config(), _audio())


@pytest.mark.asyncio
async def test_transcribe_no_provider_raises(monkeypatch) -> None:
    monkeypatch.setattr(vc, "build_stt_chain", lambda cfg: [])
    with pytest.raises(vc.VoiceCaptureError):
        await vc.transcribe_audio(_config(), _audio())


@pytest.mark.asyncio
async def test_transcribe_language_hint_resolution(monkeypatch) -> None:
    p = _FakeProvider("cloud", text="hi")
    monkeypatch.setattr(vc, "build_stt_chain", lambda cfg: [p])
    await vc.transcribe_audio(_config(language="zh"), _audio())
    assert p.calls[0][2] == "zh"  # global language used
    await vc.transcribe_audio(_config(language="zh"), _audio(), language="en")
    assert p.calls[1][2] == "en"  # explicit overrides global


# --------------------------------------------------------- device parsing --


def _mock_devices(monkeypatch, stderr: str) -> None:
    monkeypatch.setattr(vc, "find_ffmpeg", lambda: "ffmpeg")

    class _Proc:
        returncode = 0

        def __init__(self) -> None:
            self.stderr = stderr.encode("utf-8")

    import subprocess

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc())


def test_list_devices_new_format(monkeypatch) -> None:
    _mock_devices(
        monkeypatch,
        '[dshow @ x] "麦克风 (Steam Streaming Microphone)" (audio)\n'
        '[dshow @ x]   Alternative name "@device_cm_{...}"\n'
        '[dshow @ x] "Camera" (video)\n',
    )
    monkeypatch.setattr(vc.sys, "platform", "win32")
    assert vc.list_capture_devices() == ["麦克风 (Steam Streaming Microphone)"]


def test_list_devices_legacy_format(monkeypatch) -> None:
    _mock_devices(
        monkeypatch,
        '[dshow @ x] DirectShow audio devices\n'
        '[dshow @ x] "Microphone (Realtek)"\n'
        '[dshow @ x] DirectShow video devices\n'
        '[dshow @ x] "Webcam"\n',
    )
    monkeypatch.setattr(vc.sys, "platform", "win32")
    assert vc.list_capture_devices() == ["Microphone (Realtek)"]


def test_resolve_device_prefers_configured(monkeypatch) -> None:
    monkeypatch.setattr(vc, "list_capture_devices", lambda: ["Auto Mic"])
    assert vc.resolve_device("My Mic") == "My Mic"
    assert vc.resolve_device("") == "Auto Mic"
    monkeypatch.setattr(vc, "list_capture_devices", lambda: [])
    assert vc.resolve_device("") == ""


# ------------------------------------------------------------- recorder --


def test_capture_input_args_platform(monkeypatch) -> None:
    monkeypatch.setattr(vc.sys, "platform", "win32")
    assert vc._capture_input_args("Mic") == ["-f", "dshow", "-i", "audio=Mic"]
    monkeypatch.setattr(vc.sys, "platform", "darwin")
    assert vc._capture_input_args("0") == ["-f", "avfoundation", "-i", ":0"]
    monkeypatch.setattr(vc.sys, "platform", "linux")
    assert vc._capture_input_args("") == ["-f", "pulse", "-i", "default"]


@pytest.mark.asyncio
async def test_recorder_stop_reads_wav(monkeypatch, tmp_path) -> None:
    wav = tmp_path / "rec.wav"
    wav.write_bytes(b"RIFFdata")
    monkeypatch.setattr(vc, "find_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(vc.tempfile, "mkstemp", lambda **k: (0, str(wav)))
    monkeypatch.setattr(vc.os, "close", lambda fd: None)
    monkeypatch.setattr(vc.os, "unlink", lambda p: None)

    class _Stdin:
        def write(self, b):
            pass

        async def drain(self):
            pass

    class _Proc:
        returncode = None
        stdin = _Stdin()

        async def wait(self):
            self.returncode = 0

    async def _fake_exec(*args, **kwargs):
        return _Proc()

    monkeypatch.setattr(vc.asyncio, "create_subprocess_exec", _fake_exec)
    rec = vc.MicrophoneRecorder(max_seconds=10)
    await rec.start("Mic")
    assert rec.recording
    data = await rec.stop()
    assert data == b"RIFFdata"
    assert not rec.recording


# --------------------------------------------------------- /voice helper --


@pytest.mark.asyncio
async def test_capture_voice_text_host_ui_notice(monkeypatch) -> None:
    from opensquilla.cli.tui.adapters import voice

    printed: list[str] = []
    monkeypatch.setattr(voice.console, "print", lambda *a, **k: printed.append(str(a[0])))
    cfg = SimpleNamespace(audio=SimpleNamespace(enabled=True))
    assert await voice.capture_voice_text(cfg, host_ui=True) is None
    assert any("plain terminal" in p for p in printed)


@pytest.mark.asyncio
async def test_capture_voice_text_disabled(monkeypatch) -> None:
    from opensquilla.cli.tui.adapters import voice

    printed: list[str] = []
    monkeypatch.setattr(voice.console, "print", lambda *a, **k: printed.append(str(a[0])))
    cfg = SimpleNamespace(audio=SimpleNamespace(enabled=False))
    assert await voice.capture_voice_text(cfg) is None
    assert any("disabled" in p for p in printed)
