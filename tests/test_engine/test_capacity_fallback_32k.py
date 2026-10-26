"""Capacity failover uses the authorized chain after bounded local recovery."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy

import httpx
import pytest

from opensquilla.engine import Agent, AgentConfig, ToolResult
from opensquilla.engine.runtime import _SelectorFallbackProvider
from opensquilla.engine.types import CompactionEvent, DoneEvent, ErrorEvent
from opensquilla.provider import ChatConfig, ModelCapabilities, OpenAIProvider
from opensquilla.provider.selector import ModelSelector, ProviderConfig, SelectorConfig
from tests.test_engine.test_native_overflow_recovery import (
    CURRENT,
    NativeOverflowProvider,
    setup_case,
)


class CapacityProvider(NativeOverflowProvider):
    async def chat(self, messages, tools=None, config=None):
        projection = self.project_final_request(messages, tools, config)
        self.calls.append((deepcopy(messages), projection, config))
        async for event in OpenAIProvider.chat(self, messages, tools=tools, config=config):
            yield event


def wrap(monkeypatch, primary, *, same_window=False, output=2048):
    primary._model = "capacity-primary"
    fallback = NativeOverflowProvider()
    fallback._model = "capacity-fallback"
    configs = [ProviderConfig(provider="openrouter", model=model, api_key="synthetic-unused")
               for model in (primary.model, fallback.model)]
    providers = {primary.model: primary, fallback.model: fallback}
    monkeypatch.setattr("opensquilla.provider.selector._build_provider",
                        lambda config: providers[config.model])
    monkeypatch.setattr("opensquilla.engine.runtime.resolve_effective_context_window",
                        lambda _catalog, model, **kwargs: (
                            kwargs.get("global_override") or (
                                32768 if model == primary.model or same_window else 131072
                            ), "catalog",
                        ))
    selector = ModelSelector(SelectorConfig(primary=configs[0], fallbacks=[configs[1]]))
    metadata = {}
    wrapper = _SelectorFallbackProvider(selector.resolve(), selector, turn_metadata=metadata)
    wrapper.configure_fallback_deployment_limits([
        (configs[0], 32768, output, ModelCapabilities(supports_tools=True)),
        (configs[1], 32768 if same_window else 131072, 2048,
         ModelCapabilities(supports_tools=True)),
    ])
    return wrapper, fallback, metadata, configs


def transport(monkeypatch, *, reject_fallback=False, tool_once=False):
    physical = []

    def handle(request):
        payload = json.loads(request.content)
        physical.append(payload)
        if tool_once and len(physical) == 1:
            body = {"choices": [{"delta": {"tool_calls": [{
                "index": 0, "id": "new-call", "type": "function",
                "function": {"name": "lookup", "arguments": '{"target":"west"}'},
            }]}, "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 10}}
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"},
                content=(f"data: {json.dumps(body)}\n\ndata: [DONE]\n\n").encode(),
            )
        if payload["model"] == "capacity-primary" or reject_fallback:
            return httpx.Response(400, json={"error": {
                "message": "maximum context length exceeded", "code": "context_length_exceeded",
                "type": "invalid_request_error",
            }})
        body = {"choices": [{"delta": {"content": "Continued safely."}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 10}}
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=(f"data: {json.dumps(body)}\n\ndata: [DONE]\n\n").encode())

    client = httpx.AsyncClient
    monkeypatch.setattr(
        "opensquilla.provider.openai.httpx.AsyncClient",
        lambda **kwargs: client(**{**kwargs, "transport": httpx.MockTransport(handle)}),
    )
    return physical


@pytest.mark.asyncio
@pytest.mark.parametrize("minimal,reject_fallback", [(False, False), (True, False), (False, True)])
async def test_native_overflow_exhausts_local_window_before_authorized_capacity_fallback(
    monkeypatch, minimal, reject_fallback,
):
    agent, primary, history, _old_physical = setup_case(monkeypatch)
    # Restore the original client before installing this test's transport.
    monkeypatch.undo()
    # 32K is the primary deployment's real capacity, not an operator ceiling
    # which must constrain every fallback as well.
    agent.config.context_window_tokens_global_override = 0
    if minimal:
        history = []
        agent.set_history(history)
    wrapper, fallback, metadata, _configs = wrap(monkeypatch, primary)
    physical = transport(monkeypatch, reject_fallback=reject_fallback)
    agent.provider = wrapper
    events = [event async for event in agent.run_turn(CURRENT)]
    primary_attempts = 1 if minimal else 1 + agent.config.max_overflow_retries
    assert [call["model"] for call in physical] == (
        ["capacity-primary"] * primary_attempts + ["capacity-fallback"]
    )
    if not minimal:
        sizes = [len(json.dumps(payload)) for payload in physical[:primary_attempts]]
        assert all(after < before for before, after in zip(sizes, sizes[1:]))
    assert metadata["executed_model"] == "capacity-fallback"
    assert metadata["router_fallback_reason"] == "context_overflow_after_recovery"
    assert not any(isinstance(event, CompactionEvent) for event in events)
    assert any(isinstance(event, ErrorEvent if reject_fallback else DoneEvent) for event in events)
    assert bool([event for event in events if isinstance(event, ErrorEvent)]) == reject_fallback
    assert any(isinstance(event, DoneEvent) and event.text == "Continued safely."
               for event in events) is (not reject_fallback)
    assert primary.calls[-1][2].turn_deadline_at_monotonic == (
        fallback.calls[0][2].turn_deadline_at_monotonic
    )
    assert fallback.calls[0][2].provider_context_window_tokens == 131072
    assert fallback.calls[0][1].fits
    assert physical[-1] == fallback.calls[0][1].payload
    assert CURRENT in str(physical[-1])
    assert agent.history_snapshot()[:len(history)] == history


@pytest.mark.asyncio
async def test_same_total_window_dynamic_output_reaches_agent_capacity_fallback(
    monkeypatch,
):
    # Keep this capacity boundary offline and valid without an optional tokenizer.
    monkeypatch.setattr("opensquilla.token_estimation._get_encoding", lambda: None)
    primary = CapacityProvider()
    wrapper, fallback, metadata, _configs = wrap(
        monkeypatch, primary, same_window=True, output=24576,
    )
    physical = transport(monkeypatch)
    # The primary's dynamic output now makes the input locally admissible.
    # Its actual server overflow must reach the Agent's bounded recovery and
    # authorized fallback, rather than assuming the wrapper rejects it locally.
    agent = Agent(provider=wrapper, config=AgentConfig(
        context_window_tokens=32768, context_window_known=True, max_tokens=24576,
        model_capabilities=ModelCapabilities(supports_tools=True),
        max_provider_retries=0, max_overflow_retries=2, timeout=30, iteration_timeout=30,
    ))
    current = "Current protected request. " + "word " * 10000
    events = [event async for event in agent.run_turn(current)]
    assert [call["model"] for call in physical] == ["capacity-primary", "capacity-fallback"]
    assert 2048 < physical[0]["max_tokens"] < 24576
    assert metadata["router_fallback_reason"] == "context_overflow_after_recovery"
    assert fallback.calls[0][2].max_tokens == 2048
    assert fallback.calls[0][2].provider_context_window_tokens == 32768
    assert physical[-1] == fallback.calls[0][1].payload and fallback.calls[0][1].fits
    assert current in json.dumps(physical[-1])
    assert any(isinstance(event, DoneEvent) and event.text == "Continued safely."
               for event in events)
    assert not any(isinstance(event, (ErrorEvent, CompactionEvent)) for event in events)
    assert primary.calls[0][2].turn_deadline_at_monotonic == (
        fallback.calls[0][2].turn_deadline_at_monotonic
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("guard", [
    "deadline", "single_attempt", "vision", "no_gain", "global_override", "tools",
])
async def test_capacity_fallback_keeps_existing_authority_and_attempt_guards(monkeypatch, guard):
    wrapper, fallback, _metadata, configs = wrap(
        monkeypatch, NativeOverflowProvider(), same_window=guard == "no_gain",
    )
    if guard == "tools":
        wrapper.configure_fallback_deployment_limits([
            (configs[0], 32768, 2048, ModelCapabilities(supports_tools=True)),
            (configs[1], 131072, 2048, ModelCapabilities(supports_tools=False)),
        ])
    config = ChatConfig(max_tokens=2048, provider_context_window_tokens=32768,
                        physical_attempt_limit=1 if guard == "single_attempt" else 0,
                        context_window_tokens_global_override=(
                            32768 if guard == "global_override" else 0
                        ),
                        turn_deadline_at_monotonic=(
                            asyncio.get_running_loop().time() - 1 if guard == "deadline" else None
                        ))
    assert not wrapper.fallback_after_context_overflow(
        config, requires_vision=guard == "vision", requires_tools=guard == "tools",
    )
    assert wrapper.active_model_id == "capacity-primary" and not fallback.calls


@pytest.mark.asyncio
async def test_capacity_fallback_preserves_completed_tool_once(monkeypatch):
    agent, primary, _history, _old_physical = setup_case(monkeypatch, tool_history=True)
    monkeypatch.undo()
    agent.config.context_window_tokens_global_override = 0
    wrapper, fallback, _metadata, _configs = wrap(monkeypatch, primary)
    physical = transport(monkeypatch, tool_once=True)
    agent.provider = wrapper
    calls = []

    async def lookup(call):
        calls.append(call.tool_use_id)
        return ToolResult(tool_use_id=call.tool_use_id, tool_name=call.tool_name,
                          content="WEST_VERIFIED_ONCE")

    agent.tool_handler = lookup
    events = [event async for event in agent.run_turn(CURRENT)]
    assert calls == ["new-call"]
    primary_attempts = 2 + agent.config.max_overflow_retries
    assert [call["model"] for call in physical] == (
        ["capacity-primary"] * primary_attempts + ["capacity-fallback"]
    )
    sizes = [len(json.dumps(payload)) for payload in physical[1:primary_attempts]]
    assert all(after < before for before, after in zip(sizes, sizes[1:]))
    assert "WEST_VERIFIED_ONCE" in str(physical[-1])
    assert fallback.calls[0][2].turn_deadline_at_monotonic == (
        primary.calls[0][2].turn_deadline_at_monotonic
    )
    assert any(isinstance(event, DoneEvent) for event in events)
    assert not any(isinstance(event, (ErrorEvent, CompactionEvent)) for event in events)
    assert physical[-1] == fallback.calls[0][1].payload and fallback.calls[0][1].fits
    assert any(message.get("tool_call_id") == "new-call"
               for message in physical[-1]["messages"])


@pytest.mark.asyncio
async def test_capacity_fallback_does_not_restart_exhausted_chain(monkeypatch):
    wrapper, _fallback, _metadata, _configs = wrap(monkeypatch, NativeOverflowProvider())
    config = ChatConfig(max_tokens=2048, provider_context_window_tokens=32768)
    assert wrapper.fallback_after_context_overflow(config)
    assert not wrapper.fallback_after_context_overflow(config)
    assert wrapper.active_model_id == "capacity-fallback"


@pytest.mark.asyncio
async def test_capacity_fallback_cannot_replay_visible_output(monkeypatch):
    agent, primary, _history, _old_physical = setup_case(monkeypatch, partial=True)
    monkeypatch.undo()
    agent.config.context_window_tokens_global_override = 0
    wrapper, fallback, _metadata, _configs = wrap(monkeypatch, primary)
    agent.provider = wrapper
    events = [event async for event in agent.run_turn(CURRENT)]
    assert len(primary.calls) == 1 and not fallback.calls
    assert any(isinstance(event, ErrorEvent) for event in events)


@pytest.mark.asyncio
async def test_capacity_fallback_does_not_reset_turn_call_budget(monkeypatch):
    agent, primary, _history, _old_physical = setup_case(monkeypatch)
    monkeypatch.undo()
    agent.config.context_window_tokens_global_override = 0
    agent.config.max_turn_llm_calls = 2
    wrapper, fallback, _metadata, _configs = wrap(monkeypatch, primary)
    physical = transport(monkeypatch)
    agent.provider = wrapper
    events = [event async for event in agent.run_turn(CURRENT)]
    assert len(physical) == 2 and not fallback.calls
    assert not any(isinstance(event, DoneEvent) for event in events)
