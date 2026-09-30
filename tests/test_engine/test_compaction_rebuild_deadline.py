"""An expired local rebuild may use a proven window without committing its summary."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from opensquilla.engine import Agent, AgentConfig, ErrorEvent
from opensquilla.engine.agent import _CompactionParentDeadlineError
from opensquilla.engine.runtime import _SelectorFallbackProvider
from opensquilla.engine.types import CompactionEvent
from opensquilla.engine.usage_accounting import UsageAccountingUnavailableError
from opensquilla.provider import ErrorEvent as ProviderError
from opensquilla.provider import Message, OpenAIProvider
from opensquilla.provider.retry_after import record_provider_retry_after
from opensquilla.provider.selector import ModelSelector, ProviderConfig, SelectorConfig
from opensquilla.session.compaction import CompactionResult

CURRENT = "Continue this active request. " + "x" * 4000
CANDIDATE = "Uninstalled candidate checkpoint must never enter the retry."


class UsageSink:
    def __init__(self):
        self.starts = []

    async def start(self, call):
        self.starts.append(call)

    async def finalize(self, call, result):
        pass

    async def mark_unknown(self, call, reason):
        pass


class NativeContinuationProvider(OpenAIProvider):
    def __init__(self):
        super().__init__(api_key="synthetic", model="synthetic-deadline")
        self.projections = []

    async def chat(self, messages, tools=None, config=None):
        projection = self.project_final_request(messages, tools, config)
        self.projections.append(projection)
        if not projection.fits:
            yield ProviderError(
                code="provider_request_budget_exhausted", message=json.dumps(projection.proof),
            )
            return
        async for event in super().chat(messages, tools=tools, config=config):
            yield event


def setup_case(monkeypatch, tmp_path, *, body_timeout=False):
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "isolated-state"))
    provider = NativeContinuationProvider()
    sink = UsageSink()
    agent = Agent(
        provider=provider, session_key="agent:offline:rebuild-deadline", usage_event_sink=sink,
        config=AgentConfig(
            context_window_tokens=16_000, max_tokens=4096, max_provider_retries=0,
            max_overflow_retries=1, timeout=5.0, iteration_timeout=5.0,
            compaction_total_timeout_seconds=0.1,
        ),
    )
    history = [Message(role="user", content="Earlier question " + "q" * 20000),
               Message(role="assistant", content="Earlier answer " + "a" * 20000)]
    agent.set_history(history)
    case = SimpleNamespace(agent=agent, provider=provider, history=history, sink=sink,
                           notifications=[], summaries=[], physical=[], rebuilds=0,
                           body_error=TimeoutError("independent rebuild timeout"))

    async def compact(request):
        case.summaries.append(request)
        protected = int(request.config.protected_recent_messages or 0)
        cut = max(0, len(request.entries) - protected)
        return CompactionResult(summary=CANDIDATE, kept_entries=request.entries[cut:],
                                removed_count=cut, kept_start_index=cut, chunks_processed=1)

    monkeypatch.setattr("opensquilla.engine.agent.compact_context", compact)
    monkeypatch.setattr("opensquilla.engine.agent.notify_compaction",
                        lambda _, **payload: case.notifications.append(payload))
    original_rebuild = agent._provider_request_messages_async

    async def rebuild(messages, **kwargs):
        if agent._pending_durable_compaction_event is not None and not case.rebuilds:
            case.rebuilds += 1
            if body_timeout:
                raise case.body_error
            await asyncio.Event().wait()
        return await original_rebuild(messages, **kwargs)

    monkeypatch.setattr(agent, "_provider_request_messages_async", rebuild)

    def handle(request):
        case.physical.append(json.loads(request.content))
        chunks = [
            {"choices": [{"delta": {"content": "Continued safely."}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
        ]
        body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=body.encode())

    client = httpx.AsyncClient
    monkeypatch.setattr(
        "opensquilla.provider.openai.httpx.AsyncClient",
        lambda **kwargs: client(**{**kwargs, "transport": httpx.MockTransport(handle)}),
    )
    return case


def terminals(case):
    return [e for e in case.notifications if e.get("status") in {
        "completed", "emergency_ephemeral", "timed_out", "failed", "cancelled", "stale",
    }]


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapper_kind", ["direct", "selector"])
@pytest.mark.parametrize("wait_mode", ["hint_exceeds_install", "wait_expires"])
async def test_pending_cooldown_recovers_original_canonical_then_obeys_same_hint(
    monkeypatch, tmp_path, wrapper_kind, wait_mode,
):
    case = setup_case(monkeypatch, tmp_path)
    case.agent.config.compaction_total_timeout_seconds = 0.25
    if wait_mode == "wait_expires":
        first_wait = True

        async def stall_once(seconds):
            nonlocal first_wait
            if first_wait:
                first_wait = False
                await asyncio.Event().wait()
            await asyncio.sleep(seconds)

        monkeypatch.setattr("opensquilla.engine.agent.sleep_before_retry", stall_once)
        monkeypatch.setattr("opensquilla.engine.runtime.sleep_before_retry", stall_once)
    original_rebuild = Agent._provider_request_messages_async.__get__(case.agent)
    original_chat = case.provider.chat

    async def upstream_overflow(messages, tools=None, config=None):
        # Exercise an upstream-style overflow, whose durable pressure has not
        # been classified yet. The actual native projection still proves that
        # the original request is oversized for the local recovery guard.
        async for event in original_chat(messages, tools=tools, config=config):
            if (isinstance(event, ProviderError)
                    and event.code == "provider_request_budget_exhausted"):
                yield ProviderError(code="400", message="maximum context length exceeded")
            else:
                yield event

    monkeypatch.setattr(case.provider, "chat", upstream_overflow)
    # This extension reports upstream pressure; it exposes a native projection
    # but does not promise that every upstream cap is locally known.
    monkeypatch.setattr(case.provider, "final_request_admission_guaranteed", False)
    observed = []
    original_recovery = case.agent._recover_progressed_request_window

    async def rebuild(messages, **kwargs):
        if case.agent._pending_durable_compaction_event is not None and not case.rebuilds:
            case.rebuilds += 1
            case.usage_before_wait = len(case.sink.starts)
            record_provider_retry_after(case.provider, ProviderError(
                code="429", retry_after_s=0.1 if wait_mode == "wait_expires" else 0.5,
            ))
            config = ProviderConfig(
                provider="openai", model="synthetic-deadline", api_key="synthetic",
            )
            if wrapper_kind == "selector":
                monkeypatch.setattr(
                    "opensquilla.provider.selector._build_provider", lambda _: case.provider,
                )
                selector = ModelSelector(SelectorConfig(primary=config))
                case.agent.provider = _SelectorFallbackProvider(selector.resolve(), selector)
        return await original_rebuild(messages, **kwargs)

    async def recover(messages, **kwargs):
        assert messages[:len(case.history)] == case.history
        assert CANDIDATE not in json.dumps([m.model_dump() for m in messages])
        assert not case.physical
        assert len(case.sink.starts) == case.usage_before_wait
        observed.append(case.agent._pending_durable_compaction_event.compaction_id)
        return await original_recovery(messages, **kwargs)

    monkeypatch.setattr(case.agent, "_provider_request_messages_async", rebuild)
    monkeypatch.setattr(case.agent, "_recover_progressed_request_window", recover)
    started = asyncio.get_running_loop().time()
    events = [e async for e in case.agent.run_turn(CURRENT)]
    assert not [e for e in events if isinstance(e, ErrorEvent)], str(case.notifications)
    minimum_wait = 0.25 if wait_mode == "wait_expires" else 0.5
    assert asyncio.get_running_loop().time() - started >= minimum_wait
    assert len(observed) == len(case.summaries) == len(case.physical) == 1
    assert not any(isinstance(e, ErrorEvent) for e in events)
    assert any(e.kind == "done" for e in events)
    assert not any(isinstance(e, CompactionEvent) for e in events)
    assert case.agent.history_snapshot()[:len(case.history)] == case.history
    assert CANDIDATE not in json.dumps(case.physical[0])
    assert json.dumps(case.physical[0]).count(CURRENT) == 1
    assert case.agent._pending_durable_compaction_source is None
    terminal = terminals(case)
    assert len(terminal) == 1
    assert terminal[0]["compaction_id"] == observed[0]
    assert terminal[0]["status"] == "emergency_ephemeral"
    assert terminal[0]["durability"] == "request_scoped"
    assert len(case.sink.starts) == case.usage_before_wait + 1
    # A later deliberate turn must not inherit the abandoned candidate/source.
    # Give that independent request its larger actual model capacity.
    case.agent.config.context_window_tokens = 128_000
    case.agent._durable_consumer_window_tokens = 128_000
    next_events = [e async for e in case.agent.run_turn("Continue the next turn")]
    assert any(e.kind == "done" for e in next_events)
    assert not any(isinstance(e, ErrorEvent) for e in next_events)
    assert len(observed) == 1
    assert case.agent._pending_durable_compaction_source is None


@pytest.mark.asyncio
async def test_expired_local_rebuild_continues_native_window_without_summary_commit(
    monkeypatch, tmp_path,
):
    case = setup_case(monkeypatch, tmp_path)
    events = [e async for e in case.agent.run_turn(CURRENT)]
    assert not any(isinstance(e, ErrorEvent) for e in events)
    assert any(e.kind == "done" for e in events)
    assert not any(isinstance(e, CompactionEvent) for e in events)
    assert len(case.summaries) == 1
    assert len(case.provider.projections) == 2
    assert not case.provider.projections[0].fits
    assert case.provider.projections[1].fits
    assert case.physical == [case.provider.projections[1].payload]
    wire = json.dumps(case.physical[0])
    assert wire.count(CURRENT) == 1
    assert CANDIDATE not in wire
    assert case.agent.history_snapshot()[:len(case.history)] == case.history
    assert case.agent._pending_durable_compaction_event is None
    terminal = terminals(case)
    assert len(terminal) == 1
    assert terminal[0]["status"] == "emergency_ephemeral"
    assert terminal[0]["applied"] is True
    assert terminal[0]["durability"] == "request_scoped"
    assert terminal[0]["reason"] == "compaction_deadline_exceeded"
    started = next(e for e in case.notifications if e.get("status") == "started")
    assert terminal[0]["compaction_id"] == started["compaction_id"]


@pytest.mark.asyncio
async def test_internal_rebuild_timeout_keeps_original_authority(monkeypatch, tmp_path):
    case = setup_case(monkeypatch, tmp_path, body_timeout=True)
    events = [e async for e in case.agent.run_turn(CURRENT)]
    # The existing outer Agent timeout boundary owns TimeoutError normalization.
    # This local branch must not relabel it as a summary deadline or retry it.
    assert [e.code for e in events if isinstance(e, ErrorEvent)] == ["agent_runtime_timeout"]
    assert not case.physical
    assert len(terminals(case)) == 1
    assert terminals(case)[0]["status"] == "failed"
    assert terminals(case)[0]["reason"] == "request_rebuild_failed"
    assert case.agent.history_snapshot() == case.history


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["no_window", "cancel", "parent", "usage", "storage"])
async def test_window_recovery_retains_failure_owner_and_one_terminal(
    monkeypatch, tmp_path, failure,
):
    case = setup_case(monkeypatch, tmp_path)
    errors = {
        "cancel": asyncio.CancelledError(),
        "parent": _CompactionParentDeadlineError("expired parent"),
        "usage": UsageAccountingUnavailableError("ledger unavailable"),
        "storage": TimeoutError("independent storage timeout"),
    }

    async def fail(messages, **kwargs):
        assert messages[:len(case.history)] == case.history
        assert all(CANDIDATE not in json.dumps(m.model_dump()) for m in messages)
        assert case.agent._pending_durable_compaction_event is not None
        if failure == "no_window":
            return None
        raise errors[failure]

    monkeypatch.setattr(case.agent, "_recover_progressed_request_window", fail)
    if failure == "no_window":
        events = [e async for e in case.agent.run_turn(CURRENT)]
        assert any(isinstance(e, ErrorEvent) and e.code == "compaction_deadline_exceeded"
                   for e in events)
    elif failure in {"parent", "storage"}:
        events = [e async for e in case.agent.run_turn(CURRENT)]
        assert [e.code for e in events if isinstance(e, ErrorEvent)] == ["agent_runtime_timeout"]
    else:
        with pytest.raises(type(errors[failure])) as caught:
            _ = [e async for e in case.agent.run_turn(CURRENT)]
        assert caught.value is errors[failure]
    assert not case.physical
    assert case.agent.history_snapshot() == case.history
    terminal = terminals(case)
    assert len(terminal) == 1
    assert terminal[0]["status"] == (
        "timed_out" if failure == "no_window" else
        "cancelled" if failure in {"cancel", "parent"} else "failed"
    )
    assert terminal[0]["applied"] is False
    assert case.agent._pending_durable_compaction_event is None


@pytest.mark.asyncio
async def test_local_fallback_wait_cannot_outlive_original_parent(monkeypatch, tmp_path):
    case = setup_case(monkeypatch, tmp_path)
    case.agent.config.timeout = 0.5
    cancelled = []
    original_rebuild = case.agent._provider_request_messages_async

    async def wait_during_local_rebuild(*args, **kwargs):
        if case.rebuilds:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)
        return await original_rebuild(*args, **kwargs)

    monkeypatch.setattr(case.agent, "_provider_request_messages_async", wait_during_local_rebuild)
    started = asyncio.get_running_loop().time()
    events = [e async for e in case.agent.run_turn(CURRENT)]
    assert asyncio.get_running_loop().time() - started < 1.5
    assert cancelled == [True]
    assert [e.code for e in events if isinstance(e, ErrorEvent)] == ["agent_runtime_timeout"]
    terminal = terminals(case)
    assert len(terminal) == 1
    assert terminal[0]["status"] == "cancelled"
    assert terminal[0]["reason"] == "parent_deadline_exceeded"
    assert not case.physical
    assert case.agent.history_snapshot() == case.history


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["parent", "internal", "internal_without_parent"])
async def test_shared_window_helper_bounds_wait_and_preserves_inner_timeout(
    monkeypatch, tmp_path, failure,
):
    case = setup_case(monkeypatch, tmp_path)
    case.agent._current_turn_message = CURRENT
    messages = [*case.history, Message(role="user", content=CURRENT)]
    runtime = case.agent._freeze_preflight_runtime_context_message()
    config = case.agent._provider_admission_chat_config(
        CURRENT, context_window_tokens=16_000, max_output_tokens=4096,
    ).model_copy(update={"turn_deadline_at_monotonic": (
        None if failure == "internal_without_parent" else
        asyncio.get_running_loop().time() + (0.2 if failure == "parent" else 5.0)
    )})
    rejected = case.agent._provider_request_messages_for_count_projection(
        messages, request_context_message=None, request_context_insert_index=2,
        runtime_context_message=runtime, runtime_context_insert_index=2,
    )
    assert not case.provider.project_final_request(rejected, [], config).fits
    closed = []
    original_error = TimeoutError("independent local storage wait")

    async def wait(*args, **kwargs):
        try:
            if failure == "parent":
                await asyncio.Event().wait()
            raise original_error
        finally:
            closed.append(True)

    monkeypatch.setattr(case.agent, "_provider_request_messages_async", wait)
    expected = _CompactionParentDeadlineError if failure == "parent" else TimeoutError
    with pytest.raises(expected) as caught:
        await case.agent._recover_progressed_request_window(
            messages, rejected_messages=rejected, protected_turn_start_index=2,
            request_context_insert_index=2, runtime_context_insert_index=2,
            chat_config=config, tools=[], request_context_message=None,
            runtime_context_message=runtime, request_suffix_messages=[],
        )
    if failure != "parent":
        assert caught.value is original_error
        assert not isinstance(caught.value, _CompactionParentDeadlineError)
    assert closed == [True]
    assert not case.physical
    assert not terminals(case)
