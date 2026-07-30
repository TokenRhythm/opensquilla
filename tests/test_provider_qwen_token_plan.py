from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from opensquilla.engine.types import ThinkingLevel
from opensquilla.provider.anthropic import AnthropicProvider
from opensquilla.provider.model_catalog import ModelCatalog
from opensquilla.provider.openai import OpenAIProvider
from opensquilla.provider.preset_registry import get_preset
from opensquilla.provider.qwen_token_plan import (
    QWEN_TOKEN_PLAN_ANTHROPIC_BASE_URL,
    QWEN_TOKEN_PLAN_IMAGE_MODEL_IDS,
    QWEN_TOKEN_PLAN_MODEL_IDS,
    QWEN_TOKEN_PLAN_OPENAI_BASE_URL,
)
from opensquilla.provider.registry import get_provider_spec
from opensquilla.provider.selector import ProviderConfig, _build_provider
from opensquilla.provider.types import (
    ChatConfig,
    ContentBlockToolUse,
    Message,
    StreamEvent,
    ToolDefinition,
    ToolInputSchema,
)

_TOOL = ToolDefinition(
    name="read_file",
    description="Read a synthetic file.",
    input_schema=ToolInputSchema(
        properties={"path": {"type": "string"}},
        required=["path"],
    ),
)


async def _consume(stream: AsyncIterator[StreamEvent]) -> None:
    async for _event in stream:
        pass


def _patch_transport(
    monkeypatch: pytest.MonkeyPatch,
    *,
    module: str,
    captured: dict[str, Any],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = request.headers
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            400,
            json={"error": {"message": "synthetic rejection after capture"}},
            request=request,
        )

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(
        f"opensquilla.provider.{module}.httpx.AsyncClient",
        patched_async_client,
    )


def _caps(model: str):
    return ModelCatalog().get_capabilities(
        model,
        provider_name="qwen_token_plan",
    )


@pytest.mark.parametrize(
    ("provider_id", "base_url"),
    [
        ("qwen_token_plan", QWEN_TOKEN_PLAN_OPENAI_BASE_URL),
        ("qwen_token_plan_anthropic", QWEN_TOKEN_PLAN_ANTHROPIC_BASE_URL),
    ],
)
def test_token_plan_model_picker_trust_is_limited_to_the_official_host(
    provider_id: str,
    base_url: str,
) -> None:
    spec = get_provider_spec(provider_id)

    assert spec.default_base_url == base_url
    assert spec.selectable_model_catalog == "verified_live"
    assert spec.compat.official_host == "token-plan.cn-beijing.maas.aliyuncs.com"


@pytest.mark.asyncio
async def test_openai_protocol_model_discovery_excludes_native_image_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    advertised_ids = (
        "qwen3.8-max-preview",
        "qwen3.7-max",
        "qwen3.7-plus",
        "qwen3.6-flash",
        "glm-5.2",
        "deepseek-v4-pro",
        *QWEN_TOKEN_PLAN_IMAGE_MODEL_IDS,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"id": model_id} for model_id in advertised_ids]},
            request=request,
        )

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(
        "opensquilla.provider.openai.httpx.AsyncClient",
        patched_async_client,
    )
    provider = _build_provider(
        ProviderConfig(
            provider="qwen_token_plan",
            model="qwen3.7-plus",
            api_key="sk-sp-test",
        )
    )

    listed = await provider.list_models()

    assert [item.model_id for item in listed] == list(advertised_ids[:-2])
    assert not {item.model_id for item in listed} & set(QWEN_TOKEN_PLAN_IMAGE_MODEL_IDS)


@pytest.mark.asyncio
async def test_dashscope_deepseek_v4_normalizes_required_tool_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, module="openai", captured=captured)
    provider = _build_provider(
        ProviderConfig(
            provider="dashscope",
            model="deepseek-v4-pro",
            api_key="sk-ws-test",
        )
    )

    await _consume(
        provider.chat(
            [Message(role="user", content="use the tool")],
            tools=[_TOOL],
            config=ChatConfig(
                thinking=False,
                tool_choice="required",
                model_capabilities=ModelCatalog().get_capabilities(
                    "deepseek-v4-pro",
                    provider_name="dashscope",
                ),
            ),
        )
    )

    payload = captured["payload"]
    assert payload["tool_choice"] == "auto"
    assert "enable_thinking" not in payload


@pytest.mark.parametrize(
    ("model", "context_window", "max_output", "vision", "reasoning_format"),
    [
        ("qwen3.8-max-preview", 983_616, 131_072, True, "qwen_token_plan_qwen"),
        ("qwen3.7-max", 1_000_000, 65_536, False, "qwen_token_plan_qwen"),
        ("qwen3.7-plus", 1_000_000, 65_536, True, "qwen_token_plan_qwen"),
        ("qwen3.6-plus", 1_000_000, 65_536, True, "qwen_token_plan_qwen"),
        ("qwen3.6-flash", 1_000_000, 65_536, True, "qwen_token_plan_qwen"),
        (
            "deepseek-v4-pro",
            1_000_000,
            393_216,
            False,
            "qwen_token_plan_deepseek",
        ),
        (
            "deepseek-v4-flash",
            1_000_000,
            393_216,
            False,
            "qwen_token_plan_deepseek",
        ),
        ("deepseek-v3.2", 131_072, 65_536, False, "qwen_token_plan"),
        ("kimi-k2.7-code", 262_144, 16_384, True, "qwen_token_plan_kimi"),
        ("kimi-k2.6", 262_144, 98_304, True, "qwen_token_plan_kimi"),
        ("kimi-k2.5", 262_144, 32_768, True, "qwen_token_plan_kimi"),
        ("glm-5.2", 1_000_000, 131_072, False, "qwen_token_plan_glm"),
        ("glm-5.1", 202_752, 131_072, False, "qwen_token_plan_glm"),
        ("glm-5", 202_752, 16_384, False, "qwen_token_plan_glm"),
        ("MiniMax-M2.5", 196_608, 32_768, False, "qwen_token_plan"),
    ],
)
def test_token_plan_catalog_matches_documented_allowlist(
    model: str,
    context_window: int,
    max_output: int,
    vision: bool,
    reasoning_format: str,
) -> None:
    catalog = ModelCatalog()
    entry = catalog.resolve_entry(model, provider="qwen_token_plan")

    assert entry.context_window == context_window
    assert entry.max_output_tokens == max_output
    assert entry.supports_reasoning is True
    assert entry.supports_tools is True
    assert entry.supports_vision is vision
    assert entry.reasoning_format == reasoning_format


def test_anthropic_protocol_reuses_exact_token_plan_catalog() -> None:
    catalog = ModelCatalog()
    openai_entry = catalog.resolve_entry(
        "qwen3.8-max-preview",
        provider="qwen_token_plan",
    )
    anthropic_entry = catalog.resolve_entry(
        "qwen3.8-max-preview",
        provider="qwen_token_plan_anthropic",
    )

    assert anthropic_entry.provider_id == "qwen_token_plan_anthropic"
    assert anthropic_entry.model_id == openai_entry.model_id
    assert anthropic_entry.context_window == openai_entry.context_window
    assert anthropic_entry.max_output_tokens == openai_entry.max_output_tokens
    assert anthropic_entry.supports_reasoning == openai_entry.supports_reasoning
    assert anthropic_entry.supports_tools == openai_entry.supports_tools
    assert anthropic_entry.supports_vision == openai_entry.supports_vision
    assert anthropic_entry.reasoning_format == openai_entry.reasoning_format
    assert catalog.resolve_context_window(
        "qwen3.8-max-preview",
        provider="qwen_token_plan_anthropic",
    ) == 983_616


@pytest.mark.asyncio
async def test_qwen38_forces_thinking_and_normalizes_wire_constraints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, module="openai", captured=captured)
    provider = _build_provider(
        ProviderConfig(
            provider="qwen_token_plan",
            model="qwen3.8-max-preview",
            api_key="sk-sp-test",
        )
    )
    assert isinstance(provider, OpenAIProvider)
    messages = [
        Message(
            role="assistant",
            content="prior answer",
            reasoning_content="prior reasoning",
        ),
        Message(role="user", content="continue"),
    ]

    await _consume(
        provider.chat(
            messages,
            tools=[_TOOL],
            config=ChatConfig(
                temperature=0.2,
                tool_choice="required",
                model_capabilities=_caps("qwen3.8-max-preview"),
            ),
        )
    )

    payload = captured["payload"]
    assert captured["url"].endswith("/compatible-mode/v1/chat/completions")
    assert captured["headers"]["authorization"] == "Bearer sk-sp-test"
    assert payload["enable_thinking"] is True
    assert payload["temperature"] == 0.6
    assert payload["tool_choice"] == "auto"
    assert payload["preserve_thinking"] is True
    assert payload["messages"][0]["reasoning_content"] == "prior reasoning"
    assert "thinking_budget" not in payload
    assert "thinking" not in payload


@pytest.mark.asyncio
async def test_qwen_explicit_thinking_budget_is_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, module="openai", captured=captured)
    provider = _build_provider(
        ProviderConfig(
            provider="qwen_token_plan",
            model="qwen3.7-plus",
            api_key="sk-sp-test",
        )
    )

    await _consume(
        provider.chat(
            [Message(role="user", content="think")],
            config=ChatConfig(
                thinking=True,
                thinking_budget_tokens=7_000,
                model_capabilities=_caps("qwen3.7-plus"),
            ),
        )
    )

    assert captured["payload"]["enable_thinking"] is True
    assert captured["payload"]["thinking_budget"] == 7_000
    assert captured["payload"]["preserve_thinking"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("level", "expected_effort"),
    [
        (ThinkingLevel.MINIMAL, "low"),
        (ThinkingLevel.LOW, "low"),
        (ThinkingLevel.MEDIUM, "medium"),
        (ThinkingLevel.HIGH, "medium"),
        (ThinkingLevel.XHIGH, "xhigh"),
    ],
)
async def test_qwen38_maps_reasoning_effort_to_documented_values(
    monkeypatch: pytest.MonkeyPatch,
    level: ThinkingLevel,
    expected_effort: str,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, module="openai", captured=captured)
    provider = _build_provider(
        ProviderConfig(
            provider="qwen_token_plan",
            model="qwen3.8-max-preview",
            api_key="sk-sp-test",
        )
    )

    await _consume(
        provider.chat(
            [Message(role="user", content="think")],
            config=ChatConfig(
                thinking=True,
                thinking_level=level,
                model_capabilities=_caps("qwen3.8-max-preview"),
            ),
        )
    )

    payload = captured["payload"]
    assert payload["enable_thinking"] is True
    assert payload["reasoning_effort"] == expected_effort
    assert "thinking_budget" not in payload


@pytest.mark.asyncio
async def test_qwen38_explicit_budget_does_not_conflict_with_reasoning_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, module="openai", captured=captured)
    provider = _build_provider(
        ProviderConfig(
            provider="qwen_token_plan",
            model="qwen3.8-max-preview",
            api_key="sk-sp-test",
        )
    )

    await _consume(
        provider.chat(
            [Message(role="user", content="think")],
            config=ChatConfig(
                thinking=True,
                thinking_level=ThinkingLevel.LOW,
                thinking_budget_tokens=7_000,
                model_capabilities=_caps("qwen3.8-max-preview"),
            ),
        )
    )

    payload = captured["payload"]
    assert payload["thinking_budget"] == 7_000
    assert "reasoning_effort" not in payload


@pytest.mark.asyncio
async def test_non_forced_model_preserves_pinned_tool_by_disabling_thinking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, module="openai", captured=captured)
    provider = _build_provider(
        ProviderConfig(
            provider="qwen_token_plan",
            model="qwen3.7-plus",
            api_key="sk-sp-test",
        )
    )
    pinned = {"type": "function", "function": {"name": "read_file"}}

    await _consume(
        provider.chat(
            [
                Message(
                    role="assistant",
                    content="prior",
                    reasoning_content="private reasoning",
                ),
                Message(role="user", content="use exactly this tool"),
            ],
            tools=[_TOOL],
            config=ChatConfig(
                thinking=True,
                tool_choice=pinned,
                model_capabilities=_caps("qwen3.7-plus"),
            ),
        )
    )

    payload = captured["payload"]
    assert payload["enable_thinking"] is False
    assert payload["tool_choice"] == pinned
    assert "thinking_budget" not in payload
    assert "preserve_thinking" not in payload
    assert "reasoning_content" not in payload["messages"][0]


@pytest.mark.asyncio
async def test_deepseek_v4_thinking_replays_reasoning_and_maps_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, module="openai", captured=captured)
    provider = _build_provider(
        ProviderConfig(
            provider="qwen_token_plan",
            model="deepseek-v4-pro",
            api_key="sk-sp-test",
        )
    )

    await _consume(
        provider.chat(
            [
                Message(role="assistant", content="prior answer"),
                Message(role="user", content="continue"),
            ],
            config=ChatConfig(
                thinking=True,
                thinking_level=ThinkingLevel.XHIGH,
                model_capabilities=_caps("deepseek-v4-pro"),
            ),
        )
    )

    payload = captured["payload"]
    assert payload["enable_thinking"] is True
    assert payload["reasoning_effort"] == "max"
    assert payload["messages"][0]["reasoning_content"] == ""
    assert "thinking" not in payload


@pytest.mark.asyncio
async def test_deepseek_v4_thinking_off_drops_reasoning_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, module="openai", captured=captured)
    provider = _build_provider(
        ProviderConfig(
            provider="qwen_token_plan",
            model="deepseek-v4-pro",
            api_key="sk-sp-test",
        )
    )

    await _consume(
        provider.chat(
            [
                Message(
                    role="assistant",
                    content="prior answer",
                    reasoning_content="private reasoning",
                ),
                Message(role="user", content="continue"),
            ],
            config=ChatConfig(
                thinking=False,
                model_capabilities=_caps("deepseek-v4-pro"),
            ),
        )
    )

    payload = captured["payload"]
    assert payload["enable_thinking"] is False
    assert "reasoning_effort" not in payload
    assert "reasoning_content" not in payload["messages"][0]


@pytest.mark.asyncio
async def test_kimi_tool_turn_backfills_reasoning_and_normalizes_tool_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, module="openai", captured=captured)
    provider = _build_provider(
        ProviderConfig(
            provider="qwen_token_plan",
            model="kimi-k2.6",
            api_key="sk-sp-test",
        )
    )

    await _consume(
        provider.chat(
            [
                Message(
                    role="assistant",
                    content=[
                        ContentBlockToolUse(
                            id="call_1",
                            name="read_file",
                            input={"path": "synthetic.txt"},
                        )
                    ],
                ),
                Message(role="user", content="continue"),
            ],
            tools=[_TOOL],
            config=ChatConfig(
                thinking=True,
                tool_choice="required",
                model_capabilities=_caps("kimi-k2.6"),
            ),
        )
    )

    payload = captured["payload"]
    assert payload["enable_thinking"] is True
    assert payload["tool_choice"] == "auto"
    assert payload["messages"][0]["reasoning_content"] == ""
    assert "reasoning_effort" not in payload


@pytest.mark.asyncio
async def test_glm_tool_calls_enable_tool_stream_and_preserve_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, module="openai", captured=captured)
    provider = _build_provider(
        ProviderConfig(
            provider="qwen_token_plan",
            model="glm-5.2",
            api_key="sk-sp-test",
        )
    )

    await _consume(
        provider.chat(
            [Message(role="user", content="use a tool")],
            tools=[_TOOL],
            config=ChatConfig(
                thinking=True,
                thinking_level=ThinkingLevel.HIGH,
                model_capabilities=_caps("glm-5.2"),
            ),
        )
    )

    payload = captured["payload"]
    assert payload["enable_thinking"] is True
    assert payload["reasoning_effort"] == "high"
    assert payload["tool_stream"] is True


@pytest.mark.asyncio
async def test_glm_xhigh_maps_to_documented_max_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, module="openai", captured=captured)
    provider = _build_provider(
        ProviderConfig(
            provider="qwen_token_plan",
            model="glm-5.2",
            api_key="sk-sp-test",
        )
    )

    await _consume(
        provider.chat(
            [Message(role="user", content="think deeply")],
            config=ChatConfig(
                thinking=True,
                thinking_level=ThinkingLevel.XHIGH,
                model_capabilities=_caps("glm-5.2"),
            ),
        )
    )

    assert captured["payload"]["reasoning_effort"] == "max"


@pytest.mark.asyncio
async def test_anthropic_profile_uses_bearer_endpoint_and_exact_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, module="anthropic", captured=captured)
    provider = _build_provider(
        ProviderConfig(
            provider="qwen_token_plan_anthropic",
            model="qwen3.8-max-preview",
            api_key="sk-sp-test",
        )
    )
    assert isinstance(provider, AnthropicProvider)

    await _consume(
        provider.chat(
            [Message(role="user", content="hello")],
            config=ChatConfig(temperature=0.2),
        )
    )

    assert captured["url"].endswith("/apps/anthropic/v1/messages")
    assert captured["headers"]["authorization"] == "Bearer sk-sp-test"
    assert "x-api-key" not in captured["headers"]
    assert captured["payload"]["temperature"] == 0.6

    listed = await provider.list_models()
    assert tuple(model.model_id for model in listed) == QWEN_TOKEN_PLAN_MODEL_IDS
    assert {model.provider for model in listed} == {"qwen_token_plan_anthropic"}


@pytest.mark.asyncio
async def test_custom_anthropic_is_keyless_and_lists_only_configured_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, module="anthropic", captured=captured)
    provider = _build_provider(
        ProviderConfig(
            provider="custom_anthropic",
            model="vendor-model",
            base_url="https://llm.example.test/anthropic",
        )
    )
    assert isinstance(provider, AnthropicProvider)

    listed = await provider.list_models()

    assert [(model.provider, model.model_id) for model in listed] == [
        ("custom_anthropic", "vendor-model")
    ]

    await _consume(provider.chat([Message(role="user", content="hello")]))
    assert captured["url"].endswith("/anthropic/v1/messages")
    assert "authorization" not in captured["headers"]
    assert "x-api-key" not in captured["headers"]


@pytest.mark.asyncio
async def test_custom_openai_keyless_request_omits_empty_bearer_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(monkeypatch, module="openai", captured=captured)
    provider = _build_provider(
        ProviderConfig(
            provider="custom",
            model="vendor-model",
            base_url="https://llm.example.test/v1",
        )
    )

    await _consume(provider.chat([Message(role="user", content="hello")]))

    assert captured["url"].endswith("/v1/chat/completions")
    assert "authorization" not in captured["headers"]


def test_custom_endpoints_keep_upgrade_safe_context_default() -> None:
    catalog = ModelCatalog()

    assert catalog.resolve_context_window("unknown-model", provider="custom") == (
        8_192
    )
    assert catalog.resolve_context_window(
        "unknown-model", provider="custom_anthropic"
    ) == 8_192


def test_qwen_token_plan_router_preset_is_curated_and_inline_safe() -> None:
    preset = get_preset("qwen_token_plan")

    assert preset is not None
    assert preset.synthesized is False
    assert preset.default_model == "qwen3.7-plus"
    assert preset.tiers["c0"]["model"] == "qwen3.6-flash"
    assert preset.tiers["c3"]["model"] == "qwen3.8-max-preview"
    assert preset.tiers["image_model"]["supports_image"] is True
