from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from opensquilla.engine.agent import Agent
from opensquilla.engine.runtime import TurnRunner
from opensquilla.gateway.config import GatewayConfig
from opensquilla.provider.model_catalog import ModelCatalog
from opensquilla.provider.openai import OpenAIProvider
from opensquilla.provider.selector import ModelSelector, ProviderConfig, SelectorConfig
from opensquilla.provider.types import ChatConfig, DoneEvent, Message, TextDeltaEvent
from opensquilla.tools.types import CallerKind, ToolContext


@pytest.mark.parametrize(
    ("configured_provider", "configured_output", "expected_durable_output"),
    [
        ("openai", 8192, 8192),
        ("openai", 0, 128000),
        ("openrouter", 8192, 128000),
    ],
)
async def test_routed_turn_binds_base_consumer_generation_config(
    monkeypatch: pytest.MonkeyPatch,
    configured_provider: str,
    configured_output: int,
    expected_durable_output: int,
) -> None:
    requests: list[ChatConfig] = []
    agents: list[Agent] = []

    class SyntheticProvider(OpenAIProvider):
        async def chat(
            self,
            messages: list[Message],
            tools: Any = None,
            config: ChatConfig | None = None,
        ) -> AsyncIterator[Any]:
            assert config is not None
            requests.append(config)
            yield TextDeltaEvent(text="ok")
            yield DoneEvent(stop_reason="stop", input_tokens=1, output_tokens=1)

    async def route(turn: Any) -> Any:
        turn.model = "synthetic-route"
        turn.metadata.update({
            "routing_applied": True,
            "routed_model": "synthetic-route",
        })
        return turn

    original_bind = Agent.bind_durable_consumer

    def capture_bind(agent: Agent, **kwargs: Any) -> None:
        agents.append(agent)
        original_bind(agent, **kwargs)

    catalog = ModelCatalog()
    catalog.set_user_overrides({
        "openai/synthetic-base": {"context_window": 200000, "max_output_tokens": 128000},
        "openai/synthetic-route": {"context_window": 128000, "max_output_tokens": 64000},
    })
    config = GatewayConfig(
        llm={
            "provider": configured_provider,
            "model": "synthetic-base",
            "api_key": "synthetic-offline",
            "max_tokens": configured_output,
            "thinking": "off",
        },
        squilla_router={"enabled": True},
        llm_ensemble={"enabled": False},
        agent_max_provider_retries=0,
    )
    selector = ModelSelector(SelectorConfig(primary=ProviderConfig(
        provider="openai",
        model="synthetic-base",
        api_key="synthetic-offline",
        base_url="https://example.invalid/v1",
    )))
    monkeypatch.setattr("opensquilla.engine.steps.apply_squilla_router", route)
    monkeypatch.setattr(
        ModelSelector,
        "resolve",
        lambda self: SyntheticProvider(
            api_key="synthetic-offline",
            model=self.current_config.model,
            base_url="https://example.invalid/v1",
        ),
    )
    monkeypatch.setattr(Agent, "bind_durable_consumer", capture_bind)
    runner = TurnRunner(provider_selector=selector, config=config, model_catalog=catalog)
    events = [event async for event in runner.run(
        "Synthetic request",
        "agent:main:stable-budget",
        tool_context=ToolContext(is_owner=True, caller_kind=CallerKind.CLI),
        no_memory_capture=True,
    )]

    assert not [event for event in events if getattr(event, "kind", "") == "error"]
    assert len(requests) == len(agents) == 1
    assert requests[0].max_tokens == (configured_output or 64000)
    agent = agents[0]
    assert agent._durable_consumer_model_id == "synthetic-base"
    assert agent._durable_consumer_max_output_tokens == expected_durable_output
    projection = agent._project_durable_consumer_final_request(
        [Message(role="user", content="continue")],
        tools=None,
        active_config=requests[0],
    )
    assert projection is not None
    assert projection.payload["max_tokens"] == expected_durable_output
    assert projection.proof["raw_proof_token_budget"] == (
        200000 - expected_durable_output - 20000
    )
