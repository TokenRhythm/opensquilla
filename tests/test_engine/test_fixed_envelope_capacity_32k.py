"""An oversized fixed envelope cannot be repaired by summarizing its history.

These tests exercise the production adapter projection and selector with local
HTTP transport only. Context limits are 32K and 128K; no model is contacted.
"""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy

import httpx
import pytest

from opensquilla.engine import Agent, AgentConfig
from opensquilla.engine.runtime import _SelectorFallbackProvider
from opensquilla.engine.types import CompactionEvent, DoneEvent, ErrorEvent
from opensquilla.provider import (
    ChatConfig,
    Message,
    ModelCapabilities,
    OpenAIProvider,
    ToolDefinition,
    ToolInputSchema,
)
from opensquilla.provider.selector import ModelSelector, ProviderConfig, SelectorConfig


class ObservedAdapter(OpenAIProvider):
    def __init__(self, model: str) -> None:
        super().__init__(api_key="synthetic-unused", model=model,
                         base_url="https://fixed-envelope.test/v1", provider_kind="openrouter")
        self.calls = []

    async def chat(self, messages, tools=None, config=None):
        self.calls.append((deepcopy(messages), deepcopy(tools), config,
                           self.project_final_request(messages, tools, config)))
        async for event in super().chat(messages, tools=tools, config=config):
            yield event


def fixed_material(kind: str):
    parts = {"system": "Keep the task's rules.", "tools": "Read the designated record.",
             "user": "Report the designated record without changing anything."}
    for part in parts if kind == "combined" else [kind]:
        # Fit 128K with either BPE or the conservative character estimator,
        # while still exceeding 32K after the output reservation.
        parts[part] = f"{part} begin " + "fact " * (
            10000 if kind == "combined" else 34000
        ) + f" {part} end"
    tools = [ToolDefinition(
        name="lookup", description=parts["tools"],
        input_schema=ToolInputSchema(properties={"record": {"type": "string"}},
                                     required=["record"]),
    )]
    messages = [Message(role="user", content=parts["user"])]
    return parts, messages, tools


def setup(monkeypatch, *, authorized: bool, guard: str = ""):
    configs = [ProviderConfig(provider="openrouter", model=name, api_key="synthetic-unused",
                              base_url="https://fixed-envelope.test/v1")
               for name in ("fixed-small", "fixed-large")]
    adapters = {cfg.model: ObservedAdapter(cfg.model) for cfg in configs}
    builds = []

    def build(cfg):
        builds.append(cfg.model)
        return adapters[cfg.model]

    monkeypatch.setattr("opensquilla.provider.selector._build_provider", build)
    monkeypatch.setattr("opensquilla.engine.runtime.resolve_effective_context_window",
                        lambda _catalog, model, **kwargs: (
                            kwargs.get("global_override") or (
                                32768 if model == "fixed-small" else 131072
                            ), "catalog",
                        ))
    selector = ModelSelector(SelectorConfig(
        primary=configs[0], fallbacks=[configs[1]] if authorized else [],
    ))
    metadata = {}
    wrapper = _SelectorFallbackProvider(selector.resolve(), selector, turn_metadata=metadata)
    wrapper.configure_fallback_deployment_limits([
        (cfg, 32768 if cfg.model == "fixed-small" else 131072, 4096,
         ModelCapabilities(supports_tools=not (guard == "tools" and cfg.model == "fixed-large")))
        for cfg in configs[:2 if authorized else 1]
    ])
    physical = []

    def respond(request):
        payload = json.loads(request.content)
        physical.append(payload)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=(
            b'data: {"choices":[{"delta":{"content":"Finished safely."},'
            b'"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
        ))

    original_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: original_client(
        *args, **{**kwargs, "transport": httpx.MockTransport(respond)},
    ))
    return wrapper, adapters, physical, builds, metadata


def chat_config(**updates):
    return ChatConfig(max_tokens=4096, provider_context_window_tokens=32768,
                      active_user_message_index=0, **updates)


@pytest.mark.parametrize("kind", ["system", "tools", "user", "combined"])
@pytest.mark.parametrize("authorized", [False, True])
async def test_fixed_envelope_uses_only_authorized_fitting_target(monkeypatch, kind, authorized):
    wrapper, adapters, physical, builds, metadata = setup(monkeypatch, authorized=authorized)
    parts, messages, tools = fixed_material(kind)
    before = deepcopy((messages, tools))
    deadline = asyncio.get_running_loop().time() + 60
    config = chat_config(system=parts["system"], turn_deadline_at_monotonic=deadline)
    small = adapters["fixed-small"].project_final_request(messages, tools, config)
    large = adapters["fixed-large"].project_final_request(
        messages, tools, config.model_copy(update={"provider_context_window_tokens": 131072}),
    )
    assert not small.fits and large.fits
    assert small.proof["estimated_tokens"] + config.max_tokens > 32768
    assert small.payload["messages"] == large.payload["messages"]
    assert small.payload["tools"] == large.payload["tools"]
    if kind == "combined":
        # No individual part is oversized here: the sum plus output reservation
        # is the problem, so a single-component diagnostic cannot be the gate.
        for part in ("system", "tools", "user"):
            assert adapters["fixed-small"].project_final_request(
                messages if part == "user" else [Message(role="user", content="Go.")],
                tools if part == "tools" else [],
                config.model_copy(update={"system": parts["system"] if part == "system" else ""}),
            ).fits

    events = [event async for event in wrapper.chat(messages, tools=tools, config=config)]

    assert (messages, tools) == before
    assert len(adapters["fixed-small"].calls) == 1
    if authorized:
        assert [payload["model"] for payload in physical] == ["fixed-large"]
        call = adapters["fixed-large"].calls[0]
        assert call[2].turn_deadline_at_monotonic == deadline
        assert call[2].max_tokens == 4096
        assert call[2].provider_context_window_tokens == 131072
        assert physical[0] == call[3].payload
        assert physical[0]["messages"] == large.payload["messages"]
        assert physical[0]["tools"] == large.payload["tools"]
        assert metadata["router_fallback_reason"] == "local_admission_escalation"
        assert any(event.kind == "done" for event in events)
        assert not any(event.kind == "error" for event in events)
    else:
        assert not physical and not adapters["fixed-large"].calls
        assert builds == ["fixed-small"]
        errors = [event for event in events if event.kind == "error"]
        assert len(errors) == 1 and errors[0].code == "provider_request_budget_exhausted"
        assert not any(event.kind == "done" for event in events)
    assert small.payload["messages"][0]["content"] == parts["system"]
    assert small.payload["messages"][-1]["content"] == parts["user"]
    assert small.payload["tools"][0]["function"]["description"] == parts["tools"]


@pytest.mark.parametrize("guard", ["global_override", "tools", "single_attempt"])
async def test_fixed_envelope_does_not_bypass_capacity_or_authority_guards(monkeypatch, guard):
    wrapper, adapters, physical, builds, _metadata = setup(
        monkeypatch, authorized=True, guard=guard,
    )
    parts, messages, tools = fixed_material("combined")
    config = chat_config(
        system=parts["system"],
        context_window_tokens_global_override=32768 if guard == "global_override" else 0,
        physical_attempt_limit=1 if guard == "single_attempt" else 0,
    )
    events = [event async for event in wrapper.chat(messages, tools=tools, config=config)]
    assert not physical and not adapters["fixed-large"].calls
    assert builds == ["fixed-small"]
    assert any(event.kind == "error" and event.code == "provider_request_budget_exhausted"
               for event in events)
    assert not any(event.kind == "done" for event in events)


async def test_authorized_larger_target_still_refuses_oversized_fixed_material(monkeypatch):
    wrapper, adapters, physical, _builds, _metadata = setup(monkeypatch, authorized=True)
    text = "Current request begin " + "fact " * 150000 + " current request end"
    messages = [Message(role="user", content=text)]
    config = chat_config(system="Preserve the task.")
    events = [event async for event in wrapper.chat(messages, tools=[], config=config)]
    assert not physical
    assert [len(adapters[name].calls) for name in ("fixed-small", "fixed-large")] == [1, 1]
    for adapter in adapters.values():
        _messages, _tools, _config, projection = adapter.calls[0]
        assert not projection.fits
        assert projection.payload["messages"][-1]["content"] == text
    assert sum(event.kind == "error" and event.code == "provider_request_budget_exhausted"
               for event in events) == 1
    assert not any(event.kind == "done" for event in events)
    assert messages[0].content == text


@pytest.mark.parametrize("kind", ["system", "tools", "user", "combined"])
@pytest.mark.parametrize("authorized", [False, True])
async def test_agent_fixed_envelope_has_no_summary_loop_or_durable_event(
    monkeypatch, kind, authorized,
):
    wrapper, adapters, physical, builds, _metadata = setup(monkeypatch, authorized=authorized)
    parts, _messages, tools = fixed_material(kind)
    summary_calls = []
    from opensquilla.engine import agent as agent_module

    original_compact = agent_module.compact_context

    async def observe_compaction(request):
        summary_calls.append(request)
        return await original_compact(request)

    monkeypatch.setattr("opensquilla.engine.agent.compact_context", observe_compaction)
    agent = Agent(wrapper, AgentConfig(
        system_prompt=parts["system"], context_window_tokens=32768,
        context_window_known=True, max_tokens=4096, max_provider_retries=0,
        model_capabilities=ModelCapabilities(supports_tools=True),
        timeout=60, iteration_timeout=60,
    ), tool_definitions=tools)
    events = [event async for event in agent.run_turn(parts["user"])]
    # One local planning pass may establish that there is no removable prefix.
    # It must not issue a summary HTTP request or restart the same futile plan.
    assert len(summary_calls) <= 1
    assert not any(isinstance(event, CompactionEvent) for event in events)
    errors = [event for event in events if isinstance(event, ErrorEvent)]
    if authorized:
        assert [payload["model"] for payload in physical] == ["fixed-large"]
        assert not errors
        assert any(isinstance(event, DoneEvent) for event in events)
        assert physical[0]["messages"][0]["content"] == parts["system"]
        # Agent._append_runtime_context_to_user_message appends this exact
        # envelope after the unmodified current request. Compare the complete
        # pre-fallback and sent messages as well, not a loose substring match.
        assert any(message["content"].startswith(
            parts["user"] + "\n\n[Runtime context for this turn]\n",
        )
                   for message in physical[0]["messages"] if message["role"] == "user")
        assert physical[0]["messages"] == adapters["fixed-small"].calls[0][3].payload["messages"]
        assert physical[0] == adapters["fixed-large"].calls[0][3].payload
        assert physical[0]["tools"][0]["function"]["description"] == parts["tools"]
    else:
        assert not physical and not adapters["fixed-large"].calls
        assert builds == ["fixed-small"]
        assert len(errors) == 1 and errors[0].code == "provider_request_too_large"
        assert not any(isinstance(event, DoneEvent) for event in events)
        assert not agent.history_snapshot()
    assert agent.config.system_prompt == parts["system"]
    assert agent.tool_definitions == tools
