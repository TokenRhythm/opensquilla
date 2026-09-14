from __future__ import annotations

import asyncio
import json
from collections import Counter

import httpx
import pytest

from opensquilla.engine import Agent, AgentConfig
from opensquilla.engine.routing.health import ProviderHealthLedger
from opensquilla.engine.runtime import _SelectorFallbackProvider
from opensquilla.provider.failures import ProviderFailureKind
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
    ToolUseStartEvent,
)


def _success():
    return [TextDeltaEvent(text="recovered"), DoneEvent(stop_reason="stop")]


def _connection():
    return ErrorEvent(message="connection failed", code="connection_failed")


def _rate():
    return ErrorEvent(message="rate limit exceeded", code="429", retry_after_s=8)


class _Provider:
    provider_name = "openai"

    def __init__(self, model, streams, calls):
        self.model = model
        self.streams = streams
        self.calls = calls
        self.attempt = 0

    async def chat(self, messages, tools=None, config=None):
        self.calls.append(self.model)
        stream = self.streams[min(self.attempt, len(self.streams) - 1)]
        self.attempt += 1
        for event in stream:
            yield event


def _wrapper(monkeypatch, streams, *, configs=None):
    calls = []
    providers = {name: _Provider(name, events, calls) for name, events in streams.items()}
    configs = configs or [
        ProviderConfig(
            provider="openai", model=name, api_key="dummy", base_url=f"https://{name}.test"
        )
        for name in streams
    ]
    monkeypatch.setattr(
        "opensquilla.provider.selector._build_provider", lambda config: providers[config.model]
    )
    selector = ModelSelector(SelectorConfig(primary=configs[0], fallbacks=configs[1:]))
    health = ProviderHealthLedger(failure_threshold=1)
    wrapper = _SelectorFallbackProvider(selector.resolve(), selector, health_ledger=health)
    return wrapper, health, calls


def _agent(provider, **config):
    return Agent(provider, AgentConfig(
        retry_base_backoff_ms=0, retry_max_backoff_ms=0, **config
    ))


class _InterruptedOpenAIStream(httpx.AsyncByteStream):
    def __init__(self, prefix=b""):
        self.prefix = prefix

    async def __aiter__(self):
        if self.prefix:
            yield self.prefix
        raise httpx.ReadTimeout("stream interrupted after HTTP 200")


def _openai_success():
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=(
        b'data: {"choices":[{"delta":{"content":"recovered"},"finish_reason":"stop"}]}'
        b'\n\ndata: [DONE]\n\n'
    ))


def _openai_wrapper(monkeypatch, handler, *, secondary=False):
    original_client = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs.pop("proxy", None)
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    configs = [ProviderConfig(
        provider="openai", model=name, api_key="dummy", base_url=f"https://{name}.test",
    ) for name in (["primary", "secondary"] if secondary else ["primary"])]
    monkeypatch.setattr("opensquilla.provider.selector._build_provider", lambda config: (
        OpenAIProvider(
            api_key=config.api_key, model=config.model,
            base_url=config.base_url, provider_kind="openrouter",
        )
    ))
    selector = ModelSelector(SelectorConfig(primary=configs[0], fallbacks=configs[1:]))
    return _SelectorFallbackProvider(selector.resolve(), selector)


@pytest.fixture
async def clock(monkeypatch):
    loop = asyncio.get_running_loop()
    now = [loop.time()]
    delays = []
    real_sleep = asyncio.sleep

    async def sleep(delay):
        delays.append(delay)
        now[0] += delay
        await real_sleep(0)

    monkeypatch.setattr(loop, "time", lambda: now[0])
    monkeypatch.setattr("opensquilla.engine.fallback.asyncio.sleep", sleep)
    return now, delays


@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ConnectTimeout])
async def test_compat_connection_failure_keeps_finite_retry_budget(monkeypatch, clock, error_type):
    calls = []

    def handler(request):
        streaming = json.loads(request.content)["stream"]
        calls.append(streaming)
        if streaming:
            # A later success makes an accidental persistent retry fail promptly.
            if calls.count(True) >= 7:
                return _openai_success()
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"},
                stream=_InterruptedOpenAIStream(),
            )
        raise error_type("non-stream connection failed", request=request)

    provider = _openai_wrapper(monkeypatch, handler)
    events = [event async for event in _agent(
        provider, timeout=1800, max_provider_retries=3,
    ).run_turn("run")]

    assert calls == [True, False] * 4
    expected = "timeout" if error_type is httpx.ConnectTimeout else "request_error"
    assert [event.code for event in events if event.kind == "error"] == [expected]
    assert not any(event.kind == "done" for event in events)
    assert not any(
        event.kind == "provider_activity" and event.phase == "retry_wait" and event.retry_limit == 0
        for event in events
    )


@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ConnectTimeout])
async def test_first_request_connection_failure_still_recovers(monkeypatch, clock, error_type):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content)["stream"])
        if len(calls) < 7:
            raise error_type("first request connection failed", request=request)
        return _openai_success()

    provider = _openai_wrapper(monkeypatch, handler)
    events = [event async for event in _agent(
        provider, timeout=1800, max_provider_retries=3,
    ).run_turn("run")]

    assert calls == [True] * 7
    assert [delay for delay in clock[1] if delay > 0] == [5, 10, 20, 40, 60, 60]
    waits = [event for event in events if (
        event.kind == "provider_activity" and event.phase == "retry_wait"
    )]
    assert [event.retry_limit for event in waits] == [0] * 6
    assert not any(event.kind == "error" for event in events)
    assert any(event.kind == "done" and event.text == "recovered" for event in events)


@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ConnectTimeout])
async def test_compat_connection_failure_can_use_configured_fallback(
    monkeypatch, clock, error_type,
):
    calls = []

    def handler(request):
        streaming = json.loads(request.content)["stream"]
        calls.append((request.url.host, streaming))
        if request.url.host == "secondary.test" or calls.count(("primary.test", True)) >= 7:
            return _openai_success()
        if streaming:
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"},
                stream=_InterruptedOpenAIStream(),
            )
        raise error_type("non-stream connection failed", request=request)

    provider = _openai_wrapper(monkeypatch, handler, secondary=True)
    events = [event async for event in _agent(provider, timeout=1800).run_turn("run")]

    assert calls == [("primary.test", True), ("primary.test", False), ("secondary.test", True)]
    assert not any(event.kind == "error" for event in events)
    assert any(event.kind == "done" and event.text == "recovered" for event in events)


@pytest.mark.parametrize("delta", [{"content": "partial"}, {"reasoning_content": "reasoning"}])
async def test_stream_timeout_after_content_does_not_resend_compat_or_fallback(
    monkeypatch, clock, delta,
):
    calls = []
    prefix = f'data: {json.dumps({"choices": [{"delta": delta, "finish_reason": None}]})}\n\n'

    def handler(request):
        calls.append((request.url.host, json.loads(request.content)["stream"]))
        if request.url.host == "secondary.test":
            return _openai_success()
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            stream=_InterruptedOpenAIStream(prefix.encode()),
        )

    provider = _openai_wrapper(monkeypatch, handler, secondary=True)
    events = [event async for event in _agent(provider, timeout=1800).run_turn("run")]

    assert calls == [("primary.test", True)]
    expected_kind = "text_delta" if "content" in delta else "thinking"
    assert any(
        event.kind == expected_kind and event.text == next(iter(delta.values())) for event in events
    )
    assert any(event.kind == "error" for event in events)
    assert not any(event.kind == "done" for event in events)


@pytest.mark.parametrize("retries", [0, 1, 3])
@pytest.mark.parametrize("fallback", [False, True])
async def test_rate_retry_budget_applies_once_per_physical_leg(
    monkeypatch, clock, retries, fallback
):
    streams = {"primary": [[_rate()]]}
    if fallback:
        streams["secondary"] = [[_rate()]]
    provider, _, calls = _wrapper(monkeypatch, streams)
    agent = _agent(provider, max_provider_retries=retries)
    events = [event async for event in agent.run_turn("run")]
    assert Counter(calls) == {name: retries + 1 for name in streams}
    assert len(clock[1]) == retries * len(streams)
    assert clock[1] == [8] * (retries * len(streams))
    failures = [event for event in events if event.kind == "error"]
    assert len(failures) == 1
    assert failures[0].failure_kind == "rate_limited"


async def test_connection_wait_keeps_selected_leg_and_discards_failed_tool_frames(
    monkeypatch, clock
):
    partial = ToolUseStartEvent(tool_use_id="uncommitted", tool_name="write_file")
    provider, health, calls = _wrapper(monkeypatch, {
        "primary": [[partial, _connection()]] * 5 + [[_rate()], _success()],
        "secondary": [_success()],
    })
    events = [event async for event in _agent(provider).run_turn("run")]
    assert calls == ["primary"] * 7
    assert clock[1] == [5, 10, 20, 40, 60, 8]
    assert not health.is_benched("openai", "primary")
    assert not any(getattr(event, "tool_use_id", "") == "uncommitted" for event in events)
    assert not any(event.kind == "error" for event in events)
    assert any(event.kind == "done" and event.text == "recovered" for event in events)
    waits = [
        event for event in events
        if event.kind == "provider_activity" and event.phase == "retry_wait"
    ]
    assert [event.retry_limit for event in waits] == [0] * 5 + [3]


async def test_selected_fallback_recovers_connection_without_returning_to_primary(
    monkeypatch, clock
):
    provider, _, calls = _wrapper(monkeypatch, {
        "primary": [[ErrorEvent(message="model not found", code="404")]],
        "secondary": [[_connection()], [_connection()], _success()],
    })
    events = [event async for event in _agent(provider).run_turn("run")]
    assert calls == ["primary", "secondary", "secondary", "secondary"]
    assert clock[1] == [5, 10]
    assert any(event.kind == "done" and event.text == "recovered" for event in events)


@pytest.mark.parametrize("prefix", [TextDeltaEvent(text="partial"), ReasoningDeltaEvent(text="r")])
async def test_committed_stream_is_not_replayed(monkeypatch, clock, prefix):
    provider, _, calls = _wrapper(monkeypatch, {
        "primary": [[prefix, _connection()]], "secondary": [_success()]
    })
    events = [event async for event in _agent(provider).run_turn("run")]
    assert calls == ["primary"]
    assert clock[1] == []
    assert any(event.kind == "error" for event in events)


async def test_connection_wait_is_cancelled_with_current_stream(monkeypatch):
    provider, _, calls = _wrapper(monkeypatch, {"primary": [[_connection()]]})
    entered = asyncio.Event()

    async def wait(_delay):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("opensquilla.engine.runtime.sleep_before_retry", wait)

    async def run():
        return [event async for event in _agent(provider).run_turn("run")]

    task = asyncio.create_task(run())
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == ["primary"]


async def test_normal_task_deadline_closes_connection_retry_wait(monkeypatch):
    provider, _, calls = _wrapper(monkeypatch, {"primary": [[_connection()]]})
    events = [event async for event in _agent(provider, timeout=0.05).run_turn("run")]
    assert calls == ["primary"]
    assert any(event.kind == "error" and event.code == "agent_runtime_timeout" for event in events)


@pytest.mark.parametrize("timeout", [7, 8])
@pytest.mark.parametrize("failed_leg", ["primary", "fallback"])
async def test_rate_wait_beyond_deadline_uses_independent_fallback(
    monkeypatch, clock, timeout, failed_leg
):
    streams = {}
    if failed_leg == "fallback":
        streams["unavailable"] = [[ErrorEvent(message="model not found", code="404")]]
    streams.update({
        "limited": [[ToolUseStartEvent(tool_use_id="discard", tool_name="write"), _rate()]],
        "same_account": [_success()],
        "independent": [_success()],
    })
    configs = [
        ProviderConfig(
            provider="openai", model=name, api_key="dummy",
            base_url=f"https://{'limited' if name == 'same_account' else name}.test",
        )
        for name in streams
    ]
    provider, _, calls = _wrapper(monkeypatch, streams, configs=configs)

    events = [event async for event in _agent(provider, timeout=timeout).run_turn("run")]

    assert calls == (["unavailable"] if failed_leg == "fallback" else []) + [
        "limited", "independent"
    ]
    assert clock[1] == []
    assert not any(event.kind == "error" for event in events)
    assert not any(getattr(event, "tool_use_id", "") == "discard" for event in events)
    assert any(event.kind == "done" and event.text == "recovered" for event in events)


@pytest.mark.parametrize("same_account_candidate", [False, True])
async def test_rate_wait_beyond_deadline_without_independent_fallback_fails_without_waiting(
    monkeypatch, clock, same_account_candidate
):
    streams = {"limited": [[_rate()]]}
    if same_account_candidate:
        streams["same_account"] = [_success()]
    configs = [
        ProviderConfig(provider="openai", model=name, api_key="dummy", base_url="https://same.test")
        for name in streams
    ]
    provider, _, calls = _wrapper(monkeypatch, streams, configs=configs)

    events = [event async for event in _agent(provider, timeout=8).run_turn("run")]

    assert calls == ["limited"]
    assert clock[1] == []
    errors = [event for event in events if event.kind == "error"]
    assert len(errors) == 1
    assert errors[0].failure_kind == "rate_limited"
    assert not any(
        event.kind == "provider_activity" and event.phase == "retry_wait" for event in events
    )


async def test_deadline_fallback_keeps_each_provider_retry_budget(monkeypatch, clock):
    provider, _, calls = _wrapper(monkeypatch, {
        "primary": [[_rate()]] * 3 + [[ErrorEvent(
            message="rate limit exceeded", code="429", retry_after_s=80
        )]],
        "secondary": [[_rate()]] * 3 + [_success()],
    })

    events = [event async for event in _agent(provider, timeout=60).run_turn("run")]

    assert calls == ["primary"] * 4 + ["secondary"] * 4
    assert clock[1] == [8] * 6
    assert any(event.kind == "done" and event.text == "recovered" for event in events)


@pytest.mark.parametrize("prefix", [TextDeltaEvent(text="partial"), ReasoningDeltaEvent(text="r")])
async def test_rate_deadline_does_not_replay_committed_content(monkeypatch, clock, prefix):
    provider, _, calls = _wrapper(monkeypatch, {
        "primary": [[prefix, _rate()]], "secondary": [_success()]
    })

    events = [event async for event in _agent(provider, timeout=8).run_turn("run")]

    assert calls == ["primary"]
    assert clock[1] == []
    assert any(event.kind == "error" for event in events)


async def test_rate_deadline_fallback_keeps_tools_health_capacity_and_replay_policy(
    monkeypatch, clock
):
    streams = {"limited": [[_rate()]], **{
        name: [_success()] for name in ("excluded", "no_tools", "unhealthy", "allowed")
    }}
    provider, health, calls = _wrapper(monkeypatch, streams)
    configs = list(provider._selector.remaining_chain())
    provider._selector._install_capacity_fallback_bound(configs[2:])
    provider._selector.disable_provider_state_replay()
    provider.configure_fallback_deployment_limits([
        (configs[2], 0, 0, ModelCapabilities(supports_tools=False)),
    ])
    health.record_failure("openai", "unhealthy", ProviderFailureKind.RATE_LIMITED)
    agent = Agent(
        provider,
        AgentConfig(timeout=8, retry_base_backoff_ms=0, retry_max_backoff_ms=0),
        tool_definitions=[ToolDefinition(
            name="write", description="Write", input_schema=ToolInputSchema(properties={})
        )],
    )

    events = [event async for event in agent.run_turn("run")]

    assert calls == ["limited", "allowed"]
    assert provider._selector.current_config.replay_provider_state is False
    assert clock[1] == []
    assert any(event.kind == "done" and event.text == "recovered" for event in events)


async def test_rate_deadline_health_skip_without_tools_cannot_return_to_same_authority(
    monkeypatch, clock
):
    streams = {
        "limited": [[_rate()]], "unhealthy": [_success()],
        "same_account": [_success()], "allowed": [_success()],
    }
    configs = [
        ProviderConfig(
            provider="openai", model=name, api_key="dummy",
            base_url=f"https://{'limited' if name == 'same_account' else name}.test",
        )
        for name in streams
    ]
    provider, health, calls = _wrapper(monkeypatch, streams, configs=configs)
    health.record_failure("openai", "unhealthy", ProviderFailureKind.RATE_LIMITED)

    events = [event async for event in _agent(provider, timeout=8).run_turn("run")]

    assert calls == ["limited", "allowed"]
    assert clock[1] == []
    assert any(event.kind == "done" and event.text == "recovered" for event in events)


async def test_physical_attempt_limit_keeps_auxiliary_request_bounded(monkeypatch):
    provider, _, calls = _wrapper(monkeypatch, {"primary": [[_connection()]]})
    events = [event async for event in provider.chat(
        [Message(role="user", content="summarize")], config=ChatConfig(physical_attempt_limit=1)
    )]
    assert calls == ["primary"]
    errors = [event.code for event in events if isinstance(event, ErrorEvent)]
    assert errors == ["connection_failed"]


async def test_retry_after_survives_early_timer_wakeup(monkeypatch, clock):
    provider, _, calls = _wrapper(monkeypatch, {"primary": [[_rate()], _success()]})
    now, delays = clock
    real_sleep = asyncio.sleep
    count = 0

    async def early_sleep(delay):
        nonlocal count
        count += 1
        if count == 1:
            delays.append(delay)
            now[0] += delay - 0.01
        else:
            await real_sleep(delay)

    monkeypatch.setattr("opensquilla.engine.fallback.asyncio.sleep", early_sleep)
    events = [event async for event in _agent(provider).run_turn("run")]
    assert calls == ["primary", "primary"]
    assert delays == pytest.approx([8, 0.01])
    assert any(event.kind == "done" and event.text == "recovered" for event in events)
