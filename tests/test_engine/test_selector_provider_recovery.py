from __future__ import annotations

import asyncio
from collections import Counter

import pytest

from opensquilla.engine import Agent, AgentConfig
from opensquilla.engine.routing.health import ProviderHealthLedger
from opensquilla.engine.runtime import _SelectorFallbackProvider
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


def _wrapper(monkeypatch, streams):
    calls = []
    providers = {name: _Provider(name, events, calls) for name, events in streams.items()}
    configs = [
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


@pytest.mark.parametrize("failure", [_connection(), _rate()], ids=["connection", "rate-limit"])
async def test_normal_task_deadline_closes_provider_retry_wait(monkeypatch, failure):
    provider, _, calls = _wrapper(monkeypatch, {"primary": [[failure]]})
    events = [event async for event in _agent(provider, timeout=0.05).run_turn("run")]
    assert calls == ["primary"]
    assert any(event.kind == "error" and event.code == "agent_runtime_timeout" for event in events)


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
