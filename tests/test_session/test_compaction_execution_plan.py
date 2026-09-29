from __future__ import annotations

from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.provider.openai import OpenAIProvider
from opensquilla.provider.selector import ProviderConfig, build_provider_from_config
from opensquilla.provider.types import (
    ChatConfig,
    DoneEvent,
    Message,
    ModelCapabilities,
    ProviderReplayState,
    TextDeltaEvent,
)
from opensquilla.session.compaction import (
    CompactionRequestContext,
    call_compaction_provider,
)
from opensquilla.session.compaction_deployment import (
    CompactionDeploymentIdentity,
    resolve_compaction_execution_plan,
)


@dataclass
class _BuiltProvider:
    config: ProviderConfig

    def chat(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def __repr__(self) -> str:
        return f"_BuiltProvider(api_key={self.config.api_key!r})"


@pytest.fixture
def built_configs(monkeypatch: pytest.MonkeyPatch) -> list[ProviderConfig]:
    captured: list[ProviderConfig] = []

    def build(config: ProviderConfig) -> _BuiltProvider:
        captured.append(config)
        return _BuiltProvider(config)

    monkeypatch.setattr(
        "opensquilla.session.compaction_deployment.build_provider_from_config",
        build,
    )
    return captured


def _config(
    provider: str,
    model: str,
    *,
    api_key: str = "",
) -> ProviderConfig:
    return ProviderConfig(
        provider=provider,
        model=model,
        api_key=api_key,
        provider_routing={"order": "latency"},
    )


def test_suffix_uses_current_physical_model_instead_of_previous_or_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = replace(
        _config("openrouter", "synthetic/model-b", api_key="synthetic-key"),
        provider_routing={"synthetic/model-b": "synthetic-provider"},
    )
    current_provider = build_provider_from_config(current)
    assert isinstance(current_provider, OpenAIProvider)

    def unexpected_resolution(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("suffix must use the already resolved current deployment")

    monkeypatch.setattr(
        "opensquilla.provider.deployment.resolve_provider_deployment",
        unexpected_resolution,
    )
    plan = resolve_compaction_execution_plan(
        app_config=None,
        active_provider=current_provider,
        active_provider_config=current,
        previous_deployment_identities=(
            CompactionDeploymentIdentity("openrouter", "synthetic/model-a"),
        ),
        fallback_provider_configs=(_config("openrouter", "synthetic/fallback"),),
        compaction_config=SimpleNamespace(provider="openai", model="synthetic/summary"),
        context_window_tokens=64_000,
        active_only=True,
    )

    assert plan is not None
    assert len(plan.candidates) == 1
    # The bound adapter already resolved the registry endpoint. An incomplete
    # config must not replace that physical connection for an auxiliary call.
    assert plan.primary.provider is current_provider
    assert isinstance(plan.primary.provider, OpenAIProvider)
    messages = [Message(role="user", content="Summarize the selected history.")]
    chat_config = ChatConfig(max_tokens=4096)
    projection = plan.primary.provider.project_final_request(messages, config=chat_config)
    parent_projection = current_provider.project_final_request(messages, config=chat_config)
    assert projection.payload["model"] == current.model
    assert projection.payload["provider"] == parent_projection.payload["provider"]


@pytest.mark.parametrize("active_only", [False, True], ids=["legacy-flag-off", "legacy-flag-on"])
@pytest.mark.parametrize("replay", [False, True], ids=["replay-off", "replay-on"])
def test_suffix_preserves_parent_replay_in_final_payload(
    active_only: bool,
    replay: bool,
) -> None:
    current = ProviderConfig(
        provider="openrouter",
        model="anthropic/synthetic-reasoning-model",
        api_key="synthetic-key",
        replay_provider_state=replay,
    )
    parent = build_provider_from_config(current)
    assert isinstance(parent, OpenAIProvider)
    messages = [
        Message(role="user", content="Synthetic source history."),
        Message(
            role="assistant",
            content="Synthetic answer.",
            provider_replay=ProviderReplayState(
                protocol="openai_chat_completions",
                source=parent._replay_source,
                model=current.model,
                reasoning_details=[
                    {
                        "type": "reasoning.encrypted",
                        "data": "synthetic-opaque-state",
                        "id": "synthetic-reasoning-1",
                        "format": "anthropic-claude-v1",
                        "index": 0,
                    },
                ],
            ),
        ),
    ]
    chat_config = ChatConfig(
        system="Synthetic shared system.",
        max_tokens=8192,
        thinking=True,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="openrouter",
        ),
    )
    plan = resolve_compaction_execution_plan(
        app_config=None,
        active_provider=parent,
        active_provider_config=current,
        context_window_tokens=64_000,
        active_only=active_only,
    )
    assert plan is not None
    assert isinstance(plan.primary.provider, OpenAIProvider)
    before = parent.project_final_request(messages, config=chat_config).payload
    after = plan.primary.provider.project_final_request(
        [*messages, Message(role="user", content="Summarize the selected history.")],
        config=chat_config,
    ).payload

    before_assistant = next(msg for msg in before["messages"] if msg["role"] == "assistant")
    after_assistant = next(msg for msg in after["messages"] if msg["role"] == "assistant")
    assert ("reasoning_details" in before_assistant) is replay
    assert ("reasoning_details" in after_assistant) is replay
    assert after["messages"][:-1] == before["messages"]
    assert current.replay_provider_state is replay


def test_suffix_uses_only_the_actual_ensemble_aggregator(
    built_configs: list[ProviderConfig],
) -> None:
    aggregator_config = replace(
        _config("anthropic", "aggregator-model"),
        replay_provider_state=False,
    )
    base_config = _config("openai", "routed-base")
    ensemble = SimpleNamespace(
        aggregator=SimpleNamespace(provider_config=aggregator_config, ready=True),
    )

    plan = resolve_compaction_execution_plan(
        app_config=None,
        active_provider=ensemble,
        active_provider_config=base_config,
        previous_deployment_identities=(
            CompactionDeploymentIdentity("openai", "previous-model"),
        ),
        active_only=True,
    )

    assert plan is not None
    assert [(target.model, target.source) for target in plan.candidates] == [
        ("aggregator-model", "ensemble_aggregator"),
    ]
    assert built_configs == [aggregator_config]


@pytest.mark.parametrize("replay", [False, True])
def test_suffix_without_factory_config_preserves_resolved_provider(replay: bool) -> None:
    current = replace(
        _config("openrouter", "synthetic/current-model", api_key="synthetic-key"),
        replay_provider_state=replay,
    )
    provider = build_provider_from_config(current)
    assert isinstance(provider, OpenAIProvider)
    plan = resolve_compaction_execution_plan(
        app_config=None,
        active_provider=provider,
        active_provider_config=None,
        previous_deployment_identities=(
            CompactionDeploymentIdentity("openrouter", "synthetic/previous-model"),
        ),
        active_only=True,
    )

    assert plan is not None
    assert plan.primary.provider is provider
    assert provider._replay_provider_state is replay
    assert plan.primary.model == current.model


@pytest.mark.parametrize("window,accepted", [(16_000, True), (6_000, False)])
async def test_suffix_uses_anthropic_expanded_thinking_budget(
    monkeypatch: pytest.MonkeyPatch,
    window: int,
    accepted: bool,
) -> None:
    from opensquilla.provider.anthropic import AnthropicProvider

    monkeypatch.setenv("OPENSQUILLA_COMPACTION_PROMPT_LAYOUT", "suffix")
    config = _config("anthropic", "claude-sonnet-4-20250514", api_key="synthetic-key")
    plan = resolve_compaction_execution_plan(
        app_config=None,
        active_provider=None,
        active_provider_config=config,
        context_window_tokens=window,
        active_chat_config=ChatConfig(max_tokens=2048, provider_context_window_tokens=window),
        active_only=True,
    )
    assert plan is not None
    provider = plan.primary.provider
    assert isinstance(provider, AnthropicProvider)
    sent_payloads: list[dict[str, Any]] = []

    async def chat(messages, tools=None, config=None):
        sent_payloads.append(provider.project_final_request(messages, tools, config).payload)
        yield TextDeltaEvent(text="Short portable checkpoint.")
        yield DoneEvent(output_tokens=6000)

    monkeypatch.setattr(provider, "chat", chat)
    context = CompactionRequestContext(chat_config=ChatConfig(
        system="Stable system instructions.",
        max_tokens=2048,
        thinking=True,
        thinking_budget_tokens=4096,
        thinking_budget_explicit=True,
    ))
    result = await call_compaction_provider(
        "", "", plan,
        request_context=context,
        source_entries=[{"role": "user", "content": "Synthetic source history."}],
    )

    assert (result == "Short portable checkpoint.") is accepted
    assert context.chat_config.max_tokens == 2048
    if accepted:
        assert sent_payloads[0]["max_tokens"] == 8192
        assert sent_payloads[0]["thinking"] == {"type": "enabled", "budget_tokens": 4096}
    else:
        # The 2048-token ChatConfig would fit, but the actual 8192-token
        # Anthropic generation allowance does not. Refuse before transport.
        assert sent_payloads == []
