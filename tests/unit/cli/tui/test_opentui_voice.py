"""Unit tests for the OpenTUI Alt+V push-to-talk handler (PR-4 hotkey).

Offline: the bridge, microphone recorder, and STT chain are faked. Verifies the
toggle flow (first press records, second press transcribes and fills the
composer), the disabled-audio guard, and that input.voice is consumed by the
input pump rather than surfaced as chat text.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.cli.tui.opentui.messages import HostInputVoice
from opensquilla.cli.tui.opentui.surface import OpenTuiSurface
from opensquilla.engine.commands import Surface


class _FakeBridge:
    def __init__(self) -> None:
        self.sent: list[tuple[str, Any]] = []
        self.messages: asyncio.Queue[object] = asyncio.Queue()

    async def send(self, message_type: str, payload: object | None = None) -> None:
        if payload is not None and not isinstance(payload, dict):
            payload = asdict(payload)
        self.sent.append((message_type, payload))

    async def next_message(self) -> object | None:
        return await self.messages.get()


class _FakeRecorder:
    instances: list[_FakeRecorder] = []

    def __init__(self, *, max_seconds: float = 60) -> None:
        self.started = False
        self.stopped = False
        _FakeRecorder.instances.append(self)

    async def start(self, device: str) -> None:
        self.started = True

    async def stop(self) -> bytes:
        self.stopped = True
        return b"RIFFdata"


def _config(*, enabled: bool = True) -> Any:
    return SimpleNamespace(
        audio=SimpleNamespace(
            enabled=enabled,
            capture=SimpleNamespace(device="", max_seconds=60),
            stt=SimpleNamespace(language=""),
        )
    )


def _patch_voice(monkeypatch, *, text: str = "你好世界", enabled: bool = True) -> None:
    import opensquilla.audio.voice_capture as vc
    import opensquilla.onboarding.config_store as cs

    _FakeRecorder.instances = []
    monkeypatch.setattr(vc, "MicrophoneRecorder", _FakeRecorder)
    monkeypatch.setattr(vc, "resolve_device", lambda d: "Mic")

    async def _transcribe(config, audio, **kw):
        return SimpleNamespace(text=text)

    monkeypatch.setattr(vc, "transcribe_audio", _transcribe)
    monkeypatch.setattr(cs, "load_config", lambda p: _config(enabled=enabled))
    monkeypatch.setattr(cs, "default_config_path", lambda: "config.toml")


@pytest.mark.asyncio
async def test_voice_toggle_records_then_fills_composer(monkeypatch) -> None:
    _patch_voice(monkeypatch)
    bridge = _FakeBridge()
    surface = OpenTuiSurface(bridge, approval_surface=Surface.CLI_GATEWAY)

    surface._toggle_voice()  # first press → start recording
    await asyncio.sleep(0.05)
    assert len(_FakeRecorder.instances) == 1
    assert _FakeRecorder.instances[0].started
    assert any(
        t == "notice.write" and "录音中" in (p or {}).get("text", "")
        for t, p in bridge.sent
    )

    surface._toggle_voice()  # second press → stop + transcribe + fill
    await asyncio.sleep(0.05)
    assert _FakeRecorder.instances[0].stopped
    assert any(
        t == "composer.set" and (p or {}).get("text") == "你好世界"
        for t, p in bridge.sent
    )


@pytest.mark.asyncio
async def test_voice_disabled_audio_notifies(monkeypatch) -> None:
    _patch_voice(monkeypatch, enabled=False)
    bridge = _FakeBridge()
    surface = OpenTuiSurface(bridge, approval_surface=Surface.CLI_GATEWAY)

    surface._toggle_voice()
    await asyncio.sleep(0.05)
    assert _FakeRecorder.instances == []  # never started
    assert any(
        t == "notice.write" and "未启用" in (p or {}).get("text", "")
        for t, p in bridge.sent
    )


@pytest.mark.asyncio
async def test_voice_empty_transcription_notifies(monkeypatch) -> None:
    _patch_voice(monkeypatch, text="")
    bridge = _FakeBridge()
    surface = OpenTuiSurface(bridge, approval_surface=Surface.CLI_GATEWAY)

    surface._toggle_voice()
    await asyncio.sleep(0.05)
    surface._toggle_voice()
    await asyncio.sleep(0.05)
    assert not any(t == "composer.set" for t, _ in bridge.sent)
    assert any(
        t == "notice.write" and "未识别" in (p or {}).get("text", "")
        for t, p in bridge.sent
    )


@pytest.mark.asyncio
async def test_input_voice_consumed_not_returned_as_text(monkeypatch) -> None:
    _patch_voice(monkeypatch)
    bridge = _FakeBridge()
    surface = OpenTuiSurface(bridge, approval_surface=Surface.CLI_GATEWAY)

    bridge.messages.put_nowait(HostInputVoice())
    bridge.messages.put_nowait(HostInputVoice())  # stop again to settle
    # next_line must pump past voice frames; feed EOF to terminate the pump.
    from opensquilla.cli.tui.opentui.messages import HostInputEof

    bridge.messages.put_nowait(HostInputEof())
    assert await surface.next_line() is None
    await asyncio.sleep(0.05)
    assert len(_FakeRecorder.instances) >= 1


@pytest.mark.asyncio
async def test_voice_hold_release_kitty_flow(monkeypatch) -> None:
    _patch_voice(monkeypatch)
    bridge = _FakeBridge()
    surface = OpenTuiSurface(bridge, approval_surface=Surface.CLI_GATEWAY)

    surface._toggle_voice("press")  # key down → start
    await asyncio.sleep(0.05)
    assert _FakeRecorder.instances[0].started
    assert not _FakeRecorder.instances[0].stopped

    surface._toggle_voice("release")  # key up → stop + transcribe + fill
    await asyncio.sleep(0.05)
    assert _FakeRecorder.instances[0].stopped
    assert any(
        t == "composer.set" and (p or {}).get("text") == "你好世界"
        for t, p in bridge.sent
    )


@pytest.mark.asyncio
async def test_voice_release_without_recording_is_noop(monkeypatch) -> None:
    _patch_voice(monkeypatch)
    bridge = _FakeBridge()
    surface = OpenTuiSurface(bridge, approval_surface=Surface.CLI_GATEWAY)

    surface._toggle_voice("release")  # stray key-up, nothing recording
    await asyncio.sleep(0.05)
    assert _FakeRecorder.instances == []
    assert bridge.sent == []


@pytest.mark.asyncio
async def test_voice_input_action_parsed() -> None:
    from opensquilla.cli.tui.opentui.messages import host_message_from_json

    assert host_message_from_json('{"type":"input.voice","action":"press"}').action == "press"
    assert host_message_from_json('{"type":"input.voice","action":"release"}').action == "release"
    # Missing/invalid action defaults to press (forward tolerance).
    assert host_message_from_json('{"type":"input.voice"}').action == "press"
    assert host_message_from_json('{"type":"input.voice","action":"bogus"}').action == "press"
