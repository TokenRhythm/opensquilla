from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from starlette.applications import Starlette
from starlette.testclient import TestClient

from opensquilla.audio.stt_registry import SttConfigError
from opensquilla.audio.stt_types import SttProviderError, SttResult
from opensquilla.gateway.audio_transcription import register_audio_transcription_routes
from opensquilla.gateway.config import GatewayConfig


@dataclass
class _CapturedRequest:
    audio_bytes: bytes
    filename: str
    mime_type: str
    model_id: str
    language: str | None = None


class _FakeProvider:
    provider_id = "elevenlabs"
    accepts_request_model = True

    def __init__(self) -> None:
        self.requests: list[_CapturedRequest] = []

    async def transcribe(self, audio, *, model_id=None, language=None):
        effective_model = model_id or "scribe_v2"
        self.requests.append(
            _CapturedRequest(
                audio_bytes=audio.data,
                filename=audio.filename,
                mime_type=audio.mime_type,
                model_id=effective_model,
                language=language,
            )
        )
        return SttResult(
            text="请帮我生成语音",
            provider="elevenlabs",
            model=effective_model,
            language_code="zh",
            language_probability=0.96,
        )


def _client(fake: _FakeProvider) -> TestClient:
    app = Starlette()
    config = GatewayConfig()
    config.auth.mode = "token"
    config.auth.token = "token-123"
    config.audio.enabled = True
    register_audio_transcription_routes(app, config=config, stt_chain_factory=lambda _cfg: [fake])
    return TestClient(app)


def test_audio_transcription_route_requires_token_header() -> None:
    fake = _FakeProvider()
    client = _client(fake)

    response = client.post(
        "/api/audio/transcribe",
        files={"file": ("voice.webm", b"spoken", "audio/webm")},
    )

    assert response.status_code == 401
    assert fake.requests == []


def test_audio_transcription_route_transcribes_multipart_audio() -> None:
    fake = _FakeProvider()
    client = _client(fake)

    response = client.post(
        "/api/audio/transcribe",
        headers={"Authorization": "Bearer token-123"},
        files={"file": ("voice.webm", b"spoken", "audio/webm")},
    )

    assert response.status_code == 200
    assert response.json() == {
        "text": "请帮我生成语音",
        "provider": "elevenlabs",
        "model": "scribe_v2",
        "language_code": "zh",
        "language_probability": 0.96,
    }
    assert fake.requests == [
        _CapturedRequest(
            audio_bytes=b"spoken",
            filename="voice.webm",
            mime_type="audio/webm",
            model_id="scribe_v2",
        )
    ]


def test_audio_transcription_freezes_owner_and_rejects_reused_session_key() -> None:
    from opensquilla.engine.usage_accounting import current_usage_accounting_scope

    captured = []
    node = SimpleNamespace(session_id="old-id", epoch=4, agent_id="main")

    class Storage:
        async def get_session(self, key):
            return node

    class Provider(_FakeProvider):
        async def transcribe(self, audio, *, model_id=None, language=None):
            captured.append(current_usage_accounting_scope().context)
            return await super().transcribe(audio, model_id=model_id, language=language)

    fake = Provider()
    config = GatewayConfig()
    config.audio.enabled = True
    config.auth.mode = "none"
    app = Starlette()
    register_audio_transcription_routes(
        app,
        config=config,
        stt_chain_factory=lambda _cfg: [fake],
        usage_event_sink=object(),
        session_manager=SimpleNamespace(storage=Storage()),
    )
    client = TestClient(app)
    identity = {"session_key": "reused-key", "session_id": "old-id", "session_epoch": "4"}
    first = client.post(
        "/api/audio/transcribe", data=identity, files={"file": ("voice.wav", b"audio", "audio/wav")}
    )
    assert first.status_code == 200
    assert captured[0].session_id == "old-id" and captured[0].session_epoch == 4
    node.session_id, node.epoch = "new-id", 5
    stale = client.post(
        "/api/audio/transcribe", data=identity, files={"file": ("voice.wav", b"audio", "audio/wav")}
    )
    assert stale.status_code == 409 and len(captured) == 1
    draft = client.post(
        "/api/audio/transcribe", files={"file": ("voice.wav", b"audio", "audio/wav")}
    )
    assert draft.status_code == 200
    assert captured[1].session_id.startswith("usage-draft:")
    assert captured[1].turn_id is None


class _BrokenProvider:
    provider_id = "broken"
    accepts_request_model = False

    def __init__(self) -> None:
        self.calls = 0

    async def transcribe(self, audio, *, model_id=None, language=None):
        self.calls += 1
        raise SttProviderError("first backend down")


def _make_config() -> GatewayConfig:
    config = GatewayConfig()
    config.auth.mode = "none"
    config.audio.enabled = True
    return config


def _route_with(chain_factory, config=None) -> TestClient:
    app = Starlette()
    register_audio_transcription_routes(
        app, config=config or _make_config(), stt_chain_factory=chain_factory
    )
    return TestClient(app)


def test_stt_chain_falls_back_to_next_provider() -> None:
    broken = _BrokenProvider()
    healthy = _FakeProvider()
    client = _route_with(lambda _cfg: [broken, healthy])

    response = client.post(
        "/api/audio/transcribe",
        files={"file": ("voice.wav", b"spoken", "audio/wav")},
    )

    assert response.status_code == 200
    assert broken.calls == 1
    assert len(healthy.requests) == 1
    assert response.json()["provider"] == "elevenlabs"


def test_stt_chain_all_failures_report_provider_error() -> None:
    broken = _BrokenProvider()
    client = _route_with(lambda _cfg: [broken])

    response = client.post(
        "/api/audio/transcribe",
        files={"file": ("voice.wav", b"spoken", "audio/wav")},
    )

    assert response.status_code == 502
    payload = response.json()
    assert payload["code"] == "PROVIDER_ERROR"
    assert "first backend down" in payload["error"]


def test_stt_config_error_never_reaches_provider() -> None:
    healthy = _FakeProvider()

    def factory(_cfg):
        raise SttConfigError("unknown provider")

    client = _route_with(factory)
    response = client.post(
        "/api/audio/transcribe",
        files={"file": ("voice.wav", b"spoken", "audio/wav")},
    )

    assert response.status_code == 503
    assert response.json()["code"] == "STT_PROVIDER_MISCONFIGURED"
    assert healthy.requests == []


def test_stale_client_model_id_cannot_leak_to_fixed_model_provider() -> None:
    class _FixedModelProvider:
        provider_id = "openai_chat"
        accepts_request_model = False

        def __init__(self) -> None:
            self.model_ids: list[str | None] = []

        async def transcribe(self, audio, *, model_id=None, language=None):
            self.model_ids.append(model_id)
            return SttResult(text="ok", provider="openai_chat", model="configured-asr")

    fake = _FixedModelProvider()
    client = _route_with(lambda _cfg: [fake])

    response = client.post(
        "/api/audio/transcribe",
        data={"model_id": "scribe_v2"},
        files={"file": ("voice.wav", b"spoken", "audio/wav")},
    )

    assert response.status_code == 200
    # The route must not forward the browser's stale ElevenLabs id to a
    # provider whose model is fixed by configuration.
    assert fake.model_ids == [None]
    assert response.json()["model"] == "configured-asr"


def test_audio_transcription_disabled_flag_returns_unavailable() -> None:
    config = _make_config()
    config.audio.enabled = False
    client = _route_with(lambda _cfg: [_FakeProvider()], config=config)

    response = client.post(
        "/api/audio/transcribe",
        files={"file": ("voice.wav", b"spoken", "audio/wav")},
    )

    assert response.status_code == 503
    assert response.json()["code"] == "UNAVAILABLE"
