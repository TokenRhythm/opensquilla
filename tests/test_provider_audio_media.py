from __future__ import annotations

import json
from pathlib import Path

import pytest

from opensquilla.gateway.config import AudioConfig
from opensquilla.provider.audio import (
    AudioGenerationResult,
    DubbingResult,
    ElevenLabsSharedVoicesResult,
    ElevenLabsSubscriptionResult,
    ElevenLabsVoicesListResult,
    MusicGenerationResult,
    VoiceCloneResult,
    VoiceConversionResult,
)
from opensquilla.tools.types import CallerKind, ToolContext, current_tool_context


def _audio_config(*, enabled: bool = True, api_key: str = "el-test") -> AudioConfig:
    config = AudioConfig(enabled=enabled)
    config.providers.elevenlabs.api_key = api_key
    config.providers.elevenlabs.api_key_env = "ELEVENLABS_API_KEY"
    return config


def _tool_context(tmp_path: Path) -> ToolContext:
    return ToolContext(
        caller_kind=CallerKind.AGENT,
        workspace_dir=str(tmp_path / "workspace"),
    )


def test_audio_default_env_does_not_cross_endpoint_origin(monkeypatch) -> None:
    from opensquilla.tools.builtin import media

    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk-default-origin-key")
    config = AudioConfig(enabled=True)
    provider_config = config.providers.elevenlabs
    provider_config.base_url = "https://other.example.test/v1"
    provider_config.api_key_env = ""

    assert not media._audio_configured(config)
    provider = media._elevenlabs_provider(config)
    assert provider._api_key_env == ""
    assert provider._resolve_api_key() == ""


def test_audio_explicit_env_is_allowed_for_custom_origin(monkeypatch) -> None:
    from opensquilla.tools.builtin import media

    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk-default-origin-key")
    monkeypatch.setenv("CUSTOM_AUDIO_KEY", "sk-custom-origin-key")
    config = AudioConfig(enabled=True)
    provider_config = config.providers.elevenlabs
    provider_config.base_url = "https://other.example.test/v1"
    provider_config.api_key_env = "CUSTOM_AUDIO_KEY"

    assert media._audio_configured(config)
    provider = media._elevenlabs_provider(config)
    assert provider._api_key_env == "CUSTOM_AUDIO_KEY"
    assert provider._resolve_api_key() == "sk-custom-origin-key"


@pytest.mark.anyio
async def test_tts_uses_elevenlabs_and_writes_audio(monkeypatch, tmp_path: Path) -> None:
    from opensquilla.tools.builtin import media

    captured: dict[str, object] = {}

    class FakeProvider:
        def __init__(self, **_kwargs):
            return None

        async def text_to_speech(self, request):
            captured["request"] = request
            return AudioGenerationResult(
                audio_bytes=b"spoken-audio",
                provider="elevenlabs",
                model=request.model_id,
                voice=request.voice,
                response_format=request.output_format,
                mime_type="audio/mpeg",
                generation_id="gen_tts",
            )

    monkeypatch.setattr(media, "ElevenLabsAudioProductionProvider", FakeProvider)
    media.configure_audio(_audio_config())

    token = current_tool_context.set(_tool_context(tmp_path))
    try:
        payload = await media.tts(
            text="Speak this",
            voice="voice_123",
            output_path="speech.mp3",
            language_code="en-GB",
            speed=0.92,
            stability=0.65,
            similarity_boost=0.8,
            style=0.0,
            use_speaker_boost=True,
        )
    finally:
        current_tool_context.reset(token)
        media.configure_audio(None)

    target = tmp_path / "workspace" / "speech.mp3"
    result = json.loads(payload)
    assert target.read_bytes() == b"spoken-audio"
    assert result["status"] == "ok"
    assert result["provider"] == "elevenlabs"
    assert result["path"] == str(target)
    request = captured["request"]
    assert getattr(request, "text") == "Speak this"
    assert getattr(request, "voice") == "voice_123"
    assert getattr(request, "language_code") == "en-GB"
    assert getattr(request, "voice_settings") == {
        "stability": 0.65,
        "similarity_boost": 0.8,
        "style": 0.0,
        "use_speaker_boost": True,
        "speed": 0.92,
    }


@pytest.mark.anyio
async def test_tts_uses_configured_elevenlabs_voice_when_voice_is_omitted(
    monkeypatch, tmp_path: Path
) -> None:
    from opensquilla.tools.builtin import media

    captured: dict[str, object] = {}

    class FakeProvider:
        def __init__(self, **_kwargs):
            return None

        async def text_to_speech(self, request):
            captured["voice"] = request.voice
            return AudioGenerationResult(
                audio_bytes=b"default-voice-audio",
                provider="elevenlabs",
                model=request.model_id,
                voice=request.voice,
                response_format=request.output_format,
                mime_type="audio/mpeg",
            )

    config = _audio_config()
    config.tts.voice = "configured_voice"
    monkeypatch.setattr(media, "ElevenLabsAudioProductionProvider", FakeProvider)
    media.configure_audio(config)

    token = current_tool_context.set(_tool_context(tmp_path))
    try:
        payload = await media.tts(text="Default voice", output_path="default.mp3")
    finally:
        current_tool_context.reset(token)
        media.configure_audio(None)

    result = json.loads(payload)
    assert result["status"] == "ok"
    assert result["voice"] == "configured_voice"
    assert captured["voice"] == "configured_voice"


@pytest.mark.anyio
async def test_voice_clone_requires_consent_metadata(tmp_path: Path) -> None:
    from opensquilla.tools.builtin import media

    sample = tmp_path / "workspace" / "sample.mp3"
    sample.parent.mkdir(parents=True)
    sample.write_bytes(b"ID3sample")
    media.configure_audio(_audio_config())

    token = current_tool_context.set(_tool_context(tmp_path))
    try:
        payload = await media.voice_clone(sample_audio="sample.mp3", name="Demo")
    finally:
        current_tool_context.reset(token)
        media.configure_audio(None)

    result = json.loads(payload)
    assert result["status"] == "consent_required"
    assert result["tool"] == "voice_clone"


@pytest.mark.anyio
async def test_voice_clone_calls_elevenlabs_with_consent(monkeypatch, tmp_path: Path) -> None:
    from opensquilla.tools.builtin import media

    captured: dict[str, object] = {}

    class FakeProvider:
        def __init__(self, **_kwargs):
            return None

        async def clone_voice(self, request):
            captured["request"] = request
            return VoiceCloneResult(
                provider="elevenlabs",
                voice_id="voice_new",
                name=request.name,
            )

    sample = tmp_path / "workspace" / "sample.mp3"
    sample.parent.mkdir(parents=True)
    sample.write_bytes(b"ID3sample")
    monkeypatch.setattr(media, "ElevenLabsAudioProductionProvider", FakeProvider)
    media.configure_audio(_audio_config())

    token = current_tool_context.set(_tool_context(tmp_path))
    try:
        payload = await media.voice_clone(
            sample_audio="sample.mp3",
            name="Demo",
            consent_metadata={"speaker": "me", "consent": True},
        )
    finally:
        current_tool_context.reset(token)
        media.configure_audio(None)

    result = json.loads(payload)
    assert result["status"] == "ok"
    assert result["voice_id"] == "voice_new"
    request = captured["request"]
    assert getattr(request, "sample_audio_bytes") == b"ID3sample"
    assert getattr(request, "name") == "Demo"


@pytest.mark.anyio
async def test_voice_convert_calls_elevenlabs_and_writes_audio(monkeypatch, tmp_path: Path) -> None:
    from opensquilla.tools.builtin import media

    class FakeProvider:
        def __init__(self, **_kwargs):
            return None

        async def convert_voice(self, request):
            return VoiceConversionResult(
                audio_bytes=b"converted-audio",
                provider="elevenlabs",
                model=request.model_id,
                voice=request.target_voice,
                response_format=request.output_format,
                mime_type="audio/mpeg",
            )

    source = tmp_path / "workspace" / "source.mp3"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"ID3source")
    monkeypatch.setattr(media, "ElevenLabsAudioProductionProvider", FakeProvider)
    media.configure_audio(_audio_config())

    token = current_tool_context.set(_tool_context(tmp_path))
    try:
        payload = await media.voice_convert(
            source_audio="source.mp3",
            target_voice="voice_123",
            output_path="converted.mp3",
            consent_metadata={"speaker": "me", "consent": True},
        )
    finally:
        current_tool_context.reset(token)
        media.configure_audio(None)

    target = tmp_path / "workspace" / "converted.mp3"
    result = json.loads(payload)
    assert target.read_bytes() == b"converted-audio"
    assert result["status"] == "ok"
    assert result["voice"] == "voice_123"


@pytest.mark.anyio
async def test_dubbing_generate_submits_elevenlabs_job(monkeypatch, tmp_path: Path) -> None:
    from opensquilla.tools.builtin import media

    captured: dict[str, object] = {}

    class FakeProvider:
        def __init__(self, **_kwargs):
            return None

        async def create_dubbing(self, request):
            captured["request"] = request
            return DubbingResult(
                provider="elevenlabs",
                dubbing_id="dub_123",
                status="submitted",
                target_language=request.target_language,
                source_language=request.source_language,
            )

    source = tmp_path / "workspace" / "source.mp3"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"ID3source")
    monkeypatch.setattr(media, "ElevenLabsAudioProductionProvider", FakeProvider)
    media.configure_audio(_audio_config())

    token = current_tool_context.set(_tool_context(tmp_path))
    try:
        payload = await media.dubbing_generate(
            source_media="source.mp3",
            target_language="zh",
            source_language="en",
        )
    finally:
        current_tool_context.reset(token)
        media.configure_audio(None)

    result = json.loads(payload)
    assert result["status"] == "ok"
    assert result["dubbing_id"] == "dub_123"
    assert result["target_language"] == "zh"
    assert getattr(captured["request"], "watermark") is True


@pytest.mark.anyio
async def test_music_generate_calls_elevenlabs_and_writes_audio(
    monkeypatch, tmp_path: Path
) -> None:
    from opensquilla.tools.builtin import media

    captured: dict[str, object] = {}

    class FakeProvider:
        def __init__(self, **_kwargs):
            return None

        async def generate_music(self, request):
            captured["request"] = request
            return MusicGenerationResult(
                audio_bytes=b"music-audio",
                provider="elevenlabs",
                model=request.model_id,
                response_format=request.output_format,
                mime_type="audio/mpeg",
                generation_id="music_123",
            )

    monkeypatch.setattr(media, "ElevenLabsAudioProductionProvider", FakeProvider)
    media.configure_audio(_audio_config())

    token = current_tool_context.set(_tool_context(tmp_path))
    try:
        payload = await media.music_generate(
            prompt="upbeat product intro",
            style="electronic",
            duration_seconds=12,
            output_path="intro.mp3",
        )
    finally:
        current_tool_context.reset(token)
        media.configure_audio(None)

    target = tmp_path / "workspace" / "intro.mp3"
    result = json.loads(payload)
    assert target.read_bytes() == b"music-audio"
    assert result["status"] == "ok"
    assert result["path"] == str(target)
    request = captured["request"]
    assert getattr(request, "prompt") == "upbeat product intro\nStyle: electronic"
    assert getattr(request, "duration_seconds") == 12
    assert getattr(request, "force_instrumental") is True


@pytest.mark.anyio
async def test_song_generate_calls_elevenlabs_music_provider_with_lyrics(
    monkeypatch, tmp_path: Path
) -> None:
    from opensquilla.tools.builtin import media

    captured: dict[str, object] = {}

    class FakeProvider:
        def __init__(self, **_kwargs):
            return None

        async def generate_music(self, request):
            captured["request"] = request
            return MusicGenerationResult(
                audio_bytes=b"song-audio",
                provider="elevenlabs",
                model=request.model_id,
                response_format=request.output_format,
                mime_type="audio/mpeg",
            )

    monkeypatch.setattr(media, "ElevenLabsAudioProductionProvider", FakeProvider)
    media.configure_audio(_audio_config())

    token = current_tool_context.set(_tool_context(tmp_path))
    try:
        payload = await media.song_generate(
            lyrics="original line one\noriginal line two",
            vocal_style="warm alto",
            backing_style="acoustic",
            duration_seconds=30,
            output_path="song.mp3",
        )
    finally:
        current_tool_context.reset(token)
        media.configure_audio(None)

    target = tmp_path / "workspace" / "song.mp3"
    result = json.loads(payload)
    assert target.read_bytes() == b"song-audio"
    assert result["status"] == "ok"
    request = captured["request"]
    assert getattr(request, "lyrics") == "original line one\noriginal line two"
    assert getattr(request, "force_instrumental") is False


@pytest.mark.anyio
async def test_song_generate_retries_short_preview_when_quota_exceeded(
    monkeypatch, tmp_path: Path
) -> None:
    from opensquilla.tools.builtin import media

    requests: list[object] = []

    class FakeProvider:
        def __init__(self, **_kwargs):
            return None

        async def generate_music(self, request):
            requests.append(request)
            if len(requests) == 1:
                raise RuntimeError(
                    "Music generation provider returned HTTP 401 Unauthorized: "
                    '{"detail":{"code":"quota_exceeded","message":"This request '
                    'exceeds your API key quota. You have 2344 credits remaining, '
                    'while 2888 credits are required for this request."}}'
                )
            return MusicGenerationResult(
                audio_bytes=b"short-song-audio",
                provider="elevenlabs",
                model=request.model_id,
                response_format=request.output_format,
                mime_type="audio/mpeg",
            )

    monkeypatch.setattr(media, "ElevenLabsAudioProductionProvider", FakeProvider)
    media.configure_audio(_audio_config())

    long_lyrics = "\n".join(
        [
            "[副歌]",
            "夏天的风吹过你的头发",
            "那是我们一起走过的年华",
            "阳光洒在石板路上",
            "你的笑容像花儿一样绽放",
            "[主歌]",
            "还记得那年放学的傍晚",
            "你背着书包站在校门口等",
            "我说今天作业又留了一大堆",
            "你说没关系慢慢做别太认真",
        ]
    )
    token = current_tool_context.set(_tool_context(tmp_path))
    try:
        payload = await media.song_generate(
            lyrics=long_lyrics,
            vocal_style="温暖男声",
            backing_style="木吉他民谣",
            output_path="song.mp3",
        )
    finally:
        current_tool_context.reset(token)
        media.configure_audio(None)

    target = tmp_path / "workspace" / "song.mp3"
    result = json.loads(payload)
    assert target.read_bytes() == b"short-song-audio"
    assert result["status"] == "ok"
    assert result["quota_retry"]["strategy"] == "short_preview"
    assert len(requests) == 2
    retry_request = requests[1]
    assert getattr(retry_request, "duration_seconds") == 8.0
    assert len(getattr(retry_request, "lyrics")) < len(long_lyrics)
    assert getattr(retry_request, "force_instrumental") is False


@pytest.mark.anyio
async def test_audio_provider_capabilities_reports_live_subscription(
    monkeypatch,
) -> None:
    from opensquilla.tools.builtin import media

    class FakeProvider:
        def __init__(self, **_kwargs):
            return None

        async def get_subscription(self, _request):
            return ElevenLabsSubscriptionResult(
                provider="elevenlabs",
                tier="creator",
                status="active",
                raw={"tier": "creator"},
            )

        async def list_voices(self, _request):
            return ElevenLabsVoicesListResult(
                provider="elevenlabs",
                voices=[{"voice_id": "voice_123"}],
                raw={"voices": [{"voice_id": "voice_123"}]},
            )

    monkeypatch.setattr(media, "ElevenLabsAudioProductionProvider", FakeProvider)
    media.configure_audio(_audio_config())
    try:
        payload = await media.audio_provider_capabilities(probe_live=True)
    finally:
        media.configure_audio(None)

    result = json.loads(payload)
    assert result["status"] == "ok"
    assert result["provider"] == "elevenlabs"
    assert result["configured"] is True
    assert result["subscription"]["tier"] == "creator"
    assert result["voice_count"] == 1
    assert result["capabilities"]["voice_cloning"]["status"] == "available"


@pytest.mark.anyio
async def test_voice_search_returns_shared_voices_for_locale_matching(monkeypatch) -> None:
    from opensquilla.tools.builtin import media

    captured: dict[str, object] = {}

    class FakeProvider:
        def __init__(self, **_kwargs):
            return None

        async def search_shared_voices(self, request):
            captured["request"] = request
            return ElevenLabsSharedVoicesResult(
                provider="elevenlabs",
                voices=[
                    {
                        "name": "Mandarin Narrator",
                        "voice_id": "voice_zh",
                        "public_owner_id": "owner_123",
                        "language": "zh",
                        "accent": "beijing mandarin",
                        "gender": "female",
                        "category": "professional",
                    }
                ],
                raw={"has_more": False},
            )

    monkeypatch.setattr(media, "ElevenLabsAudioProductionProvider", FakeProvider)
    media.configure_audio(_audio_config())
    try:
        payload = await media.voice_search(
            language="zh",
            accent="beijing mandarin",
            page_size=3,
        )
    finally:
        media.configure_audio(None)

    result = json.loads(payload)
    assert result["status"] == "ok"
    assert result["voices"][0]["voice_id"] == "voice_zh"
    assert getattr(captured["request"], "language") == "zh"
    assert getattr(captured["request"], "accent") == "beijing mandarin"
    assert getattr(captured["request"], "page_size") == 3
