"""Offline unit tests for the OpenAI-compatible STT providers and registry."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import httpx
import pytest

from opensquilla.audio.stt_openai import (
    OpenAIChatInputAudioSttProvider,
    OpenAITranscriptionsSttProvider,
)
from opensquilla.audio.stt_registry import (
    SttConfigError,
    build_stt_chain,
    resolve_stt_api_key_env,
)
from opensquilla.audio.stt_types import SttAudio, SttProviderError
from opensquilla.gateway.config import GatewayConfig

_WAV = b"RIFF\x00\x00\x00\x00WAVEfmt " + b"\x00" * 64


def _audio() -> SttAudio:
    return SttAudio(data=_WAV, filename="voice.wav", mime_type="audio/wav")


def _transcriptions_provider(handler, **overrides) -> OpenAITranscriptionsSttProvider:
    kwargs = {
        "api_key": "sk-test",
        "api_key_env": "OPENAI_API_KEY",
        "base_url": "https://api.openai.com/v1",
        "model": "whisper-1",
        "transport": httpx.MockTransport(handler),
    }
    kwargs.update(overrides)
    return OpenAITranscriptionsSttProvider(**kwargs)


async def test_openai_transcriptions_posts_multipart_and_parses_text() -> None:
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = request.content
        return httpx.Response(200, json={"text": "你好世界", "language": "zh"})

    provider = _transcriptions_provider(handler)
    result = await provider.transcribe(_audio(), language="zh")

    assert captured["url"] == "https://api.openai.com/v1/audio/transcriptions"
    assert captured["auth"] == "Bearer sk-test"
    body = captured["body"]
    assert b'name="model"' in body and b"whisper-1" in body
    assert b'name="language"' in body
    assert b'name="file"' in body and b"voice.wav" in body
    assert result.text == "你好世界"
    assert result.provider == "openai_transcriptions"
    assert result.model == "whisper-1"
    assert result.language_code == "zh"


async def test_openai_transcriptions_ignores_unsafe_language_hints() -> None:
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        return httpx.Response(200, json={"text": "ok"})

    provider = _transcriptions_provider(handler)
    await provider.transcribe(_audio(), language="yue")

    assert b'name="language"' not in captured["body"]


async def test_openai_transcriptions_surfaces_server_error_message() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "invalid model"}})

    provider = _transcriptions_provider(handler)
    with pytest.raises(SttProviderError, match="invalid model"):
        await provider.transcribe(_audio())


async def test_openai_transcriptions_rejects_empty_transcription() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"text": "   "})

    provider = _transcriptions_provider(handler)
    with pytest.raises(SttProviderError, match="empty transcription"):
        await provider.transcribe(_audio())


async def test_openai_transcriptions_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not be called")

    provider = _transcriptions_provider(handler, api_key="")
    with pytest.raises(SttProviderError, match="OPENAI_API_KEY is not set"):
        await provider.transcribe(_audio())


def _chat_provider(handler, **overrides) -> OpenAIChatInputAudioSttProvider:
    kwargs = {
        "api_key": "sk-mimo",
        "api_key_env": "MIMO_API_KEY",
        "base_url": "https://api.xiaomimimo.com/v1/",
        "model": "mimo-v2.5-asr",
        "transport": httpx.MockTransport(handler),
    }
    kwargs.update(overrides)
    return OpenAIChatInputAudioSttProvider(**kwargs)


async def test_openai_chat_inlines_input_audio_and_parses_content() -> None:
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "reasoning": "thinking...",
                            "content": "开会记得带笔记本",
                        }
                    }
                ]
            },
        )

    provider = _chat_provider(handler)
    result = await provider.transcribe(_audio())

    assert captured["url"] == "https://api.xiaomimimo.com/v1/chat/completions"
    assert captured["auth"] == "Bearer sk-mimo"
    body = captured["body"]
    assert body["model"] == "mimo-v2.5-asr"
    parts = body["messages"][0]["content"]
    assert parts[0]["type"] == "input_audio"
    assert parts[0]["input_audio"]["data"] == base64.b64encode(_WAV).decode("ascii")
    assert parts[0]["input_audio"]["format"] == "wav"
    assert result.text == "开会记得带笔记本"
    assert result.provider == "openai_chat"


async def test_openai_chat_language_hint_adds_text_part_and_parses_part_list() -> None:
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": [{"type": "text", "text": "partial one"}]}}]},
        )

    provider = _chat_provider(handler)
    result = await provider.transcribe(_audio(), language="zh")

    parts = captured["body"]["messages"][0]["content"]
    assert parts[0]["type"] == "text" and "zh" in parts[0]["text"]
    assert parts[1]["type"] == "input_audio"
    assert result.text == "partial one"


async def test_openai_chat_rejects_missing_choices() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    provider = _chat_provider(handler)
    with pytest.raises(SttProviderError, match="empty transcription"):
        await provider.transcribe(_audio())


async def test_openai_providers_require_configured_model() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not be called")

    provider = _transcriptions_provider(handler, model="")
    with pytest.raises(SttProviderError, match="model is not configured"):
        await provider.transcribe(_audio())


# ---------------------------------------------------------------- registry --


def test_registry_default_chain_is_elevenlabs_only() -> None:
    config = GatewayConfig()
    chain = build_stt_chain(config)
    assert [p.provider_id for p in chain] == ["elevenlabs"]
    assert chain[0].accepts_request_model is True


def test_registry_builds_named_providers_with_ordered_deduped_fallbacks() -> None:
    config = GatewayConfig()
    config.audio.stt.provider = "openai_transcriptions"
    config.audio.stt.openai_transcriptions.model = "whisper-1"
    config.audio.stt.fallback = ["openai_chat", "elevenlabs", "openai_chat"]
    config.audio.stt.openai_chat.model = "mimo-v2.5-asr"

    chain = build_stt_chain(config)
    assert [p.provider_id for p in chain] == [
        "openai_transcriptions",
        "openai_chat",
        "elevenlabs",
    ]
    assert chain[0].accepts_request_model is False


def test_registry_rejects_unknown_provider() -> None:
    config = GatewayConfig()
    config.audio.stt.provider = "acme"
    with pytest.raises(SttConfigError, match="unknown"):
        build_stt_chain(config)


def test_registry_rejects_unknown_fallback_provider() -> None:
    config = GatewayConfig()
    config.audio.stt.fallback = ["acme"]
    with pytest.raises(SttConfigError, match="unknown"):
        build_stt_chain(config)


def test_registry_builds_local_sensevoice_provider() -> None:
    from opensquilla.audio.stt_sensevoice import SenseVoiceSttProvider

    config = GatewayConfig()
    config.audio.stt.provider = "sensevoice"
    config.audio.stt.sensevoice.model_dir = "models/sensevoice"

    chain = build_stt_chain(config)
    assert [p.provider_id for p in chain] == ["sensevoice"]
    assert isinstance(chain[0], SenseVoiceSttProvider)
    assert chain[0].accepts_request_model is False


def test_registry_accepts_legacy_local_sensevoice_names() -> None:
    config = GatewayConfig()
    for name in ("local_sensevoice", "sense_voice"):
        config.audio.stt.provider = name
        assert build_stt_chain(config)[0].provider_id == "sensevoice"


# ------------------------------------------------------------------- local --


def _sensevoice_provider(tmp_path, **overrides) -> object:
    from opensquilla.audio.stt_sensevoice import SenseVoiceSttProvider

    (tmp_path / "model.int8.onnx").write_bytes(b"fake-onnx")
    (tmp_path / "tokens.txt").write_text("hello", encoding="utf-8")
    kwargs = {"model_dir": str(tmp_path), "language": "auto", "num_threads": 1}
    kwargs.update(overrides)
    return SenseVoiceSttProvider(**kwargs)


def test_sensevoice_requires_model_files(tmp_path) -> None:
    from opensquilla.audio.stt_sensevoice import SenseVoiceSttProvider

    provider = SenseVoiceSttProvider(model_dir=str(tmp_path / "missing"))
    with pytest.raises(SttProviderError, match="model not found"):
        provider._model_files()


def test_sensevoice_accepts_plain_model_onnx(tmp_path) -> None:
    provider = _sensevoice_provider(tmp_path)
    provider._model_dir = tmp_path
    (tmp_path / "model.int8.onnx").unlink()
    (tmp_path / "model.onnx").write_bytes(b"fake-onnx")
    model, tokens = provider._model_files()
    assert model.name == "model.onnx"
    assert tokens.name == "tokens.txt"


def test_sensevoice_language_fallback_chain() -> None:
    config = GatewayConfig()
    config.audio.stt.provider = "sensevoice"
    config.audio.stt.language = "zh"
    chain = build_stt_chain(config)
    assert chain[0]._language == "zh"
    # Explicit provider block wins over the global hint.
    config.audio.stt.sensevoice.language = "en"
    chain = build_stt_chain(config)
    assert chain[0]._language == "en"
    # Empty everywhere falls back to auto.
    config.audio.stt.language = ""
    config.audio.stt.sensevoice.language = ""
    chain = build_stt_chain(config)
    assert chain[0]._language == "auto"


def test_sensevoice_resolve_language_accepts_known_tags() -> None:
    from opensquilla.audio.stt_sensevoice import SenseVoiceSttProvider

    provider = SenseVoiceSttProvider(model_dir="models", language="zh")
    assert provider._resolve_language("EN") == "en"
    assert provider._resolve_language("nope") == "zh"  # configured language
    assert provider._resolve_language(None) == "zh"
    fallback = SenseVoiceSttProvider(model_dir="models", language="乱")
    assert fallback._resolve_language(None) == "auto"


async def test_sensevoice_reports_missing_dependency(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import builtins

    provider = _sensevoice_provider(tmp_path)
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "sherpa_onnx":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(SttProviderError, match="sherpa-onnx"):
        await provider.transcribe(_audio())


def test_sensevoice_decode_wav_and_resample(tmp_path) -> None:
    import array
    import io
    import wave

    provider = _sensevoice_provider(tmp_path)
    raw = array.array("h", bytes(44100 * 2))  # 1 second of silence @44.1k
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(44100)
        wav.writeframes(raw.tobytes())
    rate, samples = provider._decode_wav(buf.getvalue())
    assert rate == 44100
    assert len(samples) == 44100
    resampled = provider._resample(rate, samples)
    assert abs(len(resampled) - 16000) <= 1


def test_sensevoice_rejects_non_mono_wav(tmp_path) -> None:
    import array
    import io
    import wave

    provider = _sensevoice_provider(tmp_path)
    raw = array.array("h", bytes(100 * 2 * 2))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(raw.tobytes())
    with pytest.raises(SttProviderError, match="mono 16-bit"):
        provider._decode_wav(buf.getvalue())


# ------------------------------------------------------- credential scoping --


def test_key_env_reuses_default_only_on_official_origin() -> None:
    cfg = SimpleNamespace(
        api_key_env="OPENAI_API_KEY",
        base_url="https://api.openai.com/v1",
        model_fields_set=set(),
    )
    assert resolve_stt_api_key_env(cfg) == "OPENAI_API_KEY"


def test_key_env_does_not_follow_custom_origin_implicitly() -> None:
    cfg = SimpleNamespace(
        api_key_env="OPENAI_API_KEY",
        base_url="https://asr.internal.example/v1",
        model_fields_set=set(),
    )
    assert resolve_stt_api_key_env(cfg) == ""


def test_operator_authored_env_name_wins_everywhere() -> None:
    cfg = SimpleNamespace(
        api_key_env="MY_ASR_KEY",
        base_url="https://asr.internal.example/v1",
        model_fields_set={"api_key_env", "base_url"},
    )
    assert resolve_stt_api_key_env(cfg) == "MY_ASR_KEY"


async def test_custom_origin_without_env_refuses_implicit_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "secret-that-must-not-follow")

    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not send the default-origin key")

    provider = OpenAITranscriptionsSttProvider(
        api_key="",
        api_key_env="",
        base_url="https://asr.internal.example/v1",
        model="whatever",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(SttProviderError, match="no API key configured"):
        await provider.transcribe(_audio())
