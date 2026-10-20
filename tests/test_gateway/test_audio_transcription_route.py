from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from starlette.applications import Starlette
from starlette.testclient import TestClient

from opensquilla.gateway.audio_transcription import register_audio_transcription_routes
from opensquilla.gateway.config import GatewayConfig
from opensquilla.provider.audio import ElevenLabsSpeechToTextResult


@dataclass
class _CapturedRequest:
    audio_bytes: bytes
    filename: str
    mime_type: str
    model_id: str


class _FakeProvider:
    def __init__(self) -> None:
        self.requests: list[_CapturedRequest] = []

    async def transcribe_audio(self, request):
        self.requests.append(
            _CapturedRequest(
                audio_bytes=request.audio_bytes,
                filename=request.filename,
                mime_type=request.mime_type,
                model_id=request.model_id,
            )
        )
        return ElevenLabsSpeechToTextResult(
            text="请帮我生成语音",
            provider="elevenlabs",
            model=request.model_id,
            language_code="zh",
            language_probability=0.96,
        )


def _client(fake: _FakeProvider) -> TestClient:
    app = Starlette()
    config = GatewayConfig()
    config.auth.mode = "token"
    config.auth.token = "token-123"
    config.audio.enabled = True
    register_audio_transcription_routes(app, config=config, provider_factory=lambda _cfg: fake)
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
        async def transcribe_audio(self, request):
            captured.append(current_usage_accounting_scope().context)
            return await super().transcribe_audio(request)

    fake = Provider()
    config = GatewayConfig()
    config.audio.enabled = True
    config.auth.mode = "none"
    app = Starlette()
    register_audio_transcription_routes(
        app,
        config=config,
        provider_factory=lambda _cfg: fake,
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
