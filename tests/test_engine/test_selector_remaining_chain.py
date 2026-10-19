"""Pre-content failures exhaust each authorized leg once, not the whole chain."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from opensquilla.engine.fallback import FallbackPolicy
from opensquilla.engine.runtime import _SelectorFallbackProvider
from opensquilla.engine.types import ToolResult
from opensquilla.provider.openai import OpenAIProvider
from opensquilla.provider.selector import ModelSelector, ProviderConfig, SelectorConfig
from opensquilla.provider.types import (
    ChatConfig,
    DoneEvent,
    ErrorEvent,
    Message,
    ModelCapabilities,
    ReasoningDeltaEvent,
    TextDeltaEvent,
    ToolDefinition,
    ToolInputSchema,
    ToolUseEndEvent,
    ToolUseStartEvent,
)
from tests.test_engine.test_selector_provider_recovery import _agent, _success, _wrapper
from tests.test_engine.test_selector_provider_recovery import clock as clock


def limited():
    return ErrorEvent(code="429", http_status=429, message="rate limit exceeded")


def config(**updates):
    return ChatConfig(
        max_tokens=2048, provider_context_window_tokens=32768, **updates,
    )


def chain(monkeypatch, streams):
    wrapper, health, calls = _wrapper(monkeypatch, streams)
    wrapper.configure_retry_policy(FallbackPolicy(base_backoff_ms=0, max_backoff_ms=0))
    wrapper.configure_fallback_deployment_limits([
        (item, 32768, 2048, ModelCapabilities(supports_tools=True))
        for item in wrapper._selector.remaining_chain()
    ])
    return wrapper, health, calls


@pytest.mark.parametrize("all_failed", [False, True])
async def test_exhausted_rate_legs_reach_remaining_authorized_target(monkeypatch, all_failed):
    wrapper, _, calls = chain(monkeypatch, {
        "primary": [[limited()]],
        "secondary": [[limited()]],
        "third": [[limited()]] if all_failed else [_success()],
    })
    health_failures = []
    record_failure = wrapper._record_health_failure

    def record_once(event):
        health_failures.append(wrapper.active_model_id)
        record_failure(event)

    monkeypatch.setattr(wrapper, "_record_health_failure", record_once)
    agent = _agent(wrapper, context_window_tokens=32768, max_tokens=2048, timeout=60)

    events = [event async for event in agent.run_turn("Answer the current task.")]

    assert calls == ["primary"] * 4 + ["secondary"] * 4 + ["third"] * (4 if all_failed else 1)
    assert health_failures == ["primary", "secondary"] + (["third"] if all_failed else [])
    failures = [event for event in events if event.kind == "error"]
    if all_failed:
        assert len(failures) == 1 and failures[0].failure_kind == "rate_limited"
    else:
        assert failures == []
        assert any(event.kind == "done" and event.text == "recovered" for event in events)


@pytest.mark.parametrize("kind", ["text", "reasoning", "tool"])
async def test_published_fallback_content_never_advances_or_replays(monkeypatch, kind):
    prefix = {
        "text": [TextDeltaEvent(text="already visible")],
        "reasoning": [ReasoningDeltaEvent(text="already visible")],
        "tool": [
            ToolUseStartEvent(tool_use_id="published", tool_name="write"),
            ToolUseEndEvent(tool_use_id="published", tool_name="write", arguments={}),
            DoneEvent(stop_reason="tool_calls"),
        ],
    }[kind]
    wrapper, _, calls = chain(monkeypatch, {
        "primary": [[ErrorEvent(code="404", message="model not found")]],
        "secondary": [[*prefix, limited()]],
        "third": [_success()],
    })

    events = [event async for event in wrapper.chat([Message(role="user", content="go")],
                                                  config=config())]

    assert calls == ["primary", "secondary"]
    assert sum(isinstance(event, ErrorEvent) for event in events) == 1
    if kind == "tool":
        assert sum(isinstance(event, ToolUseEndEvent) for event in events) == 1


async def test_provisional_tools_are_discarded_before_remaining_leg(monkeypatch):
    wrapper, _, calls = chain(monkeypatch, {
        "primary": [[ErrorEvent(code="404", message="model not found")]],
        "secondary": [[
            ToolUseStartEvent(tool_use_id="discard", tool_name="write"), limited(),
        ]],
        "third": [_success()],
    })

    events = [event async for event in wrapper.chat([Message(role="user", content="go")],
                                                  config=config())]

    assert calls == ["primary"] + ["secondary"] * 4 + ["third"]
    assert not any(getattr(event, "tool_use_id", "") == "discard" for event in events)
    assert not any(isinstance(event, ErrorEvent) for event in events)


@pytest.mark.parametrize("failure", [
    ErrorEvent(code="401", http_status=401, message="invalid API key"),
    ErrorEvent(code="policy_refusal", message="content policy violation"),
])
async def test_noneligible_failure_does_not_advance_remaining_chain(monkeypatch, failure):
    wrapper, _, calls = chain(monkeypatch, {
        "primary": [[ErrorEvent(code="404", message="model not found")]],
        "secondary": [[failure]], "third": [_success()],
    })

    events = [event async for event in wrapper.chat([Message(role="user", content="go")],
                                                  config=config())]

    assert calls == ["primary", "secondary"]
    assert events[-1] is failure


@pytest.mark.parametrize("limit", [1, 2, 3])
async def test_explicit_physical_allowance_is_shared_across_legs(monkeypatch, limit):
    wrapper, _, calls = chain(monkeypatch, {
        "primary": [[ErrorEvent(code="503", message="busy")]],
        "secondary": [[ErrorEvent(code="503", message="busy")]],
        "third": [_success()],
    })

    events = [event async for event in wrapper.chat(
        [Message(role="user", content="go")], config=config(physical_attempt_limit=limit),
    )]

    assert calls == ["primary", "secondary", "third"][:limit]
    assert any(isinstance(event, DoneEvent) for event in events) is (limit == 3)


@pytest.mark.parametrize("limit", [4, 5, 8, 9])
async def test_rate_retries_consume_the_shared_physical_allowance(monkeypatch, limit):
    wrapper, _, calls = chain(monkeypatch, {
        "primary": [[limited()]], "secondary": [[limited()]], "third": [_success()],
    })
    events = [event async for event in wrapper.chat(
        [Message(role="user", content="go")], config=config(physical_attempt_limit=limit),
    )]

    assert calls == (["primary"] * 4 + ["secondary"] * 4 + ["third"])[:limit]
    assert any(isinstance(event, DoneEvent) for event in events) is (limit == 9)


async def test_third_leg_tool_executes_once_and_continues_on_the_same_leg(monkeypatch):
    from opensquilla.engine import Agent, AgentConfig

    wrapper, _, calls = chain(monkeypatch, {
        "primary": [[ErrorEvent(code="404", message="model not found")]],
        "secondary": [[ErrorEvent(code="404", message="model not found")]],
        "third": [[
            ToolUseStartEvent(tool_use_id="one-write", tool_name="write"),
            ToolUseEndEvent(tool_use_id="one-write", tool_name="write", arguments={}),
            DoneEvent(stop_reason="tool_calls"),
        ], _success()],
    })
    effects = []

    async def execute(call):
        effects.append(call.tool_use_id)
        return ToolResult(tool_use_id=call.tool_use_id, tool_name=call.tool_name, content="written")

    agent = Agent(
        wrapper,
        AgentConfig(context_window_tokens=32768, max_tokens=2048, timeout=60,
                    retry_base_backoff_ms=0, retry_max_backoff_ms=0),
        tool_definitions=[ToolDefinition(name="write", description="write",
                                         input_schema=ToolInputSchema(properties={}))],
        tool_handler=execute,
    )
    events = [event async for event in agent.run_turn("Write once, then report.")]

    assert effects == ["one-write"]
    assert calls == ["primary", "secondary", "third", "third"]
    assert not any(event.kind == "error" for event in events)
    assert any(event.kind == "done" and event.text == "recovered" for event in events)


async def test_remaining_chain_cannot_restart_duplicate_failed_deployment(monkeypatch):
    wrapper, _, calls = chain(monkeypatch, {
        "primary": [[ErrorEvent(code="404", message="model not found")]],
        "secondary": [[ErrorEvent(code="404", message="model not found")]],
        "third": [_success()],
    })
    selector = wrapper._selector
    selector._chain.insert(2, selector._chain[0])
    hook_calls = []
    original = selector.next_fallback_after_failure

    def initial_hook(error):
        hook_calls.append(error)
        return original(error)

    monkeypatch.setattr(selector, "next_fallback_after_failure", initial_hook)
    events = [event async for event in wrapper.chat([Message(role="user", content="go")],
                                                  config=config())]

    assert calls == ["primary", "secondary", "third"]
    assert len(hook_calls) == 1
    assert not any(isinstance(event, ErrorEvent) for event in events)


async def test_remaining_chain_keeps_capacity_bound_and_tool_capabilities(monkeypatch):
    wrapper, _, calls = chain(monkeypatch, {
        "primary": [[ErrorEvent(code="404", message="model not found")]],
        "secondary": [[ErrorEvent(code="404", message="model not found")]],
        "excluded": [_success()], "no_tools": [_success()], "allowed": [_success()],
    })
    candidates = wrapper._selector.remaining_chain()
    wrapper._selector._install_capacity_fallback_bound([
        candidates[1], candidates[3], candidates[4],
    ])
    wrapper.configure_fallback_deployment_limits([
        (candidates[3], 32768, 2048, ModelCapabilities(supports_tools=False)),
    ])
    tool = ToolDefinition(name="write", description="write",
                          input_schema=ToolInputSchema(properties={}))
    events = [event async for event in wrapper.chat(
        [Message(role="user", content="go")], tools=[tool], config=config(),
    )]

    assert calls == ["primary", "secondary", "allowed"]
    assert not any(isinstance(event, ErrorEvent) for event in events)


async def test_expired_parent_prevents_remaining_target(monkeypatch, clock):
    monkeypatch.setattr("opensquilla.engine.runtime.time.monotonic", lambda: clock[0][0])
    wrapper, _, calls = chain(monkeypatch, {
        "primary": [[ErrorEvent(code="404", message="model not found")]],
        "secondary": [[ErrorEvent(code="503", message="busy")]], "third": [_success()],
    })
    original_build = __import__("opensquilla.provider.selector", fromlist=["_build_provider"])
    build = original_build._build_provider
    secondary = build(wrapper._selector.remaining_chain()[1])

    async def expire(messages, tools=None, config=None):
        calls.append("secondary")
        clock[0][0] += 61
        yield ErrorEvent(code="503", message="busy")

    monkeypatch.setattr(secondary, "chat", expire)
    events = [event async for event in wrapper.chat(
        [Message(role="user", content="go")],
        config=config(turn_deadline_at_monotonic=clock[0][0] + 60),
    )]

    assert calls == ["primary", "secondary"]
    assert any(isinstance(event, ErrorEvent) for event in events)


async def test_cancel_during_second_leg_never_starts_remaining_target(monkeypatch):
    wrapper, _, calls = chain(monkeypatch, {
        "primary": [[ErrorEvent(code="404", message="model not found")]],
        "secondary": [[limited()]], "third": [_success()],
    })
    from opensquilla.provider import selector as selector_module

    secondary = selector_module._build_provider(wrapper._selector.remaining_chain()[1])
    entered = asyncio.Event()
    closed = asyncio.Event()

    async def blocked(messages, tools=None, config=None):
        calls.append("secondary")
        entered.set()
        try:
            await asyncio.Event().wait()
            yield limited()
        finally:
            closed.set()

    monkeypatch.setattr(secondary, "chat", blocked)

    async def consume():
        return [event async for event in wrapper.chat(
            [Message(role="user", content="go")], config=config(),
        )]

    task = asyncio.create_task(consume())
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()
    assert calls == ["primary", "secondary"]


async def test_actual_openrouter_adapter_reaches_third_with_same_parent_budget(monkeypatch):
    payloads = []
    deadlines = []
    original_client = httpx.AsyncClient

    def respond(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        if payload["model"] != "third":
            return httpx.Response(429, json={"error": {
                "code": "rate_limit_exceeded", "message": "rate limit exceeded",
            }})
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=(
            b'data: {"choices":[{"delta":{"content":"recovered"},"finish_reason":"stop"}]}'
            b'\n\ndata: [DONE]\n\n'
        ))

    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: original_client(
        *args, **{**kwargs, "transport": httpx.MockTransport(respond)},
    ))

    class ObservedOpenRouter(OpenAIProvider):
        async def chat(self, messages, tools=None, config=None):
            deadlines.append(config.turn_deadline_at_monotonic)
            async for event in super().chat(messages, tools=tools, config=config):
                yield event

    candidates = [ProviderConfig(provider="openrouter", model=model, api_key="synthetic-unused",
                                 base_url=f"https://{model}.test")
                  for model in ("primary", "secondary", "third")]
    monkeypatch.setattr("opensquilla.provider.selector._build_provider", lambda cfg:
                        ObservedOpenRouter(api_key=cfg.api_key, model=cfg.model,
                                           base_url=cfg.base_url, provider_kind="openrouter"))
    selector = ModelSelector(SelectorConfig(primary=candidates[0], fallbacks=candidates[1:]))
    wrapper = _SelectorFallbackProvider(selector.resolve(), selector)
    wrapper.configure_retry_policy(FallbackPolicy(base_backoff_ms=0, max_backoff_ms=0))
    wrapper.configure_fallback_deployment_limits([
        (item, 32768, 2048, ModelCapabilities(supports_tools=True)) for item in candidates
    ])
    deadline = asyncio.get_running_loop().time() + 60
    events = [event async for event in wrapper.chat(
        [Message(role="user", content="Keep the current task.")],
        config=config(turn_deadline_at_monotonic=deadline),
    )]

    assert [payload["model"] for payload in payloads] == (
        ["primary"] * 4 + ["secondary"] * 4 + ["third"]
    )
    assert deadlines == [deadline] * 9
    assert all(payload["max_tokens"] == 2048 for payload in payloads)
    assert all(payload["messages"] == payloads[0]["messages"] for payload in payloads)
    assert not any(isinstance(event, ErrorEvent) for event in events)
