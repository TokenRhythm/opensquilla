from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
import structlog.testing

from opensquilla.engine.agent import _chat_config_with_thinking_disabled
from opensquilla.engine.types import ThinkingLevel
from opensquilla.provider.openai import (
    OpenAIProvider,
    _reasoning_replay_signature,
    _ReasoningReplayStats,
    _retained_reasoning_replay_units,
)
from opensquilla.provider.types import (
    ChatConfig,
    ContentBlockToolResult,
    ContentBlockToolUse,
    ErrorEvent,
    Message,
    ModelCapabilities,
    ToolDefinition,
    ToolInputSchema,
)

TOKENRHYTHM_BARE_V4_MODELS = (
    "deepseek-v4-flash",
    "deepseek-v4-flash-0731",
    "deepseek-v4-pro",
    "deepseek-v4-pro-0813",
)
TOKENRHYTHM_V4_MODELS = TOKENRHYTHM_BARE_V4_MODELS + (
    "tokenrhythm/deepseek-v4-flash",
    "tokenrhythm/deepseek-v4-flash-0731",
    "tokenrhythm/deepseek-v4-pro",
    "tokenrhythm/deepseek-v4-pro-0813",
)
TOKENRHYTHM_0813_MODELS = (
    "deepseek-v4-pro-0813",
    "tokenrhythm/deepseek-v4-pro-0813",
)
TOKENRHYTHM_REASONING_LIMIT_UTF16_UNITS = 50_000


@pytest.fixture(autouse=True)
def _clear_reasoning_echo_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENSQUILLA_REASONING_ECHO_TURNS", raising=False)


def _provider(
    *,
    model: str = "deepseek-v4-flash",
    base_url: str = "https://tokenrhythm.studio/v1",
    provider_kind: str = "tokenrhythm",
) -> OpenAIProvider:
    return OpenAIProvider(
        api_key="synthetic-tokenrhythm-key",
        model=model,
        base_url=base_url,
        provider_kind=provider_kind,
    )


def _tokenrhythm_caps() -> ModelCapabilities:
    # This is the catalog shape used by TokenRhythm today. The exact V4 wire
    # policy, not a falsely advertised catalog dialect, owns the compatibility
    # behavior under test.
    return ModelCapabilities(
        supports_reasoning=True,
        supports_tools=True,
        reasoning_format="none",
    )


def _config(
    *,
    thinking: bool = True,
    thinking_level: ThinkingLevel | None = ThinkingLevel.HIGH,
    tool_choice: Any | None = None,
) -> ChatConfig:
    return ChatConfig(
        thinking=thinking,
        thinking_level=thinking_level,
        tool_choice=tool_choice,
        model_capabilities=_tokenrhythm_caps(),
    )


def _payload(
    provider: OpenAIProvider,
    messages: list[Message],
    *,
    config: ChatConfig | None = None,
    tools: list[ToolDefinition] | None = None,
) -> dict[str, Any]:
    return provider.project_final_request(
        messages,
        tools=tools,
        config=config or _config(),
    ).payload


def _text_history(reasoning: str) -> list[Message]:
    return [
        Message(
            role="assistant",
            content="A prior assistant answer.",
            reasoning_content=reasoning,
        ),
        Message(role="user", content="Continue."),
    ]


def _tool_history(reasoning: str | None) -> list[Message]:
    return [
        Message(
            role="assistant",
            content=[
                ContentBlockToolUse(
                    id="call_lookup",
                    name="lookup",
                    input={"key": "synthetic"},
                )
            ],
            reasoning_content=reasoning,
        ),
        Message(
            role="user",
            content=[
                ContentBlockToolResult(
                    tool_use_id="call_lookup",
                    content="synthetic result",
                )
            ],
        ),
        Message(role="user", content="Continue."),
    ]


def _reasoning_with_utf16_units(kind: str, units: int) -> str:
    if kind == "ascii":
        value = "a" * units
    elif kind == "cjk":
        value = "界" * units
    elif kind == "emoji":
        value = "🧠" * (units // 2) + ("a" if units % 2 else "")
    else:  # pragma: no cover - the parametrization is closed
        raise AssertionError(f"unsupported fixture kind: {kind}")
    assert len(value.encode("utf-16-le")) // 2 == units
    return value


LOOKUP_TOOL = ToolDefinition(
    name="lookup",
    description="Look up a synthetic value.",
    input_schema=ToolInputSchema(
        properties={"key": {"type": "string"}},
        required=["key"],
    ),
)


@pytest.mark.parametrize("model", TOKENRHYTHM_V4_MODELS)
def test_tokenrhythm_v4_text_assistant_withholds_real_reasoning(model: str) -> None:
    reasoning = "private provider reasoning"
    messages = _text_history(reasoning)
    canonical_before = [message.model_dump(mode="json") for message in messages]

    payload = _payload(_provider(model=model), messages)

    assert payload["messages"][0]["reasoning_content"] == ""
    assert [message.model_dump(mode="json") for message in messages] == canonical_before


@pytest.mark.parametrize(
    "base_url",
    (
        "https://tokenrhythm.studio",
        "https://tokenrhythm.studio/v1",
        "https://TOKENRHYTHM.STUDIO:443/v1/",
    ),
)
def test_tokenrhythm_v4_policy_accepts_only_supported_service_endpoints(
    base_url: str,
) -> None:
    payload = _payload(
        _provider(base_url=base_url),
        _text_history("must be hidden on the supported service endpoint"),
    )

    assert payload["messages"][0]["reasoning_content"] == ""


@pytest.mark.parametrize(
    "base_url",
    (
        "http://tokenrhythm.studio/v1",
        "https://api.tokenrhythm.studio/v1",
        "https://tokenrhythm.studio.evil.example/v1",
        "https://customer-proxy.example/v1",
        "https://tokenrhythm.studio/v1/custom",
        "https://tokenrhythm.studio:8443/v1",
        "https://user@tokenrhythm.studio/v1",
        "https://tokenrhythm.studio/v1?tenant=x",
        "https://tokenrhythm.studio/v1#fragment",
    ),
)
def test_tokenrhythm_v4_policy_does_not_grant_custom_or_lookalike_endpoints(
    base_url: str,
) -> None:
    reasoning = "legacy replay remains outside the official endpoint gate"

    payload = _payload(_provider(base_url=base_url), _text_history(reasoning))

    assert payload["messages"][0]["reasoning_content"] == reasoning
    assert "thinking" not in payload
    assert "reasoning_effort" not in payload


def test_tokenrhythm_custom_endpoint_keeps_legacy_unbounded_tool_replay() -> None:
    reasoning = _reasoning_with_utf16_units(
        "emoji",
        TOKENRHYTHM_REASONING_LIMIT_UTF16_UNITS + 1,
    )

    payload = _payload(
        _provider(base_url="https://customer-proxy.example/v1"),
        _tool_history(reasoning),
    )

    assert payload["messages"][0]["reasoning_content"] == reasoning
    assert "thinking" not in payload
    assert "reasoning_effort" not in payload


@pytest.mark.parametrize("model", TOKENRHYTHM_0813_MODELS)
@pytest.mark.parametrize(
    "base_url",
    (
        "https://customer-proxy.example/v1",
        "https://tokenrhythm.studio/v1/custom",
    ),
)
def test_tokenrhythm_0813_custom_endpoint_does_not_replay_reasoning(
    model: str,
    base_url: str,
) -> None:
    messages = _tool_history("0813 reasoning must remain endpoint-scoped")
    canonical_before = [message.model_dump(mode="json") for message in messages]

    payload = _payload(
        _provider(model=model, base_url=base_url),
        messages,
    )

    assert "reasoning_content" not in payload["messages"][0]
    assert "thinking" not in payload
    assert "reasoning_effort" not in payload
    assert [message.model_dump(mode="json") for message in messages] == canonical_before


def test_tokenrhythm_v4_policy_requires_exact_provider_kind() -> None:
    payload = _payload(
        _provider(provider_kind="openai"),
        _text_history("must not gain TokenRhythm behavior from the hostname alone"),
    )

    assert "reasoning_content" not in payload["messages"][0]
    assert "thinking" not in payload
    assert "reasoning_effort" not in payload


@pytest.mark.parametrize(
    "model",
    (
        "deepseek-v3.2",
        "deepseek-v4-flash-preview",
        "deepseek-v4-pro-0813-preview",
        "untrusted/deepseek-v4-flash",
        "tokenrhythm/deepseek-v4-flash-preview",
        "tokenrhythm/deepseek-v4-pro-0813-preview",
    ),
)
def test_tokenrhythm_v4_policy_does_not_change_non_exact_model_ids(model: str) -> None:
    payload = _payload(
        _provider(model=model),
        _text_history("non-V4 history keeps its existing wire behavior"),
    )

    assert "reasoning_content" not in payload["messages"][0]
    assert "thinking" not in payload
    assert "reasoning_effort" not in payload


@pytest.mark.parametrize("model", TOKENRHYTHM_BARE_V4_MODELS)
@pytest.mark.parametrize("kind", ("ascii", "cjk", "emoji"))
@pytest.mark.parametrize(
    ("units", "is_preserved"),
    (
        (TOKENRHYTHM_REASONING_LIMIT_UTF16_UNITS - 1, True),
        (TOKENRHYTHM_REASONING_LIMIT_UTF16_UNITS, True),
        (TOKENRHYTHM_REASONING_LIMIT_UTF16_UNITS + 1, False),
    ),
)
def test_tokenrhythm_v4_tool_reasoning_uses_utf16_unit_boundary(
    model: str,
    kind: str,
    units: int,
    is_preserved: bool,
) -> None:
    reasoning = _reasoning_with_utf16_units(kind, units)
    messages = _tool_history(reasoning)
    canonical_before = [message.model_dump(mode="json") for message in messages]

    payload = _payload(_provider(model=model), messages)

    expected = reasoning if is_preserved else ""
    assert payload["messages"][0]["reasoning_content"] == expected
    assert [message.model_dump(mode="json") for message in messages] == canonical_before


def test_tokenrhythm_v4_tool_reasoning_missing_value_is_sent_as_empty() -> None:
    payload = _payload(_provider(), _tool_history(None))

    assert payload["messages"][0]["reasoning_content"] == ""


def test_tokenrhythm_projection_does_not_emit_transport_withheld_metric() -> None:
    reasoning = _reasoning_with_utf16_units(
        "ascii",
        TOKENRHYTHM_REASONING_LIMIT_UTF16_UNITS + 1,
    )

    with structlog.testing.capture_logs() as logs:
        payload = _payload(_provider(), _tool_history(reasoning))

    assert payload["messages"][0]["reasoning_content"] == ""
    assert not any(
        event.get("event") == "provider.reasoning_content_withheld"
        for event in logs
    )


@pytest.mark.parametrize(
    ("model", "thinking_level", "expected_effort"),
    (
        ("deepseek-v4-flash", ThinkingLevel.MINIMAL, "low"),
        ("deepseek-v4-flash", ThinkingLevel.LOW, "low"),
        ("deepseek-v4-flash", ThinkingLevel.MEDIUM, "high"),
        ("deepseek-v4-flash", ThinkingLevel.HIGH, "high"),
        ("deepseek-v4-flash", ThinkingLevel.XHIGH, "high"),
        ("deepseek-v4-flash", ThinkingLevel.ADAPTIVE, "high"),
        ("deepseek-v4-flash-0731", ThinkingLevel.MINIMAL, "low"),
        ("deepseek-v4-flash-0731", ThinkingLevel.LOW, "low"),
        ("deepseek-v4-flash-0731", ThinkingLevel.XHIGH, "high"),
        ("deepseek-v4-pro", ThinkingLevel.MINIMAL, "high"),
        ("deepseek-v4-pro", ThinkingLevel.LOW, "high"),
        ("deepseek-v4-pro", ThinkingLevel.XHIGH, "high"),
        ("deepseek-v4-pro", ThinkingLevel.ADAPTIVE, "high"),
        ("deepseek-v4-pro-0813", ThinkingLevel.MINIMAL, "low"),
        ("deepseek-v4-pro-0813", ThinkingLevel.LOW, "low"),
        ("deepseek-v4-pro-0813", ThinkingLevel.MEDIUM, "high"),
        ("deepseek-v4-pro-0813", ThinkingLevel.HIGH, "high"),
        ("deepseek-v4-pro-0813", ThinkingLevel.XHIGH, "high"),
        ("deepseek-v4-pro-0813", ThinkingLevel.ADAPTIVE, "high"),
        ("deepseek-v4-flash", None, "high"),
        ("tokenrhythm/deepseek-v4-flash", ThinkingLevel.LOW, "low"),
        ("tokenrhythm/deepseek-v4-flash-0731", ThinkingLevel.LOW, "low"),
        ("tokenrhythm/deepseek-v4-pro", ThinkingLevel.LOW, "high"),
        ("tokenrhythm/deepseek-v4-pro-0813", ThinkingLevel.MINIMAL, "low"),
        ("tokenrhythm/deepseek-v4-pro-0813", ThinkingLevel.LOW, "low"),
        ("tokenrhythm/deepseek-v4-pro-0813", ThinkingLevel.HIGH, "high"),
    ),
)
def test_tokenrhythm_v4_thinking_uses_conservative_model_effort_mapping(
    model: str,
    thinking_level: ThinkingLevel | None,
    expected_effort: str,
) -> None:
    payload = _payload(
        _provider(model=model),
        [Message(role="user", content="Think about this.")],
        config=_config(thinking=True, thinking_level=thinking_level),
    )

    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == expected_effort


@pytest.mark.parametrize("model", TOKENRHYTHM_BARE_V4_MODELS)
def test_tokenrhythm_v4_thinking_off_sends_explicit_disabled(model: str) -> None:
    payload = _payload(
        _provider(model=model),
        [Message(role="user", content="Answer directly.")],
        config=_config(thinking=False, thinking_level=ThinkingLevel.OFF),
    )

    assert payload["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in payload


def test_tokenrhythm_v4_unspecified_thinking_keeps_provider_default() -> None:
    payload = _payload(
        _provider(),
        [Message(role="user", content="Use the provider default.")],
        config=_config(thinking=False, thinking_level=None),
    )

    assert "thinking" not in payload
    assert "reasoning_effort" not in payload


def test_tokenrhythm_v4_runtime_thinking_fallback_sends_explicit_disabled() -> None:
    fallback_config = _chat_config_with_thinking_disabled(
        _config(thinking=True, thinking_level=ThinkingLevel.HIGH)
    )

    payload = _payload(
        _provider(),
        [Message(role="user", content="Finish without more reasoning.")],
        config=fallback_config,
    )

    assert payload["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in payload


def test_tokenrhythm_v4_unspecified_thinking_degrades_required_tool_choice() -> None:
    payload = _payload(
        _provider(),
        [Message(role="user", content="Use a tool.")],
        config=_config(
            thinking=False,
            thinking_level=None,
            tool_choice="required",
        ),
        tools=[LOOKUP_TOOL],
    )

    assert payload["tool_choice"] == "auto"
    assert "thinking" not in payload
    assert "reasoning_effort" not in payload


@pytest.mark.parametrize("model", TOKENRHYTHM_0813_MODELS)
@pytest.mark.parametrize(
    ("thinking", "thinking_level", "expected_thinking", "expected_effort"),
    (
        (True, ThinkingLevel.LOW, {"type": "enabled"}, "low"),
        (True, ThinkingLevel.HIGH, {"type": "enabled"}, "high"),
        (False, ThinkingLevel.OFF, {"type": "disabled"}, None),
        (False, None, None, None),
    ),
)
def test_tokenrhythm_0813_degrades_required_in_every_thinking_state(
    model: str,
    thinking: bool,
    thinking_level: ThinkingLevel | None,
    expected_thinking: dict[str, str] | None,
    expected_effort: str | None,
) -> None:
    payload = _payload(
        _provider(model=model),
        [Message(role="user", content="Use a tool.")],
        config=_config(
            thinking=thinking,
            thinking_level=thinking_level,
            tool_choice="required",
        ),
        tools=[LOOKUP_TOOL],
    )

    assert payload["tool_choice"] == "auto"
    if expected_thinking is None:
        assert "thinking" not in payload
    else:
        assert payload["thinking"] == expected_thinking
    if expected_effort is None:
        assert "reasoning_effort" not in payload
    else:
        assert payload["reasoning_effort"] == expected_effort


def test_tokenrhythm_generic_pro_explicit_off_keeps_required_tool_choice() -> None:
    payload = _payload(
        _provider(model="deepseek-v4-pro"),
        [Message(role="user", content="Use a tool.")],
        config=_config(
            thinking=False,
            thinking_level=ThinkingLevel.OFF,
            tool_choice="required",
        ),
        tools=[LOOKUP_TOOL],
    )

    assert payload["tool_choice"] == "required"
    assert payload["thinking"] == {"type": "disabled"}


def test_tokenrhythm_v4_unspecified_thinking_named_pin_disables_thinking() -> None:
    named_pin = {"type": "function", "function": {"name": "lookup"}}
    payload = _payload(
        _provider(),
        [Message(role="user", content="Call the selected tool.")],
        config=_config(
            thinking=False,
            thinking_level=None,
            tool_choice=named_pin,
        ),
        tools=[LOOKUP_TOOL],
    )

    assert payload["tool_choice"] == named_pin
    assert payload["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in payload


@pytest.mark.parametrize("model", TOKENRHYTHM_0813_MODELS)
@pytest.mark.parametrize(
    ("thinking", "thinking_level"),
    (
        (True, ThinkingLevel.LOW),
        (False, ThinkingLevel.OFF),
        (False, None),
    ),
)
def test_tokenrhythm_0813_named_pin_wins_in_every_thinking_state(
    model: str,
    thinking: bool,
    thinking_level: ThinkingLevel | None,
) -> None:
    named_pin = {"type": "function", "function": {"name": "lookup"}}
    payload = _payload(
        _provider(model=model),
        [Message(role="user", content="Call the selected tool.")],
        config=_config(
            thinking=thinking,
            thinking_level=thinking_level,
            tool_choice=named_pin,
        ),
        tools=[LOOKUP_TOOL],
    )

    assert payload["tool_choice"] == named_pin
    assert payload["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in payload


@pytest.mark.parametrize("tool_choice", ("auto", "none"))
def test_tokenrhythm_v4_thinking_preserves_supported_tool_choices(
    tool_choice: str,
) -> None:
    payload = _payload(
        _provider(),
        [Message(role="user", content="Use a tool if appropriate.")],
        config=_config(tool_choice=tool_choice),
        tools=[LOOKUP_TOOL],
    )

    assert payload["tool_choice"] == tool_choice
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == "high"


def test_tokenrhythm_v4_thinking_degrades_required_tool_choice_to_auto() -> None:
    payload = _payload(
        _provider(),
        [Message(role="user", content="Use a tool.")],
        config=_config(tool_choice="required"),
        tools=[LOOKUP_TOOL],
    )

    assert payload["tool_choice"] == "auto"
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == "high"


def test_tokenrhythm_v4_named_tool_pin_wins_over_thinking() -> None:
    named_pin = {"type": "function", "function": {"name": "lookup"}}
    payload = _payload(
        _provider(),
        [Message(role="user", content="Call the selected tool.")],
        config=_config(tool_choice=named_pin),
        tools=[LOOKUP_TOOL],
    )

    assert payload["tool_choice"] == named_pin
    assert payload["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in payload


def test_tokenrhythm_0813_required_rewrite_does_not_retry_reactively(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            400,
            headers={"content-type": "application/json"},
            json={
                "error": {
                    "code": "MODEL_TOOL_CHOICE_NOT_SUPPORTED",
                    "message": "synthetic selector rejection",
                }
            },
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

    async def run() -> list[ErrorEvent]:
        return [
            event
            async for event in _provider(model="deepseek-v4-pro-0813").chat(
                [Message(role="user", content="Use a tool.")],
                tools=[LOOKUP_TOOL],
                config=_config(
                    thinking=False,
                    thinking_level=ThinkingLevel.OFF,
                    tool_choice="required",
                ),
            )
            if isinstance(event, ErrorEvent)
        ]

    errors = asyncio.run(run())

    assert len(requests) == 1
    assert requests[0]["tool_choice"] == "auto"
    assert len(errors) == 1


def test_tokenrhythm_field_limit_400_is_not_retried_reactively(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            400,
            headers={"content-type": "application/json"},
            json={
                "error": {
                    "code": "BAD_REQUEST",
                    "message": "messages.0.reasoning_content exceeds provider field limit",
                }
            },
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
    private_marker = "synthetic-private-reasoning-marker"
    reasoning = private_marker + "a" * (
        TOKENRHYTHM_REASONING_LIMIT_UTF16_UNITS + 1 - len(private_marker)
    )
    messages = _tool_history(reasoning)

    async def run() -> list[ErrorEvent]:
        errors: list[ErrorEvent] = []
        async for event in _provider().chat(messages, config=_config()):
            if isinstance(event, ErrorEvent):
                errors.append(event)
        return errors

    with structlog.testing.capture_logs() as logs:
        errors = asyncio.run(run())

    assert len(requests) == 1
    assert requests[0]["messages"][0]["reasoning_content"] == ""
    assert len(errors) == 1
    withheld = [
        event
        for event in logs
        if event.get("event") == "provider.reasoning_content_withheld"
    ]
    assert withheld == [
        {
            "event": "provider.reasoning_content_withheld",
            "log_level": "info",
            "provider": "tokenrhythm",
            "model": "deepseek-v4-flash",
            "reason": "reasoning_content_limit",
            "withheld_count": 1,
            "max_observed_utf16_units": 50_001,
            "limit_utf16_units": 50_000,
        }
    ]
    assert private_marker not in json.dumps(logs, ensure_ascii=False)


def test_budget_rejection_does_not_report_a_transport_withheld_metric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ForbiddenClient:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("budget-limited request must not reach transport")

    monkeypatch.setattr(
        "opensquilla.provider.openai.httpx.AsyncClient",
        ForbiddenClient,
    )
    reasoning = _reasoning_with_utf16_units(
        "ascii",
        TOKENRHYTHM_REASONING_LIMIT_UTF16_UNITS + 1,
    )
    config = _config()
    config.provider_request_max_chars = 100

    async def run() -> list[ErrorEvent]:
        return [
            event
            async for event in _provider().chat(
                _tool_history(reasoning),
                config=config,
            )
            if isinstance(event, ErrorEvent)
        ]

    with structlog.testing.capture_logs() as logs:
        errors = asyncio.run(run())

    assert len(errors) == 1
    assert errors[0].code == "provider_request_budget_exhausted"
    assert not any(
        event.get("event") == "provider.reasoning_content_withheld"
        for event in logs
    )


def test_retained_withheld_metric_skips_ambiguous_duplicate_lengths() -> None:
    message = {
        "role": "assistant",
        "content": None,
        "reasoning_content": "",
        "tool_calls": [
            {
                "id": "duplicate_call_id",
                "type": "function",
                "function": {"name": "lookup", "arguments": "{}"},
            }
        ],
    }
    signature = _reasoning_replay_signature(message)
    stats = _ReasoningReplayStats(
        replay_candidates=[
            (signature, TOKENRHYTHM_REASONING_LIMIT_UTF16_UNITS + 1),
            (signature, TOKENRHYTHM_REASONING_LIMIT_UTF16_UNITS + 2),
        ]
    )
    payload = {"messages": [message]}

    assert _retained_reasoning_replay_units(payload, stats) == []


def test_retained_withheld_metric_counts_identical_duplicate_lengths_once() -> None:
    message = {
        "role": "assistant",
        "content": None,
        "reasoning_content": "",
        "tool_calls": [
            {
                "id": "duplicate_call_id",
                "type": "function",
                "function": {"name": "lookup", "arguments": "{}"},
            }
        ],
    }
    signature = _reasoning_replay_signature(message)
    stats = _ReasoningReplayStats(
        replay_candidates=[
            (signature, TOKENRHYTHM_REASONING_LIMIT_UTF16_UNITS + 1),
            (signature, TOKENRHYTHM_REASONING_LIMIT_UTF16_UNITS + 1),
        ]
    )

    assert _retained_reasoning_replay_units({"messages": [message]}, stats) == [
        50_001
    ]


def test_retained_withheld_metric_does_not_claim_ambiguous_natural_empty() -> None:
    message = {
        "role": "assistant",
        "content": None,
        "reasoning_content": "",
        "tool_calls": [
            {
                "id": "reused_call_id",
                "type": "function",
                "function": {"name": "lookup", "arguments": "{}"},
            }
        ],
    }
    signature = _reasoning_replay_signature(message)
    stats = _ReasoningReplayStats(
        replay_candidates=[
            (signature, TOKENRHYTHM_REASONING_LIMIT_UTF16_UNITS + 1),
            (signature, None),
        ]
    )

    assert _retained_reasoning_replay_units({"messages": [message]}, stats) == []
