from __future__ import annotations

import httpx
import pytest

from opensquilla.engine.routing.health import ProviderHealthLedger
from opensquilla.engine.runtime import (
    _provider_authority_identity,
    _same_provider_authority,
    _SelectorFallbackProvider,
)
from opensquilla.provider.selector import ModelSelector, ProviderConfig, SelectorConfig
from opensquilla.provider.types import (
    ChatConfig,
    DoneEvent,
    ErrorEvent,
    Message,
    ReasoningDeltaEvent,
    TextDeltaEvent,
    ToolUseStartEvent,
)


class _Provider:
    provider_name = "openai"

    def __init__(self, config, events, calls):
        self.model = config.model
        self.events = events
        self.calls = calls

    async def chat(self, messages, tools=None, config=None):
        self.calls.append(self.model)
        for event in self.events:
            if isinstance(event, Exception):
                raise event
            yield event


def _wrapper(monkeypatch, primary_events, *, same_authority=False, fallback_events=None):
    calls = []
    pool_failures = []
    monkeypatch.setattr(
        "opensquilla.engine.runtime._report_credential_pool_failure",
        lambda _provider, _metadata, event: pool_failures.append(event.code),
    )
    configs = [
        ProviderConfig(
            provider="openai", model="primary", api_key="dummy-key",
            base_url="https://primary.test",
        ),
        ProviderConfig(
            provider="openai", model="secondary", api_key="dummy-key",
            base_url="https://primary.test" if same_authority else "https://secondary.test",
        ),
    ]
    monkeypatch.setattr(
        "opensquilla.provider.selector._build_provider",
        lambda config: _Provider(
            config,
            primary_events if config.model == "primary" else (
                fallback_events if fallback_events is not None else [
                    TextDeltaEvent(text="done"), DoneEvent(model="secondary")
                ]
            ),
            calls,
        ),
    )
    selector = ModelSelector(SelectorConfig(primary=configs[0], fallbacks=configs[1:]))
    health = ProviderHealthLedger(failure_threshold=1)
    return (
        _SelectorFallbackProvider(selector.resolve(), selector, health_ledger=health),
        selector, health, calls, pool_failures,
    )


async def _events(provider, *, managed=True):
    return [
        event async for event in provider.chat(
            [Message(role="user", content="hi")],
            config=ChatConfig(agent_managed_recovery=managed),
        )
    ]


@pytest.mark.parametrize(
    "error",
    [
        ErrorEvent(message="temporary connection failure", code="connection_failed"),
        ErrorEvent(message="rate limit exceeded", code="429", retry_after_s=60),
        httpx.ConnectError("untrusted provider prose"),
    ],
)
async def test_managed_failure_keeps_current_leg_without_health_or_pool_effects(
    monkeypatch, error
) -> None:
    provider, selector, health, calls, pool_failures = _wrapper(monkeypatch, [error])
    for _ in range(4):
        events = await _events(provider)
        errors = [event for event in events if isinstance(event, ErrorEvent)]
        assert len(errors) == 1
        assert errors[0].code in {"connection_failed", "429"}
        if isinstance(error, Exception):
            assert "untrusted provider prose" not in errors[0].message
    # Repeated direct calls without waiting must not bypass the actual 429's
    # Retry-After. Other managed errors still reach this same physical leg.
    physical_calls = 1 if isinstance(error, ErrorEvent) and error.retry_after_s else 4
    assert calls == ["primary"] * physical_calls
    assert selector.current_config.model == "primary"
    assert not health.is_benched("openai", "primary")
    assert pool_failures == []


async def test_managed_failure_discards_uncommitted_tool_frames(monkeypatch) -> None:
    error = ErrorEvent(message="connection failed", code="connection_failed")
    provider, _, _, _, _ = _wrapper(
        monkeypatch, [ToolUseStartEvent(tool_use_id="pending", tool_name="write_file"), error]
    )
    assert await _events(provider) == [error]


@pytest.mark.parametrize("prefix", [TextDeltaEvent(text="partial"), ReasoningDeltaEvent(text="r")])
async def test_visible_content_never_switches_provider(monkeypatch, prefix) -> None:
    error = ErrorEvent(message="connection failed", code="connection_failed")
    provider, selector, _, calls, _ = _wrapper(monkeypatch, [prefix, error])
    events = await _events(provider)
    assert prefix in events and error in events
    assert selector.current_config.model == "primary"
    assert calls == ["primary"]


@pytest.mark.parametrize(
    ("error", "managed"),
    [
        (ErrorEvent(message="connection failed", code="connection_failed"), False),
        (ErrorEvent(message="rate limit exceeded", code="429"), False),
        (ErrorEvent(message="insufficient_quota", code="429"), True),
    ],
)
async def test_direct_calls_and_quota_keep_existing_fallback(monkeypatch, error, managed) -> None:
    provider, selector, _, calls, _ = _wrapper(monkeypatch, [error])
    events = await _events(provider, managed=managed)
    assert calls == ["primary", "secondary"]
    assert selector.current_config.model == "secondary"
    assert any(isinstance(event, DoneEvent) for event in events)


async def test_managed_failure_on_selected_fallback_does_not_bench_it(monkeypatch) -> None:
    connection = ErrorEvent(message="connection failed", code="connection_failed")
    provider, selector, health, calls, pool_failures = _wrapper(
        monkeypatch,
        [ErrorEvent(message="model not found", code="404")],
        fallback_events=[connection],
    )
    events = await _events(provider)
    assert connection in events
    assert calls == ["primary", "secondary"]
    assert selector.current_config.model == "secondary"
    assert not health.is_benched("openai", "secondary")
    assert pool_failures == ["404"]


@pytest.mark.parametrize("same_authority", [False, True])
async def test_final_rate_fallback_honors_retry_after_and_records_failure_once(
    monkeypatch, same_authority
) -> None:
    error = ErrorEvent(message="rate limit exceeded", code="429", retry_after_s=60)
    provider, selector, health, calls, pool_failures = _wrapper(
        monkeypatch, [error], same_authority=same_authority
    )
    await _events(provider)
    assert provider.fallback_after_managed_recovery(error) is (not same_authority)
    assert calls == ["primary"]
    assert selector.current_config.model == ("primary" if same_authority else "secondary")
    assert health.is_benched("openai", "primary")
    assert pool_failures == ["429"]


@pytest.mark.parametrize(
    ("provider", "explicit_url"),
    [
        ("anthropic", "HTTPS://API.ANTHROPIC.COM:443/"),
        ("anthropic", "https://api.anthropic.com/v1/"),
        ("ollama", "HTTP://LOCALHOST:11434/"),
        ("deepseek", "https://api.deepseek.com/v1/"),
        ("openai_responses", "https://api.openai.com/v1/"),
        ("openai_codex", "https://chatgpt.com/"),
    ],
)
def test_authority_uses_registered_adapter_default(provider, explicit_url):
    assert _same_provider_authority(
        ProviderConfig(provider=provider, model="primary", api_key="dummy"),
        ProviderConfig(
            provider=provider, model="secondary", api_key="dummy", base_url=explicit_url
        ),
    )


@pytest.mark.parametrize(
    "base_url", ["https://api.openai.com:invalid", "https://api.openai.com:65536", "https://[bad"]
)
def test_invalid_endpoint_cannot_establish_independent_authority(base_url):
    assert _provider_authority_identity(
        ProviderConfig(provider="openai", model="primary", api_key="dummy", base_url=base_url)
    ) is None


def test_same_endpoint_with_different_auth_headers_keeps_independent_authority():
    native = ProviderConfig(
        "anthropic", "primary", api_key="dummy", base_url="https://gateway.test"
    )
    bearer = ProviderConfig(
        "minimax_cn", "secondary", api_key="dummy", base_url="https://gateway.test"
    )
    native_identity = _provider_authority_identity(native)
    bearer_identity = _provider_authority_identity(bearer)
    assert native_identity.base_url == bearer_identity.base_url
    assert native_identity.auth_header_style == "x-api-key"
    assert bearer_identity.auth_header_style == "bearer"
    assert not _same_provider_authority(native, bearer)
