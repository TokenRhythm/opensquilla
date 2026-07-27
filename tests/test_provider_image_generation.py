from __future__ import annotations

import base64
import io
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from opensquilla.provider.image_generation import (
    ImageGenerationRequest,
    ImageGenerationResult,
    OpenRouterImageGenerationProvider,
    get_image_generation_provider,
)


def _test_png_bytes() -> bytes:
    buffer = io.BytesIO()
    with Image.new("RGBA", (2, 1), (30, 120, 210, 128)) as image:
        image.save(buffer, format="PNG")
    return buffer.getvalue()


def _clear_vision_provider_env(monkeypatch) -> None:
    for name in (
        "OPENSQUILLA_VISION_PROVIDER",
        "OPENSQUILLA_VISION_MODEL",
        "OPENSQUILLA_LLM_PROVIDER",
        "OPENSQUILLA_LLM_MODEL",
        "OPENSQUILLA_LLM_PROXY",
        "OPENROUTER_API_KEY",
        "OPENROUTER_BASE_URL",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.asyncio
async def test_openrouter_image_provider_adds_app_attribution_headers(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "choices": [
                    {
                        "message": {
                            "images": [
                                {
                                    "type": "image_url",
                                    "image_url": {"url": "data:image/png;base64,b3BlbnNxdWlsbGE="},
                                }
                            ]
                        }
                    }
                ]
            }

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

        async def post(self, url, *, headers, json):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return FakeResponse()

    monkeypatch.setattr(
        "opensquilla.provider.image_generation.httpx.AsyncClient",
        lambda **kwargs: FakeClient(),
    )

    provider = OpenRouterImageGenerationProvider(api_key="or-test")
    result = await provider.generate(
        ImageGenerationRequest(
            prompt="draw a squid",
            model="google/gemini-3.1-flash-image-preview",
            size="1536x1024",
            output_format="png",
            timeout_seconds=10.0,
        )
    )

    assert captured["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert captured["headers"] == {
        "Authorization": "Bearer or-test",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://opensquilla.ai",
        "X-Title": "OpenSquilla",
    }
    assert result.image_bytes == b"opensquilla"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("candidate", "wire_model"),
    [
        (
            "openrouter/google/gemini-3.1-flash-image-preview",
            "google/gemini-3.1-flash-image-preview",
        ),
        ("openrouter/auto", "openrouter/auto"),
        ("openrouter/openrouter/auto", "openrouter/auto"),
    ],
)
async def test_openrouter_model_ref_keeps_provider_for_routing_but_not_wire_model(
    monkeypatch,
    candidate,
    wire_model,
) -> None:
    from opensquilla.provider import image_generation

    captured: dict[str, object] = {}
    encoded_image = base64.b64encode(_test_png_bytes()).decode("ascii")

    class FakeResponse:
        text = ""

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "choices": [
                    {
                        "message": {
                            "images": [
                                {
                                    "image_url": {
                                        "url": f"data:image/png;base64,{encoded_image}"
                                    }
                                }
                            ]
                        }
                    }
                ]
            }

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

        async def post(self, url, *, headers, json):
            captured["url"] = url
            captured["json"] = json
            return FakeResponse()

    monkeypatch.setattr(
        image_generation.httpx,
        "AsyncClient",
        lambda **kwargs: FakeClient(),
    )
    provider = OpenRouterImageGenerationProvider(api_key="synthetic-openrouter-key")
    image_generation.register_image_generation_provider(provider)
    try:
        result = await image_generation.generate_with_fallbacks(
            request=ImageGenerationRequest(
                prompt="draw a squid",
                model=candidate,
                size="1024x1024",
            ),
            candidates=[candidate],
        )
    finally:
        image_generation.reset_image_generation_providers()

    assert captured["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert captured["json"]["model"] == wire_model
    assert result.provider == "openrouter"
    assert result.model == wire_model
    assert result.mime_type == "image/png"


@pytest.mark.asyncio
async def test_image_provider_rejects_foreign_official_endpoint_before_http(
    monkeypatch,
) -> None:
    client_constructed = False

    def fail_if_http_client_is_constructed(**_kwargs):
        nonlocal client_constructed
        client_constructed = True
        raise AssertionError("HTTP client must not be constructed")

    monkeypatch.setattr(
        "opensquilla.provider.image_generation.httpx.AsyncClient",
        fail_if_http_client_is_constructed,
    )
    provider = OpenRouterImageGenerationProvider(
        api_key="synthetic-openrouter-key",
        base_url="https://api.openai.com/v1",
    )

    with pytest.raises(RuntimeError, match="cannot use 'openai'.*official endpoint"):
        await provider.generate(
            ImageGenerationRequest(
                prompt="draw a squid",
                model="google/gemini-3.1-flash-image-preview",
                size="1024x1024",
            )
        )

    assert client_constructed is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "base_url",
    [
        " https://openrouter.ai/api/v1 ",
        "https://openrouter.ai:invalid/v1",
        "https://openrouter.ai/api/v1?tenant=test",
    ],
)
async def test_image_provider_rejects_invalid_endpoint_before_http(
    monkeypatch,
    base_url,
) -> None:
    client_constructed = False

    def fail_if_http_client_is_constructed(**_kwargs):
        nonlocal client_constructed
        client_constructed = True
        raise AssertionError("HTTP client must not be constructed")

    monkeypatch.setattr(
        "opensquilla.provider.image_generation.httpx.AsyncClient",
        fail_if_http_client_is_constructed,
    )
    provider = OpenRouterImageGenerationProvider(
        api_key="synthetic-openrouter-key",
        base_url=base_url,
    )

    with pytest.raises(RuntimeError, match="invalid endpoint"):
        await provider.generate(
            ImageGenerationRequest(
                prompt="draw a squid",
                model="google/gemini-3.1-flash-image-preview",
                size="1024x1024",
            )
        )

    assert client_constructed is False


def test_image_generation_availability_rejects_foreign_official_endpoint() -> None:
    from opensquilla.gateway.config import ImageGenerationConfig
    from opensquilla.tools.builtin.media import (
        configure_image_generation,
        image_generation_available,
    )

    config = ImageGenerationConfig(
        enabled=True,
        primary="openrouter/google/gemini-3.1-flash-image-preview",
    )
    config.providers.openrouter.base_url = "https://api.openai.com/v1"
    config.providers.openrouter.api_key = "synthetic-openrouter-key"
    configure_image_generation(config)
    try:
        assert image_generation_available() is False
    finally:
        configure_image_generation(None)


def test_image_generation_availability_allows_endpointless_registered_provider() -> None:
    from opensquilla.gateway.config import ImageGenerationConfig
    from opensquilla.provider import image_generation
    from opensquilla.tools.builtin.media import (
        configure_image_generation,
        image_generation_available,
    )

    class FakeProvider:
        provider_id = "synthetic"
        default_model = "image-model"
        auth_env_vars: tuple[str, ...] = ()

        async def generate(self, request: ImageGenerationRequest) -> ImageGenerationResult:
            return ImageGenerationResult(
                image_bytes=_test_png_bytes(),
                mime_type="image/png",
                model=request.model,
                provider=self.provider_id,
            )

    configure_image_generation(
        ImageGenerationConfig(enabled=True, primary="synthetic/image-model")
    )
    image_generation.register_image_generation_provider(FakeProvider())
    try:
        assert image_generation_available()
    finally:
        configure_image_generation(None)


@pytest.mark.asyncio
async def test_image_provider_sends_correlation_only_for_explicit_tokenrhythm_origin(
    monkeypatch,
) -> None:
    from opensquilla.provider.tokenrhythm_correlation import (
        TOKENRHYTHM_CALL_KIND_HEADER,
        TOKENRHYTHM_EXECUTION_ID_HEADER,
        TOKENRHYTHM_SESSION_ID_HEADER,
        TOKENRHYTHM_TURN_ID_HEADER,
    )
    from opensquilla.provider.types import ProviderRequestCorrelation

    captured_headers: list[dict[str, str]] = []

    class FakeResponse:
        text = ""

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "choices": [
                    {
                        "message": {
                            "images": [
                                {
                                    "image_url": {
                                        "url": "data:image/png;base64,b3BlbnNxdWlsbGE="
                                    }
                                }
                            ]
                        }
                    }
                ]
            }

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

        async def post(self, _url, *, headers, json):
            captured_headers.append(headers)
            return FakeResponse()

    monkeypatch.setattr(
        "opensquilla.provider.image_generation.httpx.AsyncClient",
        lambda **kwargs: FakeClient(),
    )
    correlation = ProviderRequestCorrelation(
        session_id="session-1",
        turn_id="turn-1",
        execution_id="image-execution-1",
        call_kind="auxiliary.image_generation",
    )
    request = ImageGenerationRequest(
        prompt="draw a squid",
        model="image-model",
        size="1024x1024",
        provider_request_correlation=correlation,
    )

    trusted = OpenRouterImageGenerationProvider(
        api_key="synthetic-token",
        base_url="https://api.tokenrhythm.studio/v1",
        provider_kind="tokenrhythm",
    )
    custom = OpenRouterImageGenerationProvider(
        api_key="synthetic-token",
        base_url="https://custom.example/v1",
        provider_kind="tokenrhythm",
    )
    untyped_official_origin = OpenRouterImageGenerationProvider(
        api_key="synthetic-token",
        base_url="https://api.tokenrhythm.studio/v1",
    )
    await trusted.generate(request)
    await custom.generate(request)
    await untyped_official_origin.generate(request)

    assert captured_headers[0][TOKENRHYTHM_SESSION_ID_HEADER] == "session-1"
    assert captured_headers[0][TOKENRHYTHM_TURN_ID_HEADER] == "turn-1"
    assert captured_headers[0][TOKENRHYTHM_EXECUTION_ID_HEADER] == "image-execution-1"
    assert (
        captured_headers[0][TOKENRHYTHM_CALL_KIND_HEADER]
        == "auxiliary.image_generation"
    )
    assert TOKENRHYTHM_SESSION_ID_HEADER not in captured_headers[1]
    assert TOKENRHYTHM_SESSION_ID_HEADER not in captured_headers[2]


@pytest.mark.asyncio
async def test_image_generation_fallback_operation_derives_one_correlation() -> None:
    from opensquilla.provider import image_generation
    from opensquilla.provider.correlation_context import (
        current_provider_request_correlation,
    )
    from opensquilla.provider.types import ProviderRequestCorrelation

    root = ProviderRequestCorrelation(
        session_id="session-1",
        turn_id="turn-1",
        execution_id="root-execution",
        call_kind="agent.chat",
    )
    captured: list[tuple[ImageGenerationRequest, object]] = []

    class FakeProvider:
        provider_id = "fake"
        default_model = "image-model"
        auth_env_vars: tuple[str, ...] = ()

        async def generate(
            self,
            request: ImageGenerationRequest,
        ) -> ImageGenerationResult:
            captured.append((request, current_provider_request_correlation()))
            if len(captured) == 1:
                raise RuntimeError("first candidate failed")
            return ImageGenerationResult(
                image_bytes=_test_png_bytes(),
                mime_type="image/png",
                model=request.model,
                provider=self.provider_id,
            )

    image_generation.register_image_generation_provider(FakeProvider())
    try:
        await image_generation.generate_with_fallbacks(
            request=ImageGenerationRequest(
                prompt="draw a squid",
                model="fake/image-model-1",
                size="1024x1024",
                provider_request_correlation=root,
            ),
            candidates=["fake/image-model-1", "fake/image-model-2"],
        )
    finally:
        image_generation.reset_image_generation_providers()

    request_correlation = captured[0][0].provider_request_correlation
    assert request_correlation == captured[0][1]
    assert "session-1" not in repr(captured[0][0])
    assert request_correlation is not None
    assert request_correlation.session_id == root.session_id
    assert request_correlation.turn_id == root.turn_id
    assert request_correlation.execution_id != root.execution_id
    assert request_correlation.call_kind == "auxiliary.image_generation"
    fallback_correlation = captured[1][0].provider_request_correlation
    assert fallback_correlation == captured[1][1]
    assert fallback_correlation.session_id == request_correlation.session_id
    assert fallback_correlation.turn_id == request_correlation.turn_id
    assert fallback_correlation.execution_id == request_correlation.execution_id
    assert (
        fallback_correlation.call_kind
        == "auxiliary.image_generation.provider_fallback"
    )


@pytest.mark.asyncio
async def test_invalid_provider_image_triggers_fallback_and_requested_format_conversion() -> None:
    from opensquilla.provider import image_generation

    generated_models: list[str] = []

    class FakeProvider:
        provider_id = "synthetic"
        default_model = "image-model"
        auth_env_vars: tuple[str, ...] = ()

        async def generate(
            self,
            request: ImageGenerationRequest,
        ) -> ImageGenerationResult:
            generated_models.append(request.model)
            image_bytes = b"not-an-image" if request.model == "broken" else _test_png_bytes()
            return ImageGenerationResult(
                image_bytes=image_bytes,
                mime_type="image/png",
                model=request.model,
                provider=self.provider_id,
            )

    image_generation.register_image_generation_provider(FakeProvider())
    try:
        result = await image_generation.generate_with_fallbacks(
            request=ImageGenerationRequest(
                prompt="draw a squid",
                model="synthetic/broken",
                size="1024x1024",
                output_format="webp",
            ),
            candidates=["synthetic/broken", "synthetic/working"],
        )
    finally:
        image_generation.reset_image_generation_providers()

    assert generated_models == ["broken", "working"]
    assert len(result.attempts) == 1
    assert "invalid image bytes" in result.attempts[0].error
    assert result.mime_type == "image/webp"
    with Image.open(io.BytesIO(result.image_bytes)) as image:
        assert image.format == "WEBP"


@pytest.mark.parametrize(
    "call_kind",
    [
        "auxiliary.image_generation",
        "auxiliary.image_generation.provider_fallback",
    ],
)
def test_image_generation_recognizes_existing_operation_call_kinds(
    call_kind: str,
) -> None:
    from opensquilla.provider.image_generation import (
        _is_image_generation_correlation,
    )
    from opensquilla.provider.types import ProviderRequestCorrelation

    assert _is_image_generation_correlation(
        ProviderRequestCorrelation(
            session_id="session-1",
            turn_id="turn-1",
            execution_id="image-execution",
            call_kind=call_kind,
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("caller_kind", ["web", "channel"])
async def test_image_generate_auto_publishes_generated_image_artifact_for_surfaces(
    monkeypatch, tmp_path, caller_kind
) -> None:
    from opensquilla.gateway.config import ImageGenerationConfig
    from opensquilla.tools.builtin import media
    from opensquilla.tools.types import CallerKind, ToolContext, current_tool_context

    async def fake_generate_with_fallbacks(**_kwargs):
        return ImageGenerationResult(
            image_bytes=b"fake-png",
            mime_type="image/png",
            model="google/gemini-3.1-flash-image-preview",
            provider="openrouter",
        )

    monkeypatch.setattr(media, "generate_with_fallbacks", fake_generate_with_fallbacks)
    config = ImageGenerationConfig(
        enabled=True,
        primary="openrouter/google/gemini-3.1-flash-image-preview",
    )
    config.providers.openrouter.api_key = "sk-or-test"
    media.configure_image_generation(config)

    ctx = ToolContext(
        caller_kind=CallerKind(caller_kind),
        workspace_dir=str(tmp_path / "workspace"),
        artifact_media_root=str(tmp_path / "media"),
        artifact_session_id="session-1",
        session_key=f"agent:main:{caller_kind}:test",
    )
    token = current_tool_context.set(ctx)
    try:
        payload = await media.image_generate(
            prompt="draw an elephant",
            filename="Elephant.png",
        )
    finally:
        current_tool_context.reset(token)
        media.configure_image_generation(None)

    result = __import__("json").loads(payload)
    assert result["status"] == "ok"
    assert result["path"].endswith("Elephant.png")
    assert result["artifact"]["name"] == "Elephant.png"
    assert result["artifact"]["mime"] == "image/png"
    assert result["artifact"]["registered_for_delivery"] is True
    assert result["artifact"]["delivery_managed_by_surface"] is True
    assert "download_url" not in result["artifact"]
    assert "registered for the current chat surface" in result["note"]
    assert "Do not call publish_artifact" in result["note"]
    assert len(ctx.published_artifacts) == 1
    published = ctx.published_artifacts[0]
    assert published["name"] == "Elephant.png"
    assert published["mime"] == "image/png"
    assert published["download_url"] == f"/api/v1/artifacts/{published['id']}"


@pytest.mark.asyncio
async def test_image_generate_does_not_auto_publish_artifact_for_subagent(
    monkeypatch, tmp_path
) -> None:
    from opensquilla.gateway.config import ImageGenerationConfig
    from opensquilla.tools.builtin import media
    from opensquilla.tools.types import CallerKind, ToolContext, current_tool_context

    async def fake_generate_with_fallbacks(**_kwargs):
        return ImageGenerationResult(
            image_bytes=b"fake-png",
            mime_type="image/png",
            model="google/gemini-3.1-flash-image-preview",
            provider="openrouter",
        )

    monkeypatch.setattr(media, "generate_with_fallbacks", fake_generate_with_fallbacks)
    config = ImageGenerationConfig(
        enabled=True,
        primary="openrouter/google/gemini-3.1-flash-image-preview",
    )
    config.providers.openrouter.api_key = "sk-or-test"
    media.configure_image_generation(config)

    ctx = ToolContext(
        caller_kind=CallerKind.SUBAGENT,
        workspace_dir=str(tmp_path / "workspace"),
        artifact_media_root=str(tmp_path / "media"),
        artifact_session_id="session-1",
        session_key="agent:main:subagent:test",
    )
    token = current_tool_context.set(ctx)
    try:
        payload = await media.image_generate(
            prompt="draw an elephant",
            filename="Elephant.png",
        )
    finally:
        current_tool_context.reset(token)
        media.configure_image_generation(None)

    result = __import__("json").loads(payload)
    assert result["status"] == "ok"
    assert "artifact" not in result
    assert ctx.published_artifacts == []


@pytest.mark.asyncio
async def test_image_generate_uses_configured_size_and_matching_output_suffix(
    tmp_path,
) -> None:
    from opensquilla.gateway.config import ImageGenerationConfig
    from opensquilla.provider import image_generation
    from opensquilla.tools.builtin import media
    from opensquilla.tools.types import ToolContext, current_tool_context

    captured_requests: list[ImageGenerationRequest] = []

    class FakeProvider:
        provider_id = "synthetic"
        default_model = "image-model"
        auth_env_vars: tuple[str, ...] = ()

        async def generate(
            self,
            request: ImageGenerationRequest,
        ) -> ImageGenerationResult:
            captured_requests.append(request)
            return ImageGenerationResult(
                image_bytes=_test_png_bytes(),
                mime_type="image/png",
                model=request.model,
                provider=self.provider_id,
            )

    config = ImageGenerationConfig(
        enabled=True,
        primary="synthetic/image-model",
        size="1536x1024",
        output_format="webp",
    )
    media.configure_image_generation(config)
    image_generation.register_image_generation_provider(FakeProvider())
    token = current_tool_context.set(ToolContext(workspace_dir=str(tmp_path)))
    try:
        payload = json.loads(
            await media.image_generate(
                prompt="draw a squid",
                filename="configured-format.png",
            )
        )
    finally:
        current_tool_context.reset(token)
        media.configure_image_generation(None)

    assert len(captured_requests) == 1
    assert captured_requests[0].size == "1536x1024"
    assert captured_requests[0].output_format == "webp"
    assert payload["path"].endswith("configured-format.webp")
    assert payload["mime_type"] == "image/webp"
    with Image.open(payload["path"]) as image:
        assert image.format == "WEBP"


@pytest.mark.asyncio
async def test_image_generate_rejects_foreign_posix_filename_on_windows(
    monkeypatch,
    tmp_path,
) -> None:
    from opensquilla.gateway.config import ImageGenerationConfig
    from opensquilla.tools.builtin import media
    from opensquilla.tools.types import CallerKind, ToolContext, ToolError, current_tool_context

    monkeypatch.setattr(media.os, "name", "nt")
    config = ImageGenerationConfig(
        enabled=True,
        primary="openrouter/google/gemini-3.1-flash-image-preview",
    )
    config.providers.openrouter.api_key = "sk-or-test"
    media.configure_image_generation(config)

    ctx = ToolContext(
        caller_kind=CallerKind.WEB,
        workspace_dir=str(tmp_path / "workspace"),
        artifact_media_root=str(tmp_path / "media"),
        artifact_session_id="session-1",
        session_key="agent:main:web:test",
    )
    token = current_tool_context.set(ctx)
    try:
        with pytest.raises(ToolError, match="foreign_host_path"):
            await media.image_generate(
                prompt="draw an elephant",
                filename="/Users/a1/Desktop/Elephant.png",
            )
    finally:
        current_tool_context.reset(token)
        media.configure_image_generation(None)


def test_image_generation_reuses_llm_key_only_after_capability_is_enabled(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    from opensquilla.gateway.config import ImageGenerationConfig, LlmProviderConfig
    from opensquilla.tools.builtin.media import (
        _resolve_image_generation_candidates,
        configure_image_generation,
        image_generation_available,
    )

    image_config = ImageGenerationConfig()
    llm_config = LlmProviderConfig(
        provider="openrouter",
        model="z-ai/glm-5.1",
        api_key="sk-or-configured",
        base_url="https://openrouter.ai/api/v1",
    )

    configure_image_generation(image_config, llm_config=llm_config)

    provider = get_image_generation_provider("openrouter")
    assert provider is not None
    assert provider._resolve_api_key() == "sk-or-configured"
    assert "openrouter/google/gemini-3.1-flash-image-preview" in (
        _resolve_image_generation_candidates(None, image_config)
    )
    assert not image_generation_available()

    image_config.enabled = True
    assert image_generation_available()


def test_image_generation_llm_key_does_not_cross_endpoint_origin(monkeypatch) -> None:
    # The primary LLM key is a credential for the LLM's endpoint only: a
    # custom image base_url with a different origin must not resolve it.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    from opensquilla.gateway.config import (
        ImageGenerationConfig,
        ImageGenerationOpenAIProviderConfig,
        ImageGenerationProvidersConfig,
        LlmProviderConfig,
    )
    from opensquilla.tools.builtin.media import configure_image_generation

    llm_config = LlmProviderConfig(
        provider="openai",
        api_key="sk-real",
        base_url="https://api.openai.com/v1",
    )
    image_config = ImageGenerationConfig(
        enabled=True,
        primary="openai/gpt-image-1",
        providers=ImageGenerationProvidersConfig(
            openai=ImageGenerationOpenAIProviderConfig(
                base_url="https://other.example.com/v1"
            )
        ),
    )

    configure_image_generation(image_config, llm_config=llm_config)

    provider = get_image_generation_provider("openai")
    assert provider is not None
    assert provider._base_url == "https://other.example.com/v1"
    assert provider._resolve_api_key() == ""


def test_image_generation_default_env_does_not_cross_endpoint_origin(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-default-origin-key")

    from opensquilla.gateway.config import (
        ImageGenerationConfig,
        ImageGenerationOpenAIProviderConfig,
        ImageGenerationProvidersConfig,
    )
    from opensquilla.tools.builtin.media import configure_image_generation

    image_config = ImageGenerationConfig(
        enabled=True,
        primary="openai/gpt-image-1",
        providers=ImageGenerationProvidersConfig(
            openai=ImageGenerationOpenAIProviderConfig(
                api_key_env="",
                base_url="https://other.example.com/v1",
            )
        ),
    )

    configure_image_generation(image_config)

    provider = get_image_generation_provider("openai")
    assert provider is not None
    assert provider._base_url == "https://other.example.com/v1"
    assert provider._resolve_api_key() == ""


def test_image_generation_explicit_env_is_allowed_for_custom_origin(monkeypatch) -> None:
    monkeypatch.setenv("CUSTOM_IMAGE_KEY", "sk-custom-origin-key")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-default-origin-key")

    from opensquilla.gateway.config import (
        ImageGenerationConfig,
        ImageGenerationOpenAIProviderConfig,
        ImageGenerationProvidersConfig,
    )
    from opensquilla.tools.builtin.media import configure_image_generation

    image_config = ImageGenerationConfig(
        enabled=True,
        primary="openai/gpt-image-1",
        providers=ImageGenerationProvidersConfig(
            openai=ImageGenerationOpenAIProviderConfig(
                api_key_env="CUSTOM_IMAGE_KEY",
                base_url="https://other.example.com/v1",
            )
        ),
    )

    configure_image_generation(image_config)

    provider = get_image_generation_provider("openai")
    assert provider is not None
    assert provider._resolve_api_key() == "sk-custom-origin-key"


def test_image_generation_llm_key_reused_on_same_endpoint_origin(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    from opensquilla.gateway.config import (
        ImageGenerationConfig,
        ImageGenerationOpenAIProviderConfig,
        ImageGenerationProvidersConfig,
        LlmProviderConfig,
    )
    from opensquilla.tools.builtin.media import configure_image_generation

    llm_config = LlmProviderConfig(
        provider="openai",
        api_key="sk-real",
        base_url="https://api.openai.com/v1",
    )
    image_config = ImageGenerationConfig(
        enabled=True,
        primary="openai/gpt-image-1",
        providers=ImageGenerationProvidersConfig(
            openai=ImageGenerationOpenAIProviderConfig(
                base_url="https://api.openai.com/images/path"
            )
        ),
    )

    configure_image_generation(image_config, llm_config=llm_config)

    provider = get_image_generation_provider("openai")
    assert provider is not None
    assert provider._resolve_api_key() == "sk-real"


def test_vision_provider_uses_configured_router_image_tier(monkeypatch) -> None:
    _clear_vision_provider_env(monkeypatch)

    from opensquilla.gateway.config import (
        ImageGenerationConfig,
        LlmProviderConfig,
        SquillaRouterConfig,
    )
    from opensquilla.tools.builtin import media

    llm_config = LlmProviderConfig(
        provider="openrouter",
        model="deepseek/deepseek-v4-flash",
        api_key="sk-or-configured",
        base_url="https://router.example/v1",
        proxy="http://proxy.example",
        provider_routing={"moonshotai/kimi-k2.6": "preferred-upstream"},
    )
    router_config = SquillaRouterConfig(
        tiers={
            "t1": {
                "provider": "openrouter",
                "model": "deepseek/deepseek-v4-flash",
                "supports_image": False,
            },
            "image_model": {
                "provider": "openrouter",
                "model": "moonshotai/kimi-k2.6",
                "supports_image": True,
                "image_only": True,
            },
        }
    )

    media.configure_image_generation(
        ImageGenerationConfig(),
        llm_config=llm_config,
        squilla_router_config=router_config,
    )
    try:
        cfg = media._resolve_vision_provider_config(default_model="openai/gpt-4o-mini")
    finally:
        media.configure_image_generation(None)

    assert cfg.provider == "openrouter"
    assert cfg.model == "moonshotai/kimi-k2.6"
    assert cfg.api_key == "sk-or-configured"
    assert cfg.base_url == "https://router.example/v1"
    assert cfg.proxy == "http://proxy.example"
    assert cfg.provider_routing == {"moonshotai/kimi-k2.6": "preferred-upstream"}


def test_vision_provider_env_override_wins_over_router_image_tier(monkeypatch) -> None:
    _clear_vision_provider_env(monkeypatch)
    monkeypatch.setenv("OPENSQUILLA_VISION_PROVIDER", "anthropic")
    monkeypatch.setenv("OPENSQUILLA_VISION_MODEL", "claude-3-5-sonnet-latest")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-configured")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://anthropic.example")

    from opensquilla.gateway.config import (
        ImageGenerationConfig,
        LlmProviderConfig,
        SquillaRouterConfig,
    )
    from opensquilla.tools.builtin import media

    media.configure_image_generation(
        ImageGenerationConfig(),
        llm_config=LlmProviderConfig(provider="openrouter", api_key="sk-or-configured"),
        squilla_router_config=SquillaRouterConfig(),
    )
    try:
        cfg = media._resolve_vision_provider_config(default_model="openai/gpt-4o-mini")
    finally:
        media.configure_image_generation(None)

    assert cfg.provider == "anthropic"
    assert cfg.model == "claude-3-5-sonnet-latest"
    assert cfg.api_key == "sk-ant-configured"
    assert cfg.base_url == "https://anthropic.example"


def test_vision_provider_resolves_demoted_primary_from_profile(monkeypatch) -> None:
    _clear_vision_provider_env(monkeypatch)

    from opensquilla.gateway.config import GatewayConfig
    from opensquilla.tools.builtin import media

    config = GatewayConfig(
        llm={
            "provider": "deepseek",
            "model": "deepseek-chat",
            "api_key": "synthetic-active-secret",
            "base_url": "https://api.deepseek.com/v1",
        },
        llm_profiles={
            "OpenAI": {
                "api_key": "synthetic-demoted-secret",
                "base_url": "https://profile-openai.example/v1",
                "proxy": "http://profile-proxy.example:8080",
            }
        },
        squilla_router={
            "tier_profile": "openai",
            "tiers": {
                "image_model": {
                    "provider": "openai",
                    "model": "gpt-4o-mini",
                    "supports_image": True,
                    "image_only": True,
                }
            },
        },
    )

    media.configure_image_generation(
        config.image_generation,
        gateway_config=config,
        llm_config=config.llm,
        squilla_router_config=config.squilla_router,
    )
    try:
        resolved = media._resolve_vision_provider_config(
            default_model="openai/gpt-4o-mini"
        )
    finally:
        media.configure_image_generation(None)

    assert resolved.provider == "openai"
    assert resolved.model == "gpt-4o-mini"
    assert resolved.api_key == "synthetic-demoted-secret"
    assert resolved.base_url == "https://profile-openai.example/v1"
    assert resolved.proxy == "http://profile-proxy.example:8080"
    assert resolved.replay_provider_state is False
    assert "synthetic-demoted-secret" not in repr(resolved)


def test_image_analysis_tool_timeout_exceeds_provider_request_timeout() -> None:
    from opensquilla.provider.types import ChatConfig
    from opensquilla.tools.registry import get_default_registry

    registered = get_default_registry().get("image")

    assert registered is not None
    assert registered.spec.execution_timeout_seconds is not None
    assert registered.spec.execution_timeout_seconds > ChatConfig().timeout


@pytest.mark.asyncio
async def test_image_tool_uses_configured_router_vision_provider_for_local_file(
    monkeypatch,
    tmp_path,
) -> None:
    _clear_vision_provider_env(monkeypatch)

    from opensquilla.gateway.config import (
        ImageGenerationConfig,
        LlmProviderConfig,
        SquillaRouterConfig,
    )
    from opensquilla.provider.types import ContentBlockImage, ContentBlockText, Message
    from opensquilla.tools.builtin import media

    llm_config = LlmProviderConfig(
        provider="openrouter",
        model="deepseek/deepseek-v4-flash",
        api_key="sk-or-configured",
    )
    router_config = SquillaRouterConfig(
        tiers={
            "image_model": {
                "provider": "openrouter",
                "model": "moonshotai/kimi-k2.6",
                "supports_image": True,
                "image_only": True,
            }
        }
    )
    media.configure_image_generation(
        ImageGenerationConfig(),
        llm_config=llm_config,
        squilla_router_config=router_config,
    )
    png_path = tmp_path / "generated-image.png"
    png_path.write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAAAAAA6fptVAAAACklEQVR4nGNgAAAAAgABSK+kcQAAAABJRU5ErkJggg=="
        )
    )
    captured: dict[str, object] = {}

    class FakeProvider:
        async def chat(self, *, messages, config=None):
            captured["messages"] = messages
            yield SimpleNamespace(text="a generated image")

    class FakeSelector:
        def __init__(self, selector_config):
            captured["primary"] = selector_config.primary

        def resolve(self):
            return FakeProvider()

    monkeypatch.setattr("opensquilla.provider.selector.ModelSelector", FakeSelector)

    try:
        result = await media.image(str(png_path), prompt="Describe this image")
    finally:
        media.configure_image_generation(None)

    payload = json.loads(result)
    assert payload["description"] == "a generated image"
    assert payload["model"] == "provider"
    assert captured["primary"].model == "moonshotai/kimi-k2.6"
    messages = captured["messages"]
    assert isinstance(messages, list)
    message = messages[0]
    assert isinstance(message, Message)
    assert isinstance(message.content[0], ContentBlockImage)
    assert message.content[0].media_type == "image/png"
    assert isinstance(message.content[1], ContentBlockText)
    assert message.content[1].text == "Describe this image"


@pytest.mark.asyncio
async def test_vision_provider_sends_provider_native_multimodal_message(monkeypatch) -> None:
    _clear_vision_provider_env(monkeypatch)

    from opensquilla.provider.correlation_context import bind_provider_request_correlation
    from opensquilla.provider.types import (
        ContentBlockImage,
        ContentBlockText,
        Message,
        ProviderRequestCorrelation,
    )
    from opensquilla.tools.builtin import media

    media.configure_image_generation(None)
    captured: dict[str, object] = {}

    class FakeProvider:
        async def chat(self, *, messages, config=None):
            captured["messages"] = messages
            captured["config"] = config
            yield SimpleNamespace(text="described")

    class FakeSelector:
        def __init__(self, selector_config):
            captured["primary"] = selector_config.primary

        def resolve(self):
            return FakeProvider()

    monkeypatch.setattr("opensquilla.provider.selector.ModelSelector", FakeSelector)

    root = ProviderRequestCorrelation(
        session_id="session-1",
        turn_id="turn-1",
        execution_id="root-execution",
        call_kind="agent.chat",
    )
    with bind_provider_request_correlation(root):
        result = await media._call_vision_provider(
            b64_data="aW1hZ2UtYnl0ZXM=",
            media_type="image/png",
            prompt="What is in this image?",
        )

    assert result == "described"
    correlation = captured["config"].provider_request_correlation
    assert correlation.session_id == root.session_id
    assert correlation.turn_id == root.turn_id
    assert correlation.execution_id != root.execution_id
    assert correlation.call_kind == "auxiliary.media"
    messages = captured["messages"]
    assert isinstance(messages, list)
    assert len(messages) == 1
    message = messages[0]
    assert isinstance(message, Message)
    assert message.role == "user"
    assert isinstance(message.content[0], ContentBlockImage)
    assert message.content[0].media_type == "image/png"
    assert message.content[0].data == "aW1hZ2UtYnl0ZXM="
    assert isinstance(message.content[1], ContentBlockText)
    assert message.content[1].text == "What is in this image?"


@pytest.mark.asyncio
async def test_vision_provider_error_event_is_not_empty_success(monkeypatch) -> None:
    _clear_vision_provider_env(monkeypatch)

    from opensquilla.provider.types import ErrorEvent
    from opensquilla.tools.builtin import media

    media.configure_image_generation(None)

    class FakeProvider:
        async def chat(self, *, messages, config=None):
            yield ErrorEvent(message="Request timed out", code="timeout")

    class FakeSelector:
        def __init__(self, selector_config):
            return None

        def resolve(self):
            return FakeProvider()

    monkeypatch.setattr("opensquilla.provider.selector.ModelSelector", FakeSelector)

    with pytest.raises(RuntimeError, match="Provider stream error.*timeout"):
        await media._call_vision_provider(
            b64_data="aW1hZ2UtYnl0ZXM=",
            media_type="image/png",
            prompt="What is in this image?",
        )


@pytest.mark.asyncio
async def test_text_media_llm_uses_provider_native_message(monkeypatch) -> None:
    _clear_vision_provider_env(monkeypatch)

    from opensquilla.provider.types import Message
    from opensquilla.tools.builtin import media

    captured: dict[str, object] = {}

    class FakeProvider:
        async def chat(self, *, messages, config=None):
            captured["messages"] = messages
            captured["config"] = config
            yield SimpleNamespace(text="analyzed")

    class FakeSelector:
        def __init__(self, selector_config):
            captured["primary"] = selector_config.primary

        def resolve(self):
            return FakeProvider()

    monkeypatch.setattr("opensquilla.provider.selector.ModelSelector", FakeSelector)

    result = await media._call_llm_with_text("Extracted text", "Analyze this")

    assert result == "analyzed"
    messages = captured["messages"]
    assert isinstance(messages, list)
    assert len(messages) == 1
    message = messages[0]
    assert isinstance(message, Message)
    assert message.role == "user"
    assert message.content == "Analyze this\n\n---\nExtracted text"


def test_image_generation_uses_provider_specific_api_key(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    from opensquilla.gateway.config import (
        ImageGenerationConfig,
        ImageGenerationOpenAIProviderConfig,
        ImageGenerationProvidersConfig,
    )
    from opensquilla.tools.builtin.media import (
        configure_image_generation,
        image_generation_available,
    )

    image_config = ImageGenerationConfig(
        enabled=True,
        primary="openai/gpt-image-1",
        providers=ImageGenerationProvidersConfig(
            openai=ImageGenerationOpenAIProviderConfig(api_key="sk-openai-configured")
        ),
    )

    configure_image_generation(image_config)

    provider = get_image_generation_provider("openai")
    assert provider is not None
    assert provider._resolve_api_key() == "sk-openai-configured"
    assert image_generation_available()


def test_image_generation_nondefault_primary_does_not_auto_add_llm_provider(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    from opensquilla.gateway.config import ImageGenerationConfig, LlmProviderConfig
    from opensquilla.tools.builtin.media import (
        _resolve_image_generation_candidates,
        configure_image_generation,
    )

    image_config = ImageGenerationConfig(primary="openai/custom-image-model")
    configure_image_generation(
        image_config,
        llm_config=LlmProviderConfig(provider="openrouter", api_key="sk-or-configured"),
    )

    assert _resolve_image_generation_candidates(None, image_config) == ["openai/custom-image-model"]


def test_image_generation_persisted_default_primary_still_adds_llm_provider(
    monkeypatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    from opensquilla.gateway.config import GatewayConfig, LlmProviderConfig
    from opensquilla.tools.builtin.media import (
        _resolve_image_generation_candidates,
        configure_image_generation,
    )

    config = GatewayConfig.model_validate(GatewayConfig().model_dump(mode="python"))
    config.llm = LlmProviderConfig(provider="openrouter", api_key="sk-or-configured")
    configure_image_generation(config.image_generation, llm_config=config.llm)

    assert "openrouter/google/gemini-3.1-flash-image-preview" in (
        _resolve_image_generation_candidates(None, config.image_generation)
    )


def test_image_generation_capability_exposes_agent_tool_when_configured(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    from opensquilla.engine.runtime import TurnRunner
    from opensquilla.gateway.config import ImageGenerationConfig, LlmProviderConfig
    from opensquilla.tools.builtin.media import configure_image_generation
    from opensquilla.tools.registry import get_default_registry
    from opensquilla.tools.types import CallerKind, ToolContext

    configure_image_generation(
        ImageGenerationConfig(enabled=True),
        llm_config=LlmProviderConfig(provider="openrouter", api_key="sk-or-configured"),
    )
    runner = object.__new__(TurnRunner)
    runner._tool_registry = get_default_registry()

    ctx = ToolContext(is_owner=True, caller_kind=CallerKind.WEB, agent_id="main")
    ctx = TurnRunner._apply_runtime_capability_denies(runner, ctx)
    tool_defs = runner._tool_registry.to_tool_definitions(ctx)
    tool_defs = TurnRunner._filter_tool_defs_by_capability(runner, tool_defs)
    names = {tool.name for tool in tool_defs}

    assert "image_generate" in names


def test_image_generation_capability_does_not_expose_agent_tool_when_disabled(
    monkeypatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    from opensquilla.engine.runtime import TurnRunner
    from opensquilla.gateway.config import ImageGenerationConfig, LlmProviderConfig
    from opensquilla.tools.builtin.media import configure_image_generation
    from opensquilla.tools.registry import get_default_registry
    from opensquilla.tools.types import CallerKind, ToolContext

    configure_image_generation(
        ImageGenerationConfig(),
        llm_config=LlmProviderConfig(provider="openrouter", api_key="sk-or-configured"),
    )
    runner = object.__new__(TurnRunner)
    runner._tool_registry = get_default_registry()

    ctx = ToolContext(is_owner=True, caller_kind=CallerKind.WEB, agent_id="main")
    ctx = TurnRunner._apply_runtime_capability_denies(runner, ctx)
    tool_defs = runner._tool_registry.to_tool_definitions(ctx)
    tool_defs = TurnRunner._filter_tool_defs_by_capability(runner, tool_defs)
    names = {tool.name for tool in tool_defs}

    assert "image_generate" not in names
