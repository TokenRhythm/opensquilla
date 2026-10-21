"""Interrupted response recovery preserves one request and completed tool work."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from opensquilla.contracts.turn_execution import (
    SurfaceCapabilities,
    TurnExecutionContext,
    TurnIdentity,
)
from opensquilla.engine import Agent, AgentConfig, ToolResult
from opensquilla.engine.agent_injection import ListPendingInputProvider
from opensquilla.engine.types import AnswerGenerationResetEvent, DoneEvent, ErrorEvent
from opensquilla.engine.usage import UsageTracker
from opensquilla.provider.types import (
    ChatConfig,
    Message,
    ToolDefinition,
    ToolInputSchema,
)
from opensquilla.provider.types import DoneEvent as ProviderDone
from opensquilla.provider.types import ErrorEvent as ProviderError
from opensquilla.provider.types import ReasoningDeltaEvent as ProviderReasoning
from opensquilla.provider.types import TextDeltaEvent as ProviderText
from opensquilla.provider.types import ToolUseDeltaEvent as ProviderToolDelta
from opensquilla.provider.types import ToolUseEndEvent as ProviderToolEnd
from opensquilla.provider.types import ToolUseStartEvent as ProviderToolStart


class _Stream:
    def __init__(self, events: list[Any], *, stall_close: bool = False) -> None:
        self.events = iter(events)
        self.stall_close = stall_close
        self.closed = False

    def __aiter__(self) -> _Stream:
        return self

    async def __anext__(self) -> Any:
        try:
            event = next(self.events)
        except StopIteration:
            raise StopAsyncIteration from None
        if isinstance(event, BaseException):
            raise event
        return event

    async def aclose(self) -> None:
        if self.stall_close:
            await asyncio.Event().wait()
        self.closed = True


class _Provider:
    provider_name = "openai"
    retry_failed_call_safe = True

    def __init__(self, streams: list[list[Any]], *, stall_first_close: bool = False) -> None:
        self.streams = streams
        self.stall_first_close = stall_first_close
        self.calls: list[tuple[list[Message], list[Any] | None, ChatConfig | None]] = []
        self.opened: list[_Stream] = []

    def chat(
        self, messages: list[Message], tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        index = len(self.calls)
        self.calls.append((copy.deepcopy(messages), copy.deepcopy(tools), config))
        stream = _Stream(
            self.streams[min(index, len(self.streams) - 1)],
            stall_close=self.stall_first_close and index == 0,
        )
        self.opened.append(stream)
        return stream

    async def list_models(self) -> list[Any]:
        return []


def _context(*, reset: bool = True, streaming: bool = True) -> TurnExecutionContext:
    return TurnExecutionContext.create(
        TurnIdentity("stream-turn", "stream-answer", "agent:main:stream-recovery"),
        surface=SurfaceCapabilities(
            supports_streaming=streaming,
            supports_edit=reset,
            supports_generation_reset=reset,
        ),
    )


def _agent(
    provider: _Provider, *, context: TurnExecutionContext | None,
    tool_handler: Any = None, **overrides: Any,
) -> Agent:
    settings = {
        "max_iterations": 1,
        "max_provider_retries": 3,
        "retry_base_backoff_ms": 0,
        "retry_max_backoff_ms": 0,
        "provider_id": "openai",
        "model_id": "synthetic-stream-model",
    }
    settings.update(overrides)
    tools = [] if tool_handler is None else [ToolDefinition(
        name="record", description="Record a synthetic value.",
        input_schema=ToolInputSchema(
            properties={"value": {"type": "string"}}, required=["value"],
        ),
    )]
    return Agent(
        provider=provider, config=AgentConfig(**settings), execution_context=context,
        tool_definitions=tools, tool_handler=tool_handler,
        usage_tracker=UsageTracker(), session_key="agent:main:stream-recovery",
    )


def _partial(error: Any) -> list[Any]:
    return [ProviderReasoning(text="discard reasoning"), ProviderText(text="discard text"), error]


def _success(text: str = "Recovered.", *, billed_cost: float = 0.02) -> list[Any]:
    return [ProviderText(text=text), ProviderDone(
        stop_reason="stop", model="synthetic-stream-model", provider="openai",
        input_tokens=10, output_tokens=2, billed_cost=billed_cost,
        cost_source="provider_billed",
    )]


async def _run(agent: Agent) -> list[Any]:
    return [event async for event in agent.run_turn("Complete the synthetic task.")]


def _resets(events: list[Any]) -> list[AnswerGenerationResetEvent]:
    return [event for event in events if isinstance(event, AnswerGenerationResetEvent)]


@pytest.mark.parametrize("error", [
    httpx.ReadTimeout("synthetic idle read"),
    httpx.ReadError("synthetic interrupted read"),
    ProviderError(
        code="request_error", message="synthetic connection interruption",
        recovery_origin="transport_disconnect",
    ),
])
async def test_interrupted_visible_response_replays_one_frozen_request(error: Any) -> None:
    provider = _Provider([_partial(error), _success()])
    events = await _run(_agent(provider, context=_context()))

    assert len(provider.calls) == 2
    assert provider.opened[0].closed
    assert provider.calls[0][0:2] == provider.calls[1][0:2]
    assert provider.calls[1][2] is not None
    assert provider.calls[1][2].physical_attempt_limit == 1
    assert len(_resets(events)) == 1
    reset = _resets(events)[0]
    assert reset.reset_scope == "provider_call"
    assert reset.rollback_model_call_id == "1.0"
    assert reset.new_generation_epoch == reset.old_generation_epoch + 1
    reset_index = events.index(reset)
    wait_index = next(index for index, event in enumerate(events)
                      if event.kind == "provider_activity" and event.phase == "retry_wait")
    assert reset_index < wait_index
    done = next(event for event in events if isinstance(event, DoneEvent))
    assert done.text == "Recovered."
    assert "discard" not in (done.reasoning_content or "")
    assert done.generation_epoch == 1
    assert done.router_model_call_id == "1.1"
    assert not any(isinstance(event, ErrorEvent) for event in events)


@pytest.mark.parametrize(("failure", "kind"), [
    (ProviderError(code="401", message="unauthorized", http_status=401), "auth_invalid"),
    (ProviderError(code="insufficient_quota", message="quota exhausted"), "insufficient_credits"),
    (RuntimeError("synthetic program failure"), "unknown"),
])
async def test_replacement_failure_keeps_its_actual_error_category(failure, kind) -> None:
    provider = _Provider([_partial(httpx.ReadTimeout("synthetic")), [failure], _success()])
    events = await _run(_agent(provider, context=_context()))
    assert len(provider.calls) == 2
    terminal = next(event for event in events if isinstance(event, ErrorEvent))
    assert terminal.failure_kind == kind


async def test_recovery_retargets_applied_steer_without_applying_it_twice() -> None:
    pending = ListPendingInputProvider()

    class SteeringProvider(_Provider):
        def chat(self, messages, tools=None, config=None):
            first = not self.calls
            stream = super().chat(messages, tools=tools, config=config)

            async def produce():
                async for event in stream:
                    yield event
                if first:
                    pending.append("Synthetic continuation input")

            return produce()

    provider = SteeringProvider([
        _success("Accepted prefix."),
        _partial(httpx.ReadTimeout("synthetic")),
        _success("Accepted suffix."),
    ])
    agent = _agent(provider, context=_context(), max_iterations=3)
    events = [event async for event in agent.run_turn(
        "Synthetic request", pending_input_provider=pending,
    )]
    done = next(event for event in events if isinstance(event, DoneEvent))

    assert len(provider.calls) == 3
    assert provider.calls[1][0] == provider.calls[2][0]
    assert len(pending.applications) == 1
    assert pending.applications[0].model_call_id == "2.0"
    assert done.text == "Accepted prefix.Accepted suffix."
    assert done.router_model_call_id == "1.0"
    assert done.model_call_segments == [{
        "model_call_id": "2.1", "iteration": 2,
        "start_codepoint": len("Accepted prefix."),
        "end_codepoint": len(done.text),
    }]


@pytest.mark.parametrize("terminal", [
    RuntimeError("timeout in synthetic program"),
    TimeoutError("unknown local deadline"),
    httpx.LocalProtocolError("synthetic malformed local request"),
    ProviderError(code="provider_protocol_error", message="synthetic invalid protocol"),
    ProviderError(code="request_error", message="unknown failure mentioning timeout"),
    None,
], ids=["program", "unknown-timeout", "local-protocol", "protocol", "unproven", "clean-eof"])
async def test_unproven_or_protocol_failure_does_not_gain_postcontent_replay(terminal: Any) -> None:
    failed = _partial(terminal)
    if terminal is None:
        failed.pop()
    provider = _Provider([failed, _success()])

    events = await _run(_agent(provider, context=_context()))

    assert len(provider.calls) == 1
    assert _resets(events) == []
    assert any(isinstance(event, ErrorEvent) for event in events)


@pytest.mark.parametrize("mode", ["no-context", "unsupported-live", "disabled"])
async def test_visible_replay_requires_supported_context_and_existing_budget(mode: str) -> None:
    provider = _Provider([_partial(httpx.ReadTimeout("synthetic read")), _success()])
    context = None if mode == "no-context" else _context(reset=mode != "unsupported-live")
    events = await _run(_agent(
        provider, context=context, max_provider_retries=0 if mode == "disabled" else 3,
    ))

    assert len(provider.calls) == 1
    assert _resets(events) == []
    assert any(isinstance(event, ErrorEvent) for event in events)


def _tool_response(index: int) -> list[Any]:
    call_id = f"tool-{index}"
    return [
        ProviderText(text=f"Completed step {index}. "),
        ProviderToolStart(tool_use_id=call_id, tool_name="record"),
        ProviderToolEnd(tool_use_id=call_id, tool_name="record", arguments={"value": str(index)}),
        ProviderDone(stop_reason="tool_use", model="synthetic-stream-model"),
    ]


async def test_replay_keeps_three_completed_steps_without_executing_failed_tool_batch() -> None:
    failed = [
        ProviderReasoning(text="discard reasoning"),
        ProviderText(text="discard text"),
        ProviderToolStart(tool_use_id="failed-tool", tool_name="record"),
        ProviderToolDelta(tool_use_id="failed-tool", json_fragment='{"value":"discard'),
        httpx.ReadTimeout("synthetic read timeout"),
    ]
    provider = _Provider([*[_tool_response(index) for index in range(1, 4)], failed,
                          _tool_response(4), _success("Finished.")])
    executed: list[str] = []

    async def handle(call: Any) -> ToolResult:
        executed.append(call.tool_use_id)
        return ToolResult(
            tool_use_id=call.tool_use_id, tool_name=call.tool_name, content="recorded",
        )

    events = await _run(_agent(provider, context=_context(), tool_handler=handle, max_iterations=5))

    assert executed == ["tool-1", "tool-2", "tool-3", "tool-4"]
    assert len(provider.calls) == 6
    assert provider.calls[3][0:2] == provider.calls[4][0:2]
    assert not any(getattr(event, "tool_use_id", "") == "failed-tool" for event in events)
    assert len(_resets(events)) == 1
    assert _resets(events)[0].rollback_model_call_id == "4.0"
    done = next(event for event in events if isinstance(event, DoneEvent))
    assert done.text == (
        "Completed step 1. Completed step 2. Completed step 3. Completed step 4. Finished."
    )
    assert "discard" not in (done.reasoning_content or "")
    assert all("discard" not in str(call[0]) for call in provider.calls[4:])


async def test_later_tool_steps_keep_their_own_existing_retry_allowance() -> None:
    streams = []
    for index in range(1, 4):
        streams.extend([_partial(httpx.ReadTimeout("synthetic read")), _tool_response(index)])
    provider = _Provider([*streams, _success("Finished.")])
    executed: list[str] = []

    async def handle(call: Any) -> ToolResult:
        executed.append(call.tool_use_id)
        return ToolResult(
            tool_use_id=call.tool_use_id, tool_name=call.tool_name, content="recorded",
        )

    events = await _run(_agent(
        provider, context=_context(), tool_handler=handle,
        max_iterations=4, max_provider_retries=1,
    ))

    assert len(provider.calls) == 7
    assert len(_resets(events)) == 3
    assert executed == ["tool-1", "tool-2", "tool-3"]
    assert not any(isinstance(event, ErrorEvent) for event in events)


async def test_second_interrupted_attempt_stops_without_another_replay() -> None:
    provider = _Provider([_partial(httpx.ReadTimeout("first")),
                          _partial(httpx.ReadTimeout("second")), _success()])
    events = await _run(_agent(provider, context=_context(), max_provider_retries=3))

    assert len(provider.calls) == 2
    assert len(_resets(events)) == 1
    assert any(isinstance(event, ErrorEvent) for event in events)


@pytest.mark.parametrize("second_fails", [False, True])
async def test_replay_binds_selected_physical_leg_without_restarting_fallback(
    monkeypatch: Any, second_fails: bool,
) -> None:
    from opensquilla.engine.runtime import _SelectorFallbackProvider
    from opensquilla.provider.selector import ModelSelector, ProviderConfig, SelectorConfig

    providers = {
        "primary": _Provider([[ProviderError(code="503", message="temporarily overloaded")]]),
        "secondary": _Provider([
            _partial(httpx.ReadTimeout("first interruption")),
            _partial(httpx.ReadTimeout("second interruption")) if second_fails else _success(),
        ]),
        "unused": _Provider([_success("Unexpected fallback.")]),
    }
    configs = [ProviderConfig(
        provider="openai", model=name, api_key="dummy", base_url=f"https://{name}.test",
    ) for name in providers]
    monkeypatch.setattr(
        "opensquilla.provider.selector._build_provider", lambda config: providers[config.model],
    )
    selector = ModelSelector(SelectorConfig(primary=configs[0], fallbacks=configs[1:]))
    wrapped = _SelectorFallbackProvider(selector.resolve(), selector)

    events = await _run(_agent(wrapped, context=_context()))

    assert len(providers["primary"].calls) == 1
    assert len(providers["secondary"].calls) == 2
    assert providers["unused"].calls == []
    assert len(_resets(events)) == 1
    assert bool([event for event in events if isinstance(event, ErrorEvent)]) is second_fails


async def test_cancellation_during_replay_wait_starts_no_new_request(monkeypatch: Any) -> None:
    waiting = asyncio.Event()

    async def wait(_delay: float) -> None:
        waiting.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("opensquilla.engine.agent.sleep_before_retry", wait)
    provider = _Provider([_partial(httpx.ReadTimeout("synthetic read")), _success()])
    task = asyncio.create_task(_run(_agent(provider, context=_context())))
    await asyncio.wait_for(waiting.wait(), timeout=2)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(provider.calls) == 1
    assert provider.opened[0].closed


async def test_existing_turn_deadline_bounds_the_replay_wait(monkeypatch: Any) -> None:
    async def wait(_delay: float) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr("opensquilla.engine.agent.sleep_before_retry", wait)
    provider = _Provider([_partial(httpx.ReadTimeout("synthetic read")), _success()])
    events = await asyncio.wait_for(
        _run(_agent(provider, context=_context(), timeout=0.02)), timeout=2,
    )

    assert len(provider.calls) == 1
    assert any(isinstance(event, ErrorEvent) and event.code == "agent_runtime_timeout"
               for event in events)


async def test_existing_turn_call_budget_also_counts_replay_requests() -> None:
    provider = _Provider([_partial(httpx.ReadTimeout("synthetic read")), _success()])
    events = await _run(_agent(provider, context=_context(), max_turn_llm_calls=1))

    assert len(provider.calls) == 1
    assert any(isinstance(event, ErrorEvent) for event in events)


async def test_stream_close_timeout_suppresses_replay() -> None:
    provider = _Provider(
        [_partial(httpx.ReadTimeout("synthetic read")), _success()], stall_first_close=True,
    )
    events = await asyncio.wait_for(_run(_agent(provider, context=_context())), timeout=3)

    assert len(provider.calls) == 1
    assert provider.opened[0].closed is False
    assert _resets(events) == []
    assert any(isinstance(event, ErrorEvent) for event in events)


async def test_replay_accumulates_failed_and_successful_receipts_once() -> None:
    failed = ProviderError(
        code="request_error", message="synthetic interrupted read", recovery_origin="read_timeout",
        model_usage_breakdown=[{
            "provider": "openai", "model": "synthetic-stream-model",
            "input_tokens": 20, "output_tokens": 3,
            "billed_cost": 0.03, "cost_source": "provider_billed",
        }],
    )
    provider = _Provider([_partial(failed), _success(billed_cost=0.02)])
    events = await _run(_agent(provider, context=_context()))

    assert len(provider.calls) == 2
    done = next(event for event in events if isinstance(event, DoneEvent))
    assert done.billed_cost == pytest.approx(0.05)
    assert done.input_tokens == 30
    assert done.output_tokens == 5
    assert done.missing_cost_entries == 0


async def test_failed_attempt_without_receipt_remains_an_accounting_gap() -> None:
    provider = _Provider([_partial(httpx.ReadTimeout("synthetic read")), _success()])
    events = await _run(_agent(provider, context=_context()))

    assert len(provider.calls) == 2
    done = next(event for event in events if isinstance(event, DoneEvent))
    assert done.billed_cost == pytest.approx(0.02)
    assert done.missing_cost_entries > 0


@pytest.mark.parametrize("close_kind", ["raises", "missing"])
async def test_selector_raw_stream_requires_confirmed_close_before_replay(
    monkeypatch: pytest.MonkeyPatch, close_kind: str,
) -> None:
    from opensquilla.engine.runtime import _SelectorFallbackProvider
    from opensquilla.provider.selector import ModelSelector, ProviderConfig, SelectorConfig

    class UnclosedStream(_Stream):
        async def aclose(self) -> None:
            raise RuntimeError("synthetic close failure")

    class UnclosedProvider(_Provider):
        def chat(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
            stream = super().chat(*args, **kwargs)
            assert isinstance(stream, _Stream)
            if len(self.calls) == 1:
                stream.__class__ = UnclosedStream
                if close_kind == "missing":
                    stream.aclose = None  # type: ignore[method-assign, assignment]
            return stream

    provider = UnclosedProvider([_partial(httpx.ReadTimeout("synthetic read")), _success()])
    monkeypatch.setattr("opensquilla.provider.selector._build_provider", lambda config: provider)
    selector = ModelSelector(SelectorConfig(primary=ProviderConfig(
        provider="openai", model="synthetic-model", api_key="dummy", base_url="https://model.test",
    )))
    wrapped = _SelectorFallbackProvider(selector.resolve(), selector)
    events = await asyncio.wait_for(_run(_agent(wrapped, context=_context())), timeout=3)

    assert len(provider.calls) == 1
    assert not provider.opened[0].closed
    assert _resets(events) == []
    assert any(isinstance(event, ErrorEvent) for event in events)


@pytest.mark.parametrize("tracked", [False, True])
@pytest.mark.parametrize("token_usage_known", [False, True])
async def test_interrupted_counter_estimates_match_ledger_after_success(
    tracked: bool, token_usage_known: bool,
) -> None:
    from opensquilla.engine.usage_accounting import (
        UsageAccountingScope,
        UsageExecutionContext,
        bind_usage_accounting_scope,
        normalize_provider_usage,
        usd_to_nanos,
    )

    class Sink:
        def __init__(self) -> None:
            self.results: list[Any] = []

        async def start(self, call: Any) -> None:
            pass

        async def finalize(self, call: Any, result: Any) -> None:
            self.results.append(result)

        async def mark_unknown(self, call: Any, reason: str) -> None:
            pytest.fail("partial usage should have a settlement")

    failed = ProviderError(
        code="timeout", recovery_origin="read_timeout", usage_missing_count=1,
        model_usage_breakdown=[{
            "provider": "anthropic", "model": "claude-sonnet-4-6",
            "input_tokens": 15_000, "output_tokens": 4_000 if token_usage_known else 0,
            "token_usage_known": token_usage_known,
            "cost_source": "unavailable",
        }],
    )
    provider = _Provider([_partial(failed), [
        ProviderText(text="Recovered."), ProviderDone(
            model="claude-sonnet-4-6", provider="anthropic",
            input_tokens=10, output_tokens=2, billed_cost=0.02, cost_source="provider_billed",
        ),
    ]])
    agent = _agent(
        provider, context=_context(), provider_id="anthropic", model_id="claude-sonnet-4-6",
    )
    if not tracked:
        agent._usage_tracker = None
    sink = Sink()
    with bind_usage_accounting_scope(UsageAccountingScope(
        sink=sink, context=UsageExecutionContext(execution_id="partial", agent_run_id="partial"),
    )):
        events = await _run(agent)
    done = next(event for event in events if isinstance(event, DoneEvent))
    failed_estimate = normalize_provider_usage(
        failed, default_provider="anthropic", default_model="claude-sonnet-4-6",
        completed_at_ms=1,
    ).estimated_cost_nanos / 1_000_000_000
    assert (failed_estimate > 0) is token_usage_known
    assert done.input_tokens == 15_010
    assert done.cost_usd == pytest.approx(0.02 + failed_estimate)
    assert done.billed_cost == pytest.approx(0.02)
    assert done.missing_cost_entries >= 1
    ledger_cost = sum(
        result.billed_cost_nanos + result.estimated_cost_nanos for result in sink.results
    )
    assert ledger_cost == usd_to_nanos(done.cost_usd)
    assert sum(row["cost_usd"] for row in done.model_usage_breakdown) == pytest.approx(
        done.cost_usd,
    )


def test_model_rollup_retains_confirmed_zero_authority_with_unknown_sibling() -> None:
    from opensquilla.engine.agent import _summarize_model_usage_breakdown

    rows = _summarize_model_usage_breakdown([
        {"model": "synthetic-model", "provider": "openai", "cost_source": "provider_billed",
         "billed_cost": 0},
        {"model": "synthetic-model", "provider": "openai", "cost_source": "unavailable",
         "missing_cost_entries": 1},
    ])
    assert len(rows) == 1
    assert rows[0]["cost_source"] == "mixed"
    assert rows[0]["cost_usd"] == 0
    assert rows[0]["has_billed_component"] is True
    assert rows[0]["has_estimated_component"] is False
    rerolled = _summarize_model_usage_breakdown([
        {**rows[0], "_opensquilla_reported_cost": True},
    ])
    assert rerolled[0]["has_billed_component"] is True
    assert rerolled[0]["has_estimated_component"] is False


@pytest.mark.parametrize("tracked", [False, True])
@pytest.mark.parametrize("confirmed_zero", [False, True])
async def test_recovered_done_preserves_zero_receipt_authority_from_durable_ledger(
    tracked: bool, confirmed_zero: bool,
) -> None:
    from opensquilla.engine.usage_accounting import (
        UsageAccountingScope,
        UsageExecutionContext,
        bind_usage_accounting_scope,
    )
    from opensquilla.gateway.usage_ledger_runtime import SessionUsageEventSink
    from opensquilla.session.models import SessionNode
    from opensquilla.session.storage import SessionStorage

    storage = await SessionStorage.open(":memory:")
    try:
        await storage.upsert_session(SessionNode(
            session_key="agent:main:stream-recovery", session_id="zero-receipt-session",
        ))
        await storage.initialize_usage_ledger(1)
        failed = ProviderError(
            code="timeout", recovery_origin="read_timeout", usage_missing_count=1,
            model_usage_breakdown=[{
                "provider": "openai", "model": "synthetic-stream-model",
                "input_tokens": 10, "output_tokens": 2, "billed_cost": 0,
                "token_usage_known": confirmed_zero,
                "cost_source": "provider_billed" if confirmed_zero else "unavailable",
            }],
        )
        provider = _Provider([_partial(failed), [
            ProviderText(text="Recovered."),
            ProviderDone(model="synthetic-stream-model", usage_missing_count=1),
        ]])
        agent = _agent(provider, context=_context())
        if not tracked:
            agent._usage_tracker = None
        with bind_usage_accounting_scope(UsageAccountingScope(
            sink=SessionUsageEventSink(storage),
            context=UsageExecutionContext(
                execution_id="zero-receipt-execution", agent_run_id="zero-receipt-run",
                session_id="zero-receipt-session", turn_id="stream-turn",
            ),
        )):
            events = await _run(agent)

        done = next(event for event in events if isinstance(event, DoneEvent))
        projection = await storage.get_turn_usage_projection(
            session_id="zero-receipt-session", session_epoch=0, turn_id="stream-turn",
        )
        assert projection is not None
        assert projection["has_billed_component"] is confirmed_zero
        assert projection["has_estimated_component"] is False
        assert done.cost_source == projection["cost_source"] == (
            "mixed" if confirmed_zero else "unavailable"
        )
        assert done.cost_usd == projection["cost_usd"] == 0
        assert done.missing_cost_entries == projection["missing_cost_entries"] > 0
        assert len(provider.calls) == 2
        assert not any(isinstance(event, ErrorEvent) for event in events)
    finally:
        await storage.close()
