"""Video credentials resolve from supplied state without a tool context."""

from __future__ import annotations

from types import SimpleNamespace

from opensquilla.provider import video_generation_credentials as credentials


def test_runtime_fallback_receives_explicit_session_and_hides_the_key(monkeypatch) -> None:
    calls = []

    def resolve_image_credential(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            available=True,
            api_key="synthetic-runtime-video-key",
            env_key="DUMMY_VIDEO_PROFILE_KEY",
            source="profile_pool",
            owner="profile",
        )

    monkeypatch.setattr(credentials, "environment_value", lambda _: "")
    monkeypatch.setattr(
        credentials, "resolve_image_generation_credential", resolve_image_credential
    )
    config = SimpleNamespace(provider="xai", primary="grok-imagine-video-1.5")
    gateway = SimpleNamespace()

    result = credentials.resolve_video_generation_credential(
        config,
        gateway_config=gateway,
        runtime=True,
        session_key="synthetic-video-session",
    )

    assert result.available is True
    assert result.source == "llm_fallback"
    assert result.api_key == "synthetic-runtime-video-key"
    assert "synthetic-runtime-video-key" not in repr(result)
    assert calls[0]["runtime"] is True
    assert calls[0]["session_key"] == "synthetic-video-session"
    assert calls[0]["gateway_config"] is gateway
    assert calls[0]["include_image_credentials"] is False


def test_status_fallback_does_not_acquire_a_runtime_credential(monkeypatch) -> None:
    calls = []

    def resolve_image_credential(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            available=False, api_key="", env_key="", source="none", owner="none"
        )

    monkeypatch.setattr(credentials, "environment_value", lambda _: "")
    monkeypatch.setattr(
        credentials, "resolve_image_generation_credential", resolve_image_credential
    )
    gateway = SimpleNamespace(video_generation=SimpleNamespace(provider="xai", primary="video"))

    status = credentials.video_generation_credential_status(gateway, provider_id="xai")

    assert status["available"] is False
    assert calls[0]["runtime"] is False
    assert calls[0]["session_key"] == ""
