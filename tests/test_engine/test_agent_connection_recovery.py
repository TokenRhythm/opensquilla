from __future__ import annotations

import asyncio
import copy
import json

import httpx
import pytest

from opensquilla.engine import Agent, AgentConfig, ToolResult
from opensquilla.engine.runtime import _SelectorFallbackProvider
from opensquilla.provider.selector import ModelSelector, ProviderConfig, SelectorConfig
from opensquilla.provider.types import (
    DoneEvent,
    ErrorEvent,
    ReasoningDeltaEvent,
    TextDeltaEvent,
    ToolDefinition,
    ToolInputSchema,
    ToolUseEndEvent,
    ToolUseStartEvent,
)


def _connection():
    return ErrorEvent(message="temporary connection failure", code="connection_failed")


def _success():
    return [TextDeltaEvent(text="done"), DoneEvent(stop_reason="stop")]


class _Provider:
    provider_name = "openai"
    retry_failed_call_safe = True

    def __init__(self, streams, *, creation_failure=False, allow_fallback=False):
        self.streams = streams
        self.creation_failure = creation_failure
        self.allow_fallback = allow_fallback
        self.calls = []
        self.fallbacks = []

    def chat(self, messages, tools=None, config=None):
        index = len(self.calls)
        self.calls.append((copy.deepcopy(messages), config))
        stream = self.streams[min(index, len(self.streams) - 1)]
        if self.creation_failure and isinstance(stream[0], Exception):
            raise stream[0]
        return self._stream(stream)

    async def _stream(self, stream):
        for event in stream:
            if isinstance(event, Exception):
                raise event
            yield event

    def fallback_after_managed_recovery(self, error, **kwargs):
        self.fallbacks.append(error.code)
        return self.allow_fallback


def _config(**overrides):
    return AgentConfig(
        retry_base_backoff_ms=0,
        retry_max_backoff_ms=0,
        **overrides,
    )


async def _run(provider, **config):
    agent = Agent(provider=provider, config=_config(**config))
    return [event async for event in agent.run_turn("complete the task")]


@pytest.fixture
def fast_wait(monkeypatch):
    delays = []
    original_sleep = asyncio.sleep

    async def sleep(delay):
        delays.append(delay)
        await original_sleep(0)

    monkeypatch.setattr("opensquilla.engine.agent.asyncio.sleep", sleep)
    return delays


@pytest.mark.parametrize("creation_failure", [False, True])
async def test_typed_connection_wait_is_independent_of_finite_retry_budget(
    fast_wait, creation_failure
) -> None:
    provider = _Provider(
        [[httpx.ConnectError("untrusted error prose")]] * 7 + [_success()],
        creation_failure=creation_failure,
    )
    events = await _run(provider, provider_connection_recovery_enabled=True, max_provider_retries=0)
    waits = [
        event
        for event in events
        if event.kind == "provider_activity" and event.phase == "retry_wait"
    ]
    assert fast_wait == [5, 10, 20, 40, 60, 60, 60]
    assert [event.retry_limit for event in waits] == [0] * 7
    assert [event.retry_attempt for event in waits] == list(range(1, 8))
    assert all(event.reason == "transport_transient" for event in waits)
    assert provider.fallbacks == []
    assert len(provider.calls) == 8
    assert any(event.kind == "done" and event.text == "done" for event in events)
    assert all(config.agent_managed_recovery for _, config in provider.calls)


async def test_connection_wait_does_not_exhaust_subsequent_rate_retries(fast_wait) -> None:
    rate = ErrorEvent(message="rate limit exceeded", code="429")
    provider = _Provider([[_connection()]] * 5 + [[rate]] * 3 + [_success()])
    events = await _run(provider, provider_connection_recovery_enabled=True)
    assert len(provider.calls) == 9
    assert provider.fallbacks == []
    assert any(event.kind == "done" and event.text == "done" for event in events)


@pytest.mark.parametrize("error", [_connection(), ErrorEvent(message="rate limit", code="429")])
async def test_finite_recovery_selects_fallback_once_without_resetting_budget(fast_wait, error):
    provider = _Provider([[error]], allow_fallback=True)
    events = await _run(provider)
    assert len(provider.calls) == 5  # initial + three retries + one fallback attempt
    assert provider.fallbacks == [error.code]
    assert any(event.kind == "error" for event in events)


@pytest.mark.parametrize(
    "error",
    [
        httpx.ReadTimeout("connection timeout"),
        RuntimeError("connection failed"),
        ErrorEvent(message="insufficient_quota", code="429"),
    ],
)
async def test_only_typed_connection_failures_enter_persistent_wait(fast_wait, error):
    provider = _Provider([[error]])
    events = await _run(provider, provider_connection_recovery_enabled=True)
    assert len(provider.calls) <= 4
    assert not any(
        event.kind == "provider_activity" and event.phase == "retry_wait" and event.retry_limit == 0
        for event in events
    )


@pytest.mark.parametrize("prefix", [TextDeltaEvent(text="partial"), ReasoningDeltaEvent(text="r")])
async def test_visible_output_prevents_connection_replay(fast_wait, prefix):
    provider = _Provider([[prefix, _connection()]], allow_fallback=True)
    events = await _run(provider, provider_connection_recovery_enabled=True)
    assert len(provider.calls) == 1
    assert fast_wait == []
    assert provider.fallbacks == []
    assert any(event.kind == "error" for event in events)


async def test_composite_provider_retains_its_retry_owner(fast_wait):
    provider = _Provider([[_connection()]], allow_fallback=True)
    provider.retry_failed_call_safe = False
    await _run(provider, provider_connection_recovery_enabled=True)
    assert len(provider.calls) == 1
    assert fast_wait == []
    assert provider.fallbacks == []
    assert provider.calls[0][1].agent_managed_recovery is False


async def test_connection_wait_is_cancellable(monkeypatch):
    waiting = asyncio.Event()

    async def sleep(_delay):
        waiting.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("opensquilla.engine.agent.asyncio.sleep", sleep)
    provider = _Provider([[_connection()]])
    task = asyncio.create_task(_run(provider, provider_connection_recovery_enabled=True))
    await asyncio.wait_for(waiting.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(provider.calls) == 1


async def test_connection_wait_stops_at_explicit_task_deadline(fast_wait):
    provider = _Provider([[_connection()]])
    events = await _run(provider, provider_connection_recovery_enabled=True, timeout=2)
    assert len(provider.calls) == 1
    assert len(fast_wait) == 1 and 0 < fast_wait[0] <= 2
    assert any(event.kind == "error" and event.code == "agent_runtime_timeout" for event in events)


async def test_managed_connection_wait_has_a_stale_recovery_bound(monkeypatch):
    loop = asyncio.get_running_loop()
    now = [loop.time()]
    original_sleep = asyncio.sleep
    delays = []

    async def sleep(delay):
        delays.append(delay)
        now[0] += delay
        await original_sleep(0)

    monkeypatch.setattr(loop, "time", lambda: now[0])
    monkeypatch.setattr("opensquilla.engine.agent.asyncio.sleep", sleep)
    provider = _Provider([[_connection()]] * 35 + [_success()])
    events = await _run(
        provider, provider_connection_recovery_enabled=True, timeout=0, iteration_timeout=1800
    )
    assert sum(delays) == 1800
    assert len(provider.calls) < 36
    assert not any(event.kind == "done" and event.text == "done" for event in events)
    assert any(event.kind == "error" and event.code == "agent_runtime_timeout" for event in events)


async def test_connection_retry_keeps_completed_tool_results_without_reexecution(fast_wait):
    provider = _Provider(
        [
            [
                ToolUseStartEvent(tool_use_id="write-1", tool_name="write_file"),
                ToolUseEndEvent(tool_use_id="write-1", tool_name="write_file", arguments={}),
                DoneEvent(stop_reason="tool_use"),
            ],
            [_connection()],
            [_connection()],
            _success(),
        ]
    )
    executions = []

    async def tool(call):
        executions.append(call.tool_use_id)
        return ToolResult(tool_use_id=call.tool_use_id, tool_name=call.tool_name, content="saved")

    agent = Agent(
        provider=provider,
        config=_config(provider_connection_recovery_enabled=True),
        tool_handler=tool,
        tool_definitions=[
            ToolDefinition(
                name="write_file", description="Write", input_schema=ToolInputSchema(properties={})
            )
        ],
    )
    events = [event async for event in agent.run_turn("write the file")]
    assert executions == ["write-1"]
    assert len(provider.calls) == 4
    assert provider.calls[1][0] == provider.calls[2][0] == provider.calls[3][0]
    assert any(event.kind == "done" and event.text == "done" for event in events)


@pytest.mark.parametrize("same_authority", [False, True])
async def test_rate_wait_beyond_task_deadline_only_falls_back_to_other_authority(
    monkeypatch, fast_wait, same_authority
):
    rate = ErrorEvent(message="rate limit exceeded", code="429", retry_after_s=60)
    providers = {"primary": _Provider([[rate]]), "secondary": _Provider([_success()])}
    monkeypatch.setattr(
        "opensquilla.provider.selector._build_provider", lambda config: providers[config.model]
    )
    selector = ModelSelector(
        SelectorConfig(
            primary=ProviderConfig(
                provider="openai", model="primary", api_key="dummy", base_url="https://primary.test"
            ),
            fallbacks=[
                ProviderConfig(
                    provider="openai",
                    model="secondary",
                    api_key="dummy",
                    base_url=(
                        "https://primary.test" if same_authority else "https://secondary.test"
                    ),
                )
            ],
        )
    )
    wrapper = _SelectorFallbackProvider(selector.resolve(), selector)
    events = await _run(wrapper, timeout=2)
    assert len(providers["primary"].calls) == 1
    assert len(providers["secondary"].calls) == (0 if same_authority else 1)
    assert fast_wait == []
    assert any(event.kind == ("error" if same_authority else "done") for event in events)


@pytest.mark.parametrize(
    (
        "primary_url", "fallback_url", "fallback_key", "fallback_org", "can_fallback",
        "fallback_kind",
    ),
    [
        ("", "https://api.openai.com/v1", "dummy", "", False, "openai"),
        ("https://api.openai.com/v1", "", "dummy", "", False, "openai"),
        ("", "HTTPS://API.OPENAI.COM:443/v1/", "dummy", "", False, "openai"),
        ("", "https://api.openai.com/", "dummy", "", False, "openai"),
        ("http://local.test/v1", "HTTP://LOCAL.TEST:80/v1/", "dummy", "", False, "openai"),
        ("", "https://api.openai.com/v1", "other-dummy", "", True, "openai"),
        ("", "https://api.openai.com/v1", "dummy", "other-org", True, "openai"),
        ("", "https://secondary.test/v1", "dummy", "", True, "openai"),
        ("", "https://api.openai.com:8443/v1", "dummy", "", True, "openai"),
        ("https://proxy.test/Tenant", "https://proxy.test/tenant", "dummy", "", True, "openai"),
        (
            "https://api.deepseek.com/v1", "https://api.deepseek.com/v1",
            "dummy", "", False, "deepseek",
        ),
    ],
    ids=[
        "implicit-default", "explicit-default", "http-case-port-slash", "versioned-root",
        "http-default-port", "other-key", "other-org", "other-host", "other-port",
        "case-sensitive-path", "same-transport-brand-alias",
    ],
)
async def test_real_adapter_rate_fallback_respects_effective_authority(
    monkeypatch, fast_wait, primary_url, fallback_url, fallback_key, fallback_org, can_fallback,
    fallback_kind,
):
    """Exercise actual adapter URL construction and HTTP 429 classification."""
    requests = []

    def handle(request):
        model = json.loads(request.content)["model"]
        requests.append(request)
        if model == "gpt-4o":
            return httpx.Response(
                429,
                headers={"retry-after": "60"},
                json={"error": {"message": "rate limit exceeded", "code": "rate_limit_exceeded"}},
            )
        frames = [
            {
                "id": "synthetic-response", "model": model,
                "choices": [{"index": 0, "delta": {"content": "done"}, "finish_reason": None}],
            },
            {
                "id": "synthetic-response", "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            },
        ]
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text="".join(f"data: {json.dumps(frame)}\n\n" for frame in frames)
            + "data: [DONE]\n\n",
        )

    original_client = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs.pop("proxy", None)
        kwargs["transport"] = httpx.MockTransport(handle)
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    selector = ModelSelector(
        SelectorConfig(
            primary=ProviderConfig(
                provider="openai", model="gpt-4o", api_key="dummy", base_url=primary_url,
            ),
            fallbacks=[ProviderConfig(
                provider=fallback_kind, model="gpt-4o-mini", api_key=fallback_key,
                base_url=fallback_url, org_id=fallback_org,
            )],
        )
    )
    events = await _run(_SelectorFallbackProvider(selector.resolve(), selector), timeout=2)

    assert [json.loads(request.content)["model"] for request in requests] == (
        ["gpt-4o", "gpt-4o-mini"] if can_fallback else ["gpt-4o"]
    )
    assert fast_wait == []
    assert any(event.kind == ("done" if can_fallback else "error") for event in events)
    if can_fallback:
        assert requests[-1].headers["authorization"] == f"Bearer {fallback_key}"
        assert requests[-1].headers.get("openai-organization", "") == fallback_org
