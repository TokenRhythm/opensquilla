from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from opensquilla.engine.runtime import _SelectorFallbackProvider
from opensquilla.provider import DoneEvent, ErrorEvent, ProviderActivityEvent, TextDeltaEvent
from opensquilla.provider.selector import ModelSelector, ProviderConfig, SelectorConfig

HIGH_TIER_MODEL = "openrouter/high-tier-region-locked"
MID_TIER_MODEL = "openrouter/mid-tier-available"
LOW_TIER_MODEL = "openrouter/low-tier-available"
BASELINE_MODEL = "openrouter/baseline-available"


class FakeProvider:
    def __init__(
        self,
        provider_name: str,
        model: str,
        events: list[Any],
        calls: list[str],
    ) -> None:
        self.provider_name = provider_name
        self.model = model
        self._events = events
        self._calls = calls

    async def chat(
        self,
        messages: list[Any],
        tools: Any = None,
        config: Any = None,
    ) -> AsyncIterator[Any]:
        self._calls.append(self.model)
        for event in self._events:
            yield event

    async def list_models(self) -> list[Any]:
        return []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "message", "expected_fallback"),
    [
        ("403", "HTTP 403: This model is not available in your region.", True),
        ("404", "HTTP 404: model not found", True),
        ("403", "HTTP 403: forbidden", False),
        ("404", "HTTP 404: not found", False),
    ],
)
async def test_runtime_only_falls_back_with_explicit_model_failure(
    monkeypatch: pytest.MonkeyPatch,
    code: str,
    message: str,
    expected_fallback: bool,
) -> None:
    calls: list[str] = []

    def fake_build_provider(cfg: ProviderConfig) -> FakeProvider:
        if cfg.model == HIGH_TIER_MODEL:
            return FakeProvider(
                cfg.provider,
                cfg.model,
                [ErrorEvent(message=message, code=code)],
                calls,
            )
        return FakeProvider(
            cfg.provider,
            cfg.model,
            [
                TextDeltaEvent(text=f"fallback-response-from:{cfg.model}"),
                DoneEvent(model=cfg.model),
            ],
            calls,
        )

    monkeypatch.setattr("opensquilla.provider.selector._build_provider", fake_build_provider)
    selector = ModelSelector(
        SelectorConfig(
            primary=ProviderConfig(
                provider="openrouter",
                model=BASELINE_MODEL,
                api_key="sk-test",
                base_url="https://openrouter.ai/api",
            )
        )
    )
    selector.override_model_with_fallback_chain(
        HIGH_TIER_MODEL,
        [
            {"tier": "c2", "provider": "openrouter", "model": MID_TIER_MODEL},
            {"tier": "c1", "provider": "openrouter", "model": BASELINE_MODEL},
            {"tier": "c0", "provider": "openrouter", "model": LOW_TIER_MODEL},
        ],
    )
    provider = _SelectorFallbackProvider(selector.resolve(), selector)

    events = [event async for event in provider.chat([{"role": "user", "content": "hi"}])]

    if not expected_fallback:
        # No retry or fallback request may follow an unclassified denial.
        assert calls == [HIGH_TIER_MODEL]
        assert len(events) == 1
        assert isinstance(events[0], ErrorEvent)
        assert events[0].code == code
        return

    assert calls == [HIGH_TIER_MODEL, MID_TIER_MODEL]
    assert [getattr(event, "kind", "") for event in events] == [
        "provider_activity",
        "text_delta",
        "done",
    ]
    activity = events[0]
    assert isinstance(activity, ProviderActivityEvent)
    assert (activity.phase, activity.reason) == ("fallback", "unknown")
    assert activity.retry_attempt == 1
    assert activity.started_at > 0
    assert events[1].text == f"fallback-response-from:{MID_TIER_MODEL}"
    assert events[2].model == MID_TIER_MODEL


@pytest.mark.asyncio
async def test_runtime_keeps_successful_router_model_without_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_build_provider(cfg: ProviderConfig) -> FakeProvider:
        return FakeProvider(
            cfg.provider,
            cfg.model,
            [
                TextDeltaEvent(text=f"primary-response-from:{cfg.model}"),
                DoneEvent(model=cfg.model),
            ],
            calls,
        )

    monkeypatch.setattr("opensquilla.provider.selector._build_provider", fake_build_provider)
    selector = ModelSelector(
        SelectorConfig(
            primary=ProviderConfig(
                provider="openrouter",
                model=BASELINE_MODEL,
                api_key="sk-test",
                base_url="https://openrouter.ai/api",
            )
        )
    )
    selector.override_model_with_fallback_chain(
        HIGH_TIER_MODEL,
        [
            {"tier": "c2", "provider": "openrouter", "model": MID_TIER_MODEL},
            {"tier": "c1", "provider": "openrouter", "model": BASELINE_MODEL},
            {"tier": "c0", "provider": "openrouter", "model": LOW_TIER_MODEL},
        ],
    )
    provider = _SelectorFallbackProvider(selector.resolve(), selector)

    events = [event async for event in provider.chat([{"role": "user", "content": "hi"}])]

    assert calls == [HIGH_TIER_MODEL]
    assert [getattr(event, "kind", "") for event in events] == ["text_delta", "done"]
    assert events[0].text == f"primary-response-from:{HIGH_TIER_MODEL}"
    assert events[1].model == HIGH_TIER_MODEL
