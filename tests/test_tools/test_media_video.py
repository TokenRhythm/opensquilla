from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.gateway.config import GatewayConfig, VideoGenerationConfig
from opensquilla.provider import video_generation
from opensquilla.provider.video_generation_policy import VIDEO_GENERATION_OFFICIAL_BASE_URLS
from opensquilla.tools.builtin import media
from opensquilla.tools.policy_runtime import (
    ToolSurfaceCapabilities,
    detect_runtime_tool_surface_capabilities,
    resolve_runtime_tool_surface,
)
from opensquilla.tools.registry import get_default_registry
from opensquilla.tools.types import CallerKind, ToolContext, ToolError, current_tool_context


def _video_config() -> VideoGenerationConfig:
    return VideoGenerationConfig(enabled=True, primary="google/veo-3.1-fast")


def _gemini_config() -> VideoGenerationConfig:
    return VideoGenerationConfig(
        enabled=True,
        provider="gemini",
        primary="veo-3.1-generate-preview",
    )


def _context(tmp_path: Path, *, artifacts: bool = False) -> ToolContext:
    return ToolContext(
        is_owner=True,
        caller_kind=CallerKind.WEB,
        workspace_dir=str(tmp_path / "workspace"),
        artifact_media_root=str(tmp_path / "artifacts") if artifacts else None,
        artifact_session_id="video-session" if artifacts else None,
        session_key="agent:main:video-test" if artifacts else None,
    )


def test_video_tool_capability_requires_opt_in_and_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    media.configure_video_generation(None)
    try:
        assert media.video_generation_available() is False
        config = _video_config()
        assert config.provider == ""
        assert config.effective_provider == "openrouter"
        media.configure_video_generation(config)
        assert media.video_generation_available() is False
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-video-key")
        assert media.video_generation_available() is True
    finally:
        media.configure_video_generation(None)


def test_gemini_video_route_requires_its_own_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    media.configure_video_generation(_gemini_config())
    try:
        assert media.video_generation_available() is False
        monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
        assert media.video_generation_available() is True
        config, credential = media._video_request_config()
        assert config.effective_provider == "gemini"
        assert credential.api_key == "test-gemini-key"
    finally:
        media.configure_video_generation(None)


def test_video_route_can_reuse_matching_primary_openrouter_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    gateway = GatewayConfig.model_validate(
        {
            "llm": {
                "provider": "openrouter",
                "model": "openai/gpt-4o-mini",
                "api_key": "test-primary-key",
                "base_url": "https://openrouter.ai/api/v1",
            },
            "video_generation": {"enabled": True, "primary": "google/veo-3.1-fast"},
        }
    )
    media.configure_video_generation(gateway.video_generation, gateway_config=gateway)
    try:
        assert media.video_generation_available() is True
    finally:
        media.configure_video_generation(None)


def test_saved_openrouter_provider_precedes_implicit_environment_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "dedicated-video-key")
    gateway = GatewayConfig.model_validate(
        {
            "llm": {
                "provider": "openrouter",
                "model": "openai/gpt-4o-mini",
                "api_key": "proxy-only-key",
                "base_url": "https://proxy.example/api/v1",
            },
            "video_generation": {"enabled": True, "primary": "google/veo-3.1-fast"},
        }
    )
    media.configure_video_generation(gateway.video_generation, gateway_config=gateway)
    try:
        assert media.video_generation_available() is True
        _, credential = media._video_request_config()
        assert credential.api_key == "proxy-only-key"
        assert credential.source == "llm_fallback"
        assert media._video_base_url(
            gateway.video_generation, "openrouter", gateway_config=gateway
        ) == "https://proxy.example/api/v1"
    finally:
        media.configure_video_generation(None)


@pytest.mark.asyncio
async def test_shared_provider_submission_and_recovery_keep_the_job_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    endpoint = "https://provider-proxy.example/videos/v1"
    gateway = GatewayConfig.model_validate({
        "llm": {"provider": "openrouter", "api_key": "synthetic-provider-video-key",
                "base_url": endpoint},
        "video_generation": {"enabled": True, "primary": "google/veo-3.1-fast"},
    })
    calls: list[dict[str, object]] = []

    async def fake_generate(**kwargs: object) -> None:
        calls.append(kwargs)
        raise video_generation.VideoGenerationPending("waiting", job_id="shared-provider-job")

    async def fake_resume(**kwargs: object) -> video_generation.VideoGenerationResult:
        calls.append(kwargs)
        path = kwargs["output_path"]
        assert isinstance(path, Path)
        path.write_bytes(b"\x00\x00\x00\x0cftypisom")
        return video_generation.VideoGenerationResult(
            job_id="shared-provider-job", output_path=path, model=str(kwargs["model"]),
            bytes_written=12,
        )

    monkeypatch.setattr(video_generation, "generate_openrouter_video", fake_generate)
    monkeypatch.setattr(video_generation, "resume_openrouter_video", fake_resume)
    media.configure_video_generation(gateway.video_generation, gateway_config=gateway)
    token = current_tool_context.set(_context(tmp_path, artifacts=True))
    handle = ""
    try:
        pending = json.loads(await media.video_generate("A synthetic clip of a paper kite"))
        handle = pending["job_id"]
        assert media._video_job_receipt(handle).base_url == endpoint
        gateway.llm.base_url = "https://provider-proxy.example/new-api/v1"
        finished = json.loads(await media.video_status(handle))
        assert finished["status"] == "ok"
        gateway.llm.base_url = "https://another-provider-proxy.example/v1"
        with pytest.raises(ToolError, match="unavailable in this session"):
            await media.video_status(handle)
    finally:
        current_tool_context.reset(token)
        media.configure_video_generation(None)
        if handle:
            with media._video_job_sessions_lock:
                media._video_job_sessions.pop(handle, None)
    assert [call["base_url"] for call in calls] == [endpoint, endpoint]
    assert [call["api_key"] for call in calls] == [
        "synthetic-provider-video-key", "synthetic-provider-video-key"
    ]


def test_tokenrhythm_video_reuses_same_origin_image_direct_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TOKENRHYTHM_API_KEY", raising=False)
    gateway = GatewayConfig.model_validate(
        {
            "image_generation": {
                "providers": {"tokenrhythm": {"api_key": "synthetic-image-key"}}
            },
            "video_generation": {
                "enabled": True,
                "provider": "tokenrhythm",
                "primary": "wan3.0-video",
            },
        }
    )
    media.configure_video_generation(gateway.video_generation, gateway_config=gateway)
    try:
        assert media.video_generation_available() is True
        _, credential = media._video_request_config()
        assert credential.api_key == "synthetic-image-key"
        assert credential.source == "image_direct"
        status = media.video_generation_credential_status(
            gateway, provider_id="tokenrhythm"
        )
        assert status == {
            "providerId": "tokenrhythm",
            "available": True,
            "source": "image_direct",
            "owner": "image",
            "envKey": "",
            "clearable": False,
            "baseUrl": VIDEO_GENERATION_OFFICIAL_BASE_URLS["tokenrhythm"],
            "baseUrlSource": "default",
            "baseUrlAuthored": False,
            "apiKeyEnvAuthored": False,
            "sharedBaseUrl": "",
            "sharedCredentialAvailable": False,
        }
    finally:
        media.configure_video_generation(None)


def test_video_image_key_reuse_stops_at_origin_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TOKENRHYTHM_API_KEY", raising=False)
    gateway = GatewayConfig.model_validate(
        {
            "image_generation": {
                "providers": {"tokenrhythm": {"api_key": "synthetic-image-key"}}
            },
            "video_generation": {
                "enabled": True,
                "provider": "tokenrhythm",
                "primary": "wan3.0-video",
                "providers": {
                    "tokenrhythm": {"base_url": "https://video-proxy.example/v1"}
                },
            },
        }
    )
    media.configure_video_generation(gateway.video_generation, gateway_config=gateway)
    try:
        assert media.video_generation_available() is False
        status = media.video_generation_credential_status(
            gateway,
            provider_id="tokenrhythm",
            base_url="https://another-proxy.example/v1",
        )
        assert status["available"] is False
        assert status["source"] != "image_direct"
    finally:
        media.configure_video_generation(None)


def test_video_reuses_image_env_for_matching_custom_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TOKENRHYTHM_API_KEY", raising=False)
    monkeypatch.setenv("IMAGE_PROXY_TOKEN", "synthetic-image-env-key")
    gateway = GatewayConfig.model_validate(
        {
            "image_generation": {
                "providers": {
                    "tokenrhythm": {
                        "base_url": "https://media-proxy.example/images/v1",
                        "api_key_env": "IMAGE_PROXY_TOKEN",
                    }
                }
            },
            "video_generation": {
                "enabled": True,
                "provider": "tokenrhythm",
                "primary": "wan3.0-video",
                "providers": {
                    "tokenrhythm": {"base_url": "https://media-proxy.example/videos/v1"}
                },
            },
        }
    )
    status = media.video_generation_credential_status(gateway, provider_id="tokenrhythm")
    assert status == {
        "providerId": "tokenrhythm",
        "available": True,
        "source": "image_env",
        "owner": "image",
        "envKey": "IMAGE_PROXY_TOKEN",
        "clearable": False,
        "baseUrl": "https://media-proxy.example/videos/v1",
        "baseUrlSource": "video",
        "baseUrlAuthored": True,
        "apiKeyEnvAuthored": False,
        "sharedBaseUrl": "",
        "sharedCredentialAvailable": False,
    }


def test_explicit_video_key_overrides_image_key_and_remains_endpoint_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TOKENRHYTHM_API_KEY", raising=False)
    gateway = GatewayConfig.model_validate(
        {
            "image_generation": {
                "providers": {"tokenrhythm": {"api_key": "synthetic-image-key"}}
            },
            "video_generation": {
                "enabled": True,
                "provider": "tokenrhythm",
                "primary": "wan3.0-video",
                "providers": {"tokenrhythm": {"api_key": "synthetic-video-key"}},
            },
        }
    )
    media.configure_video_generation(gateway.video_generation, gateway_config=gateway)
    try:
        _, credential = media._video_request_config()
        assert credential.api_key == "synthetic-video-key"
        assert credential.source == "video_direct"
        status = media.video_generation_credential_status(
            gateway, provider_id="tokenrhythm", base_url="https://proxy.example/v1"
        )
        assert status["available"] is False
        assert status["owner"] == "video"
    finally:
        media.configure_video_generation(None)


def test_stale_video_direct_key_can_use_authored_env_without_reusing_old_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIDEO_PROXY_KEY", "synthetic-new-origin-key")
    gateway = GatewayConfig.model_validate(
        {
            "video_generation": {
                "enabled": True,
                "provider": "tokenrhythm",
                "primary": "wan3.0-video",
                "providers": {
                    "tokenrhythm": {
                        "base_url": "https://new-video-origin.example/v1",
                        "api_key": "synthetic-old-origin-key",
                        "api_key_base_url": VIDEO_GENERATION_OFFICIAL_BASE_URLS["tokenrhythm"],
                        "api_key_env": "VIDEO_PROXY_KEY",
                    }
                },
            }
        }
    )
    status = media.video_generation_credential_status(gateway, provider_id="tokenrhythm")
    assert status == {
        "providerId": "tokenrhythm",
        "available": True,
        "source": "video_env",
        "owner": "video",
        "envKey": "VIDEO_PROXY_KEY",
        "clearable": False,
        "baseUrl": "https://new-video-origin.example/v1",
        "baseUrlSource": "video",
        "baseUrlAuthored": True,
        "apiKeyEnvAuthored": True,
        "sharedBaseUrl": "",
        "sharedCredentialAvailable": False,
    }
    media.configure_video_generation(gateway.video_generation, gateway_config=gateway)
    try:
        _, credential = media._video_request_config()
        assert credential.api_key == "synthetic-new-origin-key"
    finally:
        media.configure_video_generation(None)


def test_video_credential_status_uses_passed_gateway_not_current_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TOKENRHYTHM_API_KEY", raising=False)
    configured = GatewayConfig.model_validate(
        {
            "video_generation": {
                "provider": "tokenrhythm",
                "primary": "wan3.0-video",
                "providers": {"tokenrhythm": {"api_key": "synthetic-video-key"}},
            }
        }
    )
    empty = GatewayConfig.model_validate(
        {"video_generation": {"provider": "tokenrhythm", "primary": "wan3.0-video"}}
    )
    media.configure_video_generation(configured.video_generation, gateway_config=configured)
    try:
        assert media.video_generation_credential_status(
            configured, provider_id="tokenrhythm"
        )["available"] is True
        assert media.video_generation_credential_status(
            empty, provider_id="tokenrhythm"
        )["available"] is False
    finally:
        media.configure_video_generation(None)


def test_custom_video_endpoint_requires_an_authored_credential_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XAI_API_KEY", "official-only-key")
    monkeypatch.setenv("VIDEO_PROXY_API_KEY", "proxy-key")
    gateway = GatewayConfig.model_validate(
        {
            "video_generation": {
                "enabled": True,
                "provider": "xai",
                "primary": "grok-imagine-video-1.5",
                "providers": {"xai": {"base_url": "https://media-proxy.example/custom/v1"}},
            },
        }
    )
    media.configure_video_generation(gateway.video_generation, gateway_config=gateway)
    try:
        assert media.video_generation_available() is False
        gateway.video_generation.providers.xai.api_key_env = "VIDEO_PROXY_API_KEY"
        assert media.video_generation_available() is True
        _config, credential = media._video_request_config()
        assert credential.api_key == "proxy-key"
    finally:
        media.configure_video_generation(None)


def test_disabled_video_route_is_hidden_from_agent_tool_surface() -> None:
    ctx = ToolContext(is_owner=True, allowed_tools={"video_generate", "video_status"})
    resolved = resolve_runtime_tool_surface(
        ctx, capabilities=ToolSurfaceCapabilities(video_generation=False)
    )
    assert {"video_generate", "video_status"} <= resolved.denied_tools
    assert resolved.allowed_tools == set()


def test_video_tools_stay_hidden_in_unspecified_capability_snapshots() -> None:
    ctx = ToolContext(is_owner=True, allowed_tools={"video_generate", "video_status"})
    resolved = resolve_runtime_tool_surface(ctx)
    assert {"video_generate", "video_status"} <= resolved.denied_tools


def test_configured_video_tools_reach_the_agent_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-video-key")
    media.configure_video_generation(_video_config())
    try:
        capabilities = detect_runtime_tool_surface_capabilities()
        ctx = resolve_runtime_tool_surface(ToolContext(is_owner=True), capabilities=capabilities)
        names = {
            tool.name
            for tool in get_default_registry().to_model_tool_definitions(
                get_default_registry().to_tool_definitions(ctx), ctx
            )
        }
        assert {"video_generate", "video_status"} <= names
    finally:
        media.configure_video_generation(None)


def test_pending_job_keeps_status_visible_after_route_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    ctx = ToolContext(
        caller_kind=CallerKind.CHANNEL,
        session_key="channel:video-recovery",
        allowed_tools={"video_generate", "video_status"},
    )
    token = current_tool_context.set(ctx)
    try:
        media._remember_video_job(
            "video-recovery-job", "old-provider-key", "openrouter", "google/veo-3.1-fast"
        )
    finally:
        current_tool_context.reset(token)
    media.configure_video_generation(VideoGenerationConfig())
    try:
        capabilities = detect_runtime_tool_surface_capabilities()
        assert capabilities.video_generation is False
        assert capabilities.video_status is True
        resolved = resolve_runtime_tool_surface(ctx, capabilities=capabilities)
        assert "video_generate" in resolved.denied_tools
        assert "video_status" not in resolved.denied_tools
        assert resolved.allowed_tools == {"video_status"}

        foreign = resolve_runtime_tool_surface(
            ToolContext(
                caller_kind=CallerKind.CHANNEL,
                session_key="channel:other",
                allowed_tools={"video_generate", "video_status"},
            ),
            capabilities=capabilities,
        )
        assert "video_status" in foreign.denied_tools
    finally:
        with media._video_job_sessions_lock:
            media._video_job_sessions.pop("video-recovery-job", None)
        media.configure_video_generation(None)


@pytest.mark.asyncio
async def test_video_generate_registers_mp4_for_delivery_and_uses_selected_parameters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _video_config()
    monkeypatch.setattr(
        media,
        "_video_request_config",
        lambda: (config, SimpleNamespace(api_key="test-key", available=True)),
    )
    calls: list[dict[str, object]] = []

    async def fake_generate(**kwargs: object) -> video_generation.VideoGenerationResult:
        calls.append(kwargs)
        path = kwargs["output_path"]
        assert isinstance(path, Path)
        path.write_bytes(b"\x00\x00\x00\x0cftypisom")
        return video_generation.VideoGenerationResult(
            job_id="job-123", output_path=path, model=str(kwargs["model"]), bytes_written=12
        )

    monkeypatch.setattr(video_generation, "generate_openrouter_video", fake_generate)
    ctx = _context(tmp_path, artifacts=True)
    token = current_tool_context.set(ctx)
    try:
        response = json.loads(
            await media.video_generate(
                "A red kite rises over the sea",
                duration_seconds=8,
                aspect_ratio="9:16",
                resolution="1080p",
                filename="clips/kite.mp4",
            )
        )
    finally:
        current_tool_context.reset(token)

    assert len(calls) == 1
    assert calls[0]["duration"] == 8
    assert calls[0]["max_duration_seconds"] == config.max_duration_seconds
    assert calls[0]["aspect_ratio"] == "9:16"
    assert calls[0]["resolution"] == "1080p"
    assert calls[0]["max_bytes"] == config.max_output_bytes
    assert response["status"] == "ok"
    assert response["job_id"] == "job-123"
    assert response["path"] == str(tmp_path / "workspace" / "clips" / "kite.mp4")
    assert response["artifact"]["registered_for_delivery"] is True
    assert len(ctx.published_artifacts) == 1
    assert ctx.published_artifacts[0]["mime"] == "video/mp4"


@pytest.mark.asyncio
async def test_gemini_video_generation_dispatches_to_selected_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.provider import gemini_video_generation

    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    media.configure_video_generation(_gemini_config())
    calls: list[dict[str, object]] = []

    async def fake_gemini_generate(**kwargs: object) -> video_generation.VideoGenerationResult:
        calls.append(kwargs)
        path = kwargs["output_path"]
        assert isinstance(path, Path)
        path.write_bytes(b"\x00\x00\x00\x0cftypisom")
        return video_generation.VideoGenerationResult(
            job_id="operations/gemini-generate-123",
            output_path=path,
            model=str(kwargs["model"]),
            bytes_written=12,
            provider="gemini",
        )

    async def forbidden_openrouter_generate(**_kwargs: object) -> None:
        pytest.fail("A Gemini route must not submit to OpenRouter")

    monkeypatch.setattr(gemini_video_generation, "generate_gemini_video", fake_gemini_generate)
    monkeypatch.setattr(
        video_generation, "generate_openrouter_video", forbidden_openrouter_generate
    )
    token = current_tool_context.set(_context(tmp_path, artifacts=True))
    try:
        response = json.loads(await media.video_generate("A lantern drifting over a lake"))
    finally:
        current_tool_context.reset(token)
        media.configure_video_generation(None)

    assert len(calls) == 1
    assert calls[0]["base_url"] == "https://generativelanguage.googleapis.com/v1beta"
    assert calls[0]["api_key"] == "test-gemini-key"
    assert calls[0]["model"] == "veo-3.1-generate-preview"
    assert response["status"] == "ok"
    assert response["provider"] == "gemini"
    assert response["model"] == "veo-3.1-generate-preview"
    assert response["artifact"]["registered_for_delivery"] is True


@pytest.mark.asyncio
async def test_paid_video_keeps_job_and_file_receipt_when_artifact_storage_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        media,
        "_video_request_config",
        lambda: (_video_config(), SimpleNamespace(api_key="test-key", available=True)),
    )

    async def fake_generate(**kwargs: object) -> video_generation.VideoGenerationResult:
        path = kwargs["output_path"]
        assert isinstance(path, Path)
        path.write_bytes(b"\x00\x00\x00\x0cftypisom")
        return video_generation.VideoGenerationResult(
            job_id="paid-job-123", output_path=path, model=str(kwargs["model"]), bytes_written=12
        )

    def fail_publish(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(video_generation, "generate_openrouter_video", fake_generate)
    monkeypatch.setattr(media.ArtifactStore, "publish_file", fail_publish)
    ctx = _context(tmp_path, artifacts=True)
    token = current_tool_context.set(ctx)
    try:
        response = json.loads(await media.video_generate("A red kite rises over the sea"))
    finally:
        current_tool_context.reset(token)

    assert response["status"] == "generated_delivery_failed"
    assert response["job_id"] == "paid-job-123"
    assert Path(response["path"]).exists()
    assert "Do not generate it again" in response["note"]
    assert ctx.published_artifacts == []


@pytest.mark.asyncio
async def test_web_video_without_artifact_context_reports_delivery_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        media,
        "_video_request_config",
        lambda: (_video_config(), SimpleNamespace(api_key="test-key", available=True)),
    )

    async def fake_generate(**kwargs: object) -> video_generation.VideoGenerationResult:
        path = kwargs["output_path"]
        assert isinstance(path, Path)
        path.write_bytes(b"\x00\x00\x00\x0cftypisom")
        return video_generation.VideoGenerationResult(
            job_id="paid-job-456", output_path=path, model=str(kwargs["model"]), bytes_written=12
        )

    monkeypatch.setattr(video_generation, "generate_openrouter_video", fake_generate)
    token = current_tool_context.set(_context(tmp_path))
    try:
        response = json.loads(await media.video_generate("A red kite rises over the sea"))
    finally:
        current_tool_context.reset(token)

    assert response["status"] == "generated_delivery_failed"
    assert response["job_id"] == "paid-job-456"
    assert Path(response["path"]).exists()
    assert response["delivery_error"] == "Artifact delivery context is unavailable"


@pytest.mark.asyncio
async def test_video_generation_rejects_parameters_outside_operator_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _video_config()
    monkeypatch.setattr(
        media,
        "_video_request_config",
        lambda: (config, SimpleNamespace(api_key="test-key", available=True)),
    )

    async def forbidden_generate(**_kwargs: object) -> None:
        pytest.fail("The provider must not be called for an out-of-bounds request")

    monkeypatch.setattr(video_generation, "generate_openrouter_video", forbidden_generate)
    token = current_tool_context.set(_context(tmp_path))
    try:
        with pytest.raises(ToolError, match="duration_seconds"):
            await media.video_generate("A short clip", duration_seconds=9)
        with pytest.raises(ToolError, match="outside workspace"):
            await media.video_generate("A short clip", filename="../outside.mp4")
    finally:
        current_tool_context.reset(token)


@pytest.mark.asyncio
async def test_gemini_parameters_fail_before_video_submission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.provider import gemini_video_generation

    config = VideoGenerationConfig(
        enabled=True,
        provider="gemini",
        primary="veo-3.1-fast-generate-preview",
        max_duration_seconds=6,
    )
    monkeypatch.setattr(
        media, "_video_request_config", lambda: (config, SimpleNamespace(api_key="test-key"))
    )

    async def forbidden_generate(**_kwargs: object) -> None:
        pytest.fail("Unsupported Gemini parameters must fail before paid submission")

    monkeypatch.setattr(gemini_video_generation, "generate_gemini_video", forbidden_generate)
    token = current_tool_context.set(_context(tmp_path))
    try:
        with pytest.raises(ToolError, match="4, 6, or 8"):
            await media.video_generate("A city skyline", duration_seconds=5)
        with pytest.raises(ToolError, match="1080p"):
            await media.video_generate("A city skyline", resolution="1080p")
    finally:
        current_tool_context.reset(token)


@pytest.mark.parametrize(
    ("provider", "model", "cap"),
    [
        ("gemini", "veo-3.1-generate-preview", 8),
        ("xai", "grok-imagine-video-1.5", 15),
        ("qwen", "wan2.7-t2v", 15),
        ("qwen_token_plan", "wan2.7-t2v", 15),
        ("tokenrhythm", "wan3.0-video", 30),
    ],
)
def test_video_parameters_cap_agent_requests_without_rejecting_legacy_configured_limit(
    provider: str, model: str, cap: int
) -> None:
    config = VideoGenerationConfig(
        enabled=True, provider=provider, primary=model, max_duration_seconds=60
    )
    assert media._video_parameters(
        config, duration_seconds=None, aspect_ratio=None, resolution=None
    )[0] is None
    with pytest.raises(ToolError, match=f"between .* and {cap} for {provider}"):
        media._video_parameters(
            config, duration_seconds=cap + 1, aspect_ratio=None, resolution=None
        )


def test_xai_original_grok_model_rejects_1080p_request_override() -> None:
    config = VideoGenerationConfig(
        enabled=True, provider="xai", primary="grok-imagine-video"
    )
    with pytest.raises(ToolError, match="supports at most 720p"):
        media._video_parameters(
            config, duration_seconds=None, aspect_ratio=None, resolution="1080p"
        )


@pytest.mark.asyncio
async def test_video_tools_reject_workspace_write_denied_path_before_provider_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        media,
        "_video_request_config",
        lambda: (_video_config(), SimpleNamespace(api_key="test-key", available=True)),
    )

    async def forbidden_provider(**_kwargs: object) -> None:
        pytest.fail("A denied output path must never reach the provider")

    monkeypatch.setattr(video_generation, "generate_openrouter_video", forbidden_provider)
    monkeypatch.setattr(video_generation, "resume_openrouter_video", forbidden_provider)
    ctx = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.WEB,
        workspace_dir=str(tmp_path / "workspace"),
        workspace_write_deny_globs=["blocked/**"],
    )
    token = current_tool_context.set(ctx)
    try:
        with pytest.raises(ToolError, match="blocked"):
            await media.video_generate("A train crossing a bridge", filename="blocked/clip.mp4")
        with pytest.raises(ToolError, match="blocked"):
            await media.video_status("job-123", filename="blocked/clip.mp4")
    finally:
        current_tool_context.reset(token)


@pytest.mark.asyncio
async def test_subagents_cannot_run_paid_video_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def forbidden_provider(**_kwargs: object) -> None:
        pytest.fail("A subagent must never reach the video provider")

    monkeypatch.setattr(video_generation, "generate_openrouter_video", forbidden_provider)
    monkeypatch.setattr(video_generation, "resume_openrouter_video", forbidden_provider)
    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.SUBAGENT,
            workspace_dir=str(tmp_path / "workspace"),
        )
    )
    try:
        with pytest.raises(ToolError, match="subagents"):
            await media.video_generate("A train crossing a bridge")
        with pytest.raises(ToolError, match="subagents"):
            await media.video_status("job-123")
    finally:
        current_tool_context.reset(token)


@pytest.mark.asyncio
async def test_pending_generation_can_resume_without_a_second_submission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _video_config()
    monkeypatch.setattr(
        media,
        "_video_request_config",
        lambda: (config, SimpleNamespace(api_key="test-key", available=True)),
    )
    submit_count = 0
    resume_count = 0

    async def fake_generate(**_kwargs: object) -> None:
        nonlocal submit_count
        submit_count += 1
        raise video_generation.VideoGenerationPending("still processing", job_id="job-456")

    async def fake_resume(**kwargs: object) -> video_generation.VideoGenerationResult:
        nonlocal resume_count
        resume_count += 1
        assert kwargs["job_id"] == "job-456"
        path = kwargs["output_path"]
        assert isinstance(path, Path)
        path.write_bytes(b"\x00\x00\x00\x0cftypisom")
        return video_generation.VideoGenerationResult(
            job_id="job-456", output_path=path, model=str(kwargs["model"]), bytes_written=12
        )

    monkeypatch.setattr(video_generation, "generate_openrouter_video", fake_generate)
    monkeypatch.setattr(video_generation, "resume_openrouter_video", fake_resume)
    token = current_tool_context.set(_context(tmp_path, artifacts=True))
    try:
        pending = json.loads(await media.video_generate("A cloud moving over the city"))
        finished = json.loads(await media.video_status(pending["job_id"]))
    finally:
        current_tool_context.reset(token)

    assert pending["status"] == "pending"
    assert finished["status"] == "ok"
    assert submit_count == 1
    assert resume_count == 1


@pytest.mark.asyncio
async def test_completed_video_status_reuses_result_without_duplicate_delivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _video_config()
    monkeypatch.setattr(
        media,
        "_video_request_config",
        lambda: (config, SimpleNamespace(api_key="test-key", available=True)),
    )
    resume_count = 0

    async def fake_generate(**_kwargs: object) -> None:
        raise video_generation.VideoGenerationPending("still processing", job_id="cached-job-456")

    async def fake_resume(**kwargs: object) -> video_generation.VideoGenerationResult:
        nonlocal resume_count
        resume_count += 1
        await asyncio.sleep(0)
        path = kwargs["output_path"]
        assert isinstance(path, Path)
        path.write_bytes(b"\x00\x00\x00\x0cftypisom")
        return video_generation.VideoGenerationResult(
            job_id="cached-job-456", output_path=path, model=str(kwargs["model"]),
            bytes_written=12,
        )

    monkeypatch.setattr(video_generation, "generate_openrouter_video", fake_generate)
    monkeypatch.setattr(video_generation, "resume_openrouter_video", fake_resume)
    creator = ToolContext(
        caller_kind=CallerKind.CHANNEL,
        workspace_dir=str(tmp_path / "workspace"),
        artifact_media_root=str(tmp_path / "artifacts"),
        artifact_session_id="cached-video-session",
        session_key="channel:cached-video-creator",
    )
    foreign = ToolContext(
        caller_kind=CallerKind.CHANNEL,
        workspace_dir=str(tmp_path / "workspace"),
        session_key="channel:cached-video-foreign",
    )
    token = current_tool_context.set(creator)
    job_id = ""
    try:
        pending = json.loads(await media.video_generate("A cloud moving over the city"))
        job_id = pending["job_id"]
        first, second = await asyncio.gather(
            media.video_status(job_id), media.video_status(job_id)
        )
        third = await media.video_status(job_id)
        assert json.loads(first) == json.loads(second) == json.loads(third)
        assert json.loads(first)["status"] == "ok"
        assert resume_count == 1
        assert len(creator.published_artifacts) == 1
        assert len(list((tmp_path / "workspace").glob("*.mp4"))) == 1

        current_tool_context.reset(token)
        token = current_tool_context.set(foreign)
        with pytest.raises(ToolError, match="unavailable in this session"):
            await media.video_status(job_id)
        assert resume_count == 1
    finally:
        current_tool_context.reset(token)
        if job_id:
            with media._video_job_sessions_lock:
                media._video_job_sessions.pop(job_id, None)


@pytest.mark.asyncio
async def test_unknown_submission_does_not_report_generation_as_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        media,
        "_video_request_config",
        lambda: (_video_config(), SimpleNamespace(api_key="test-key", available=True)),
    )

    async def fake_generate(**_kwargs: object) -> None:
        raise video_generation.VideoGenerationSubmissionUnknown()

    monkeypatch.setattr(video_generation, "generate_openrouter_video", fake_generate)
    token = current_tool_context.set(_context(tmp_path))
    try:
        response = json.loads(await media.video_generate("A train crossing a bridge"))
    finally:
        current_tool_context.reset(token)

    assert response["status"] == "submission_unknown"
    assert "do not resubmit" in response["note"]


@pytest.mark.asyncio
async def test_accepted_failed_job_keeps_its_id_in_tool_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        media,
        "_video_request_config",
        lambda: (_video_config(), SimpleNamespace(api_key="test-key", available=True)),
    )

    async def fake_generate(**_kwargs: object) -> None:
        raise video_generation.VideoGenerationError(
            "Provider rejected the finished clip", job_id="failed-job-123"
        )

    monkeypatch.setattr(video_generation, "generate_openrouter_video", fake_generate)
    token = current_tool_context.set(_context(tmp_path))
    try:
        response = json.loads(await media.video_generate("A bridge in rain"))
    finally:
        current_tool_context.reset(token)

    assert response["status"] == "failed"
    assert response["job_id"] == "failed-job-123"
    assert "Do not generate it again" in response["note"]


@pytest.mark.asyncio
async def test_channel_cannot_resume_another_sessions_video_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        media,
        "_video_request_config",
        lambda: (_video_config(), SimpleNamespace(api_key="test-key", available=True)),
    )

    async def fake_generate(**_kwargs: object) -> None:
        raise video_generation.VideoGenerationPending("still processing", job_id="private-job-789")

    resume_calls: list[dict[str, object]] = []

    async def fake_resume(**kwargs: object) -> video_generation.VideoGenerationResult:
        resume_calls.append(kwargs)
        path = kwargs["output_path"]
        assert isinstance(path, Path)
        path.write_bytes(b"\x00\x00\x00\x0cftypisom")
        return video_generation.VideoGenerationResult(
            job_id="private-job-789",
            output_path=path,
            model=str(kwargs["model"]),
            bytes_written=12,
        )

    monkeypatch.setattr(video_generation, "generate_openrouter_video", fake_generate)
    monkeypatch.setattr(video_generation, "resume_openrouter_video", fake_resume)
    creator = ToolContext(
        caller_kind=CallerKind.CHANNEL,
        workspace_dir=str(tmp_path / "workspace"),
        artifact_media_root=str(tmp_path / "artifacts"),
        artifact_session_id="video-channel-session",
        session_key="channel:creator",
    )
    foreign = ToolContext(
        caller_kind=CallerKind.CHANNEL,
        workspace_dir=str(tmp_path / "workspace"),
        session_key="channel:foreign",
    )
    token = current_tool_context.set(creator)
    try:
        pending = json.loads(await media.video_generate("A river at sunset"))
    finally:
        current_tool_context.reset(token)
    assert resume_calls == []

    changed_model = VideoGenerationConfig(enabled=True, primary="minimax/hailuo-3")
    monkeypatch.setattr(
        media,
        "_video_request_config",
        lambda: (changed_model, SimpleNamespace(api_key="test-key", available=True)),
    )
    token = current_tool_context.set(creator)
    try:
        finished = json.loads(await media.video_status("private-job-789"))
    finally:
        current_tool_context.reset(token)
    assert finished["status"] == "ok"
    assert finished["model"] == "google/veo-3.1-fast"
    assert len(resume_calls) == 1
    assert pending["job_id"] == "private-job-789"

    token = current_tool_context.set(foreign)
    try:
        with pytest.raises(ToolError, match="unavailable in this session"):
            await media.video_status("private-job-789")
    finally:
        current_tool_context.reset(token)
    assert len(resume_calls) == 1

    monkeypatch.setattr(
        media,
        "_video_request_config",
        lambda: (_video_config(), SimpleNamespace(api_key="rotated-key", available=True)),
    )
    token = current_tool_context.set(creator)
    try:
        with pytest.raises(ToolError, match="unavailable in this session"):
            await media.video_status("private-job-789")
    finally:
        current_tool_context.reset(token)


@pytest.mark.asyncio
async def test_gemini_job_keeps_its_provider_model_and_session_after_route_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.provider import gemini_video_generation

    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    media.configure_video_generation(_gemini_config())
    submit_count = 0
    resume_calls: list[dict[str, object]] = []

    async def fake_gemini_generate(**_kwargs: object) -> None:
        nonlocal submit_count
        submit_count += 1
        raise video_generation.VideoGenerationPending(
            "still processing", job_id="operations/private-gemini-123"
        )

    async def fake_gemini_resume(
        **kwargs: object,
    ) -> video_generation.VideoGenerationResult:
        resume_calls.append(kwargs)
        path = kwargs["output_path"]
        assert isinstance(path, Path)
        path.write_bytes(b"\x00\x00\x00\x0cftypisom")
        return video_generation.VideoGenerationResult(
            job_id="operations/private-gemini-123",
            output_path=path,
            model=str(kwargs["model"]),
            bytes_written=12,
            provider="gemini",
        )

    async def forbidden_openrouter(**_kwargs: object) -> None:
        pytest.fail("An accepted Gemini job must not be sent to OpenRouter")

    monkeypatch.setattr(gemini_video_generation, "generate_gemini_video", fake_gemini_generate)
    monkeypatch.setattr(gemini_video_generation, "resume_gemini_video", fake_gemini_resume)
    monkeypatch.setattr(video_generation, "generate_openrouter_video", forbidden_openrouter)
    monkeypatch.setattr(video_generation, "resume_openrouter_video", forbidden_openrouter)
    creator = ToolContext(
        caller_kind=CallerKind.CHANNEL,
        workspace_dir=str(tmp_path / "workspace"),
        artifact_media_root=str(tmp_path / "artifacts"),
        artifact_session_id="gemini-channel-session",
        session_key="channel:gemini-creator",
    )
    foreign = ToolContext(
        caller_kind=CallerKind.CHANNEL,
        workspace_dir=str(tmp_path / "workspace"),
        session_key="channel:gemini-foreign",
    )

    token = current_tool_context.set(creator)
    try:
        pending = json.loads(await media.video_generate("A comet crossing the night sky"))
        media.configure_video_generation(_video_config())
        finished = json.loads(await media.video_status(pending["job_id"]))
    finally:
        current_tool_context.reset(token)
        media.configure_video_generation(None)

    assert pending["provider"] == "gemini"
    assert finished["status"] == "ok"
    assert finished["provider"] == "gemini"
    assert finished["model"] == "veo-3.1-generate-preview"
    assert submit_count == 1
    assert len(resume_calls) == 1
    assert resume_calls[0]["job_id"] == pending["job_id"]
    assert resume_calls[0]["model"] == "veo-3.1-generate-preview"
    assert resume_calls[0]["api_key"] == "test-gemini-key"

    media.configure_video_generation(_video_config())
    try:
        token = current_tool_context.set(foreign)
        try:
            with pytest.raises(ToolError, match="unavailable in this session"):
                await media.video_status(pending["job_id"])
        finally:
            current_tool_context.reset(token)

        monkeypatch.setenv("GEMINI_API_KEY", "rotated-gemini-key")
        token = current_tool_context.set(creator)
        try:
            with pytest.raises(ToolError, match="unavailable in this session"):
                await media.video_status(pending["job_id"])
        finally:
            current_tool_context.reset(token)
    finally:
        media.configure_video_generation(None)
    assert len(resume_calls) == 1


@pytest.mark.asyncio
async def test_custom_video_job_resumes_at_its_original_endpoint_after_provider_switch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.provider import xai_video_generation

    endpoint = "https://media-proxy.example/custom/v1"
    monkeypatch.setenv("VIDEO_PROXY_API_KEY", "proxy-key")
    monkeypatch.setenv("OTHER_VIDEO_PROXY_API_KEY", "other-proxy-key")
    config = VideoGenerationConfig(
        enabled=True,
        provider="xai",
        primary="grok-imagine-video-1.5",
        providers={"xai": {"base_url": endpoint, "api_key_env": "VIDEO_PROXY_API_KEY"}},
    )
    media.configure_video_generation(config)
    calls: list[dict[str, object]] = []

    async def fake_generate(**kwargs: object) -> None:
        calls.append(kwargs)
        raise video_generation.VideoGenerationPending("waiting", job_id="xai-private-job")

    async def fake_resume(**kwargs: object) -> video_generation.VideoGenerationResult:
        calls.append(kwargs)
        path = kwargs["output_path"]
        assert isinstance(path, Path)
        path.write_bytes(b"\x00\x00\x00\x0cftypisom")
        return video_generation.VideoGenerationResult(
            job_id="xai-private-job",
            output_path=path,
            model=str(kwargs["model"]),
            bytes_written=12,
            provider="xai",
        )

    monkeypatch.setattr(xai_video_generation, "generate_xai_video", fake_generate)
    monkeypatch.setattr(xai_video_generation, "resume_xai_video", fake_resume)
    token = current_tool_context.set(_context(tmp_path, artifacts=True))
    try:
        pending = json.loads(await media.video_generate("A wave crossing the shore"))
        media.configure_video_generation(
            VideoGenerationConfig(
                enabled=True,
                provider="xai",
                primary="grok-imagine-video-1.5-lite",
                providers={
                    "xai": {
                        "base_url": "https://other-proxy.example/custom/v1",
                        "api_key_env": "OTHER_VIDEO_PROXY_API_KEY",
                    }
                },
            )
        )
        finished = json.loads(await media.video_status(pending["job_id"]))
        monkeypatch.setenv("VIDEO_PROXY_API_KEY", "rotated-key")
        with pytest.raises(ToolError, match="unavailable in this session"):
            await media.video_status(pending["job_id"])
    finally:
        current_tool_context.reset(token)
        media.configure_video_generation(None)
    assert pending["status"] == "pending"
    assert finished["provider"] == "xai"
    assert [call["base_url"] for call in calls] == [endpoint, endpoint]
    assert [call["api_key"] for call in calls] == ["proxy-key", "proxy-key"]


@pytest.mark.asyncio
async def test_identical_provider_job_ids_keep_distinct_local_handles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.provider import gemini_video_generation

    native_job_id = "shared-native-video-job"
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-key")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
    calls: list[tuple[str, str]] = []

    async def fake_openrouter_generate(**_kwargs: object) -> None:
        raise video_generation.VideoGenerationPending("waiting", job_id=native_job_id)

    async def fake_gemini_generate(**_kwargs: object) -> None:
        raise video_generation.VideoGenerationPending("waiting", job_id=native_job_id)

    async def fake_openrouter_resume(**kwargs: object) -> None:
        calls.append(("openrouter", str(kwargs["job_id"])))
        raise video_generation.VideoGenerationPending("waiting", job_id=native_job_id)

    async def fake_gemini_resume(**kwargs: object) -> None:
        calls.append(("gemini", str(kwargs["job_id"])))
        raise video_generation.VideoGenerationPending("waiting", job_id=native_job_id)

    monkeypatch.setattr(video_generation, "generate_openrouter_video", fake_openrouter_generate)
    monkeypatch.setattr(video_generation, "resume_openrouter_video", fake_openrouter_resume)
    monkeypatch.setattr(gemini_video_generation, "generate_gemini_video", fake_gemini_generate)
    monkeypatch.setattr(gemini_video_generation, "resume_gemini_video", fake_gemini_resume)
    first = ToolContext(
        caller_kind=CallerKind.CHANNEL,
        session_key="channel:video-collision-openrouter",
        workspace_dir=str(tmp_path / "first"),
    )
    second = ToolContext(
        caller_kind=CallerKind.CHANNEL,
        session_key="channel:video-collision-gemini",
        workspace_dir=str(tmp_path / "second"),
    )
    handles: list[str] = []
    try:
        media.configure_video_generation(_video_config())
        token = current_tool_context.set(first)
        try:
            first_pending = json.loads(await media.video_generate("First clip"))
        finally:
            current_tool_context.reset(token)
        handles.append(first_pending["job_id"])

        media.configure_video_generation(_gemini_config())
        token = current_tool_context.set(second)
        try:
            second_pending = json.loads(await media.video_generate("Second clip"))
            second_status = json.loads(await media.video_status(second_pending["job_id"]))
        finally:
            current_tool_context.reset(token)
        handles.append(second_pending["job_id"])

        token = current_tool_context.set(first)
        try:
            first_status = json.loads(await media.video_status(first_pending["job_id"]))
        finally:
            current_tool_context.reset(token)

        assert first_pending["job_id"] == native_job_id
        assert second_pending["job_id"] != native_job_id
        assert first_status["job_id"] == first_pending["job_id"]
        assert second_status["job_id"] == second_pending["job_id"]
        assert calls == [("gemini", native_job_id), ("openrouter", native_job_id)]
    finally:
        with media._video_job_sessions_lock:
            for handle in handles:
                media._video_job_sessions.pop(handle, None)
        media.configure_video_generation(None)
