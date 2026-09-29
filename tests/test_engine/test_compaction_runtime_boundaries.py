"""Mandatory live state, parent deadlines, and physical deployment circuit scope."""

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from opensquilla.engine import Agent, AgentConfig
from opensquilla.engine import ErrorEvent as AgentErrorEvent
from opensquilla.engine.agent import _CompactionParentDeadlineError
from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.usage_accounting import (
    UsageAccountingBusyError,
    UsageAccountingUnavailableError,
)
from opensquilla.provider import (
    ContentBlockToolResult,
    ContentBlockToolUse,
    Message,
    OpenAIProvider,
)
from opensquilla.provider.ensemble import EnsembleMemberConfig, EnsembleProvider
from opensquilla.provider.selector import ProviderConfig
from opensquilla.provider.types import ChatConfig
from opensquilla.session.compaction import (
    CompactionConfig,
    CompactionRequest,
    CompactionRequestContext,
    CompactionResult,
)
from opensquilla.session.compaction_lifecycle import CompactionTimeoutError


def make_agent():
    provider = OpenAIProvider(api_key="unused-synthetic", model="synthetic")
    agent = Agent(provider=provider, config=AgentConfig(
        model_id="synthetic", context_window_tokens=16000, max_tokens=1024,
    ))
    agent._current_turn_message = "current exact request"
    return agent


def tool_messages(content, *, partial=False):
    calls = [ContentBlockToolUse(id="call-a", name="lookup", input={"item": "synthetic"})]
    if partial:
        calls.append(ContentBlockToolUse(id="call-b", name="lookup", input={"item": "pending"}))
    return [
        Message(role="user", content="current exact request"),
        Message(role="assistant", content=calls),
        Message(role="user", content=[ContentBlockToolResult(
            tool_use_id="call-a", content=content,
        )]),
    ]


@pytest.mark.parametrize("status", ["running", "pending", "error", "completed"])
@pytest.mark.parametrize("nested", [False, True])
def test_legacy_json_tool_status_is_preserved_only_while_live(status, nested):
    agent = make_agent()
    payload = {"status": status}
    if nested:
        payload = {"execution_status": payload}
    messages = tool_messages(json.dumps(payload))
    before = [message.model_copy(deep=True) for message in messages]
    boundary = agent._live_turn_compaction_boundary(messages, protected_turn_start_index=0)
    assert boundary == (None if status in {"running", "pending"} else (0, len(messages)))
    assert messages == before


def test_partial_parallel_tool_results_do_not_close_the_live_round():
    agent = make_agent()
    messages = tool_messages("Completed first call", partial=True)
    assert agent._live_turn_compaction_boundary(messages, protected_turn_start_index=0) is None
    before = [message.model_copy(deep=True) for message in messages]
    assert agent._recover_local_request_window(
        messages, protected_turn_start_index=0, request_context_insert_index=0,
        runtime_context_insert_index=0, input_budget_tokens=1,
    ) is None
    assert messages == before


@pytest.mark.parametrize("enabled", [False, True])
async def test_expired_parent_starts_neither_summary_nor_local_window(monkeypatch, enabled):
    agent = make_agent()
    agent.config.compaction_enabled = enabled
    parent = agent.build_compaction_request_context().chat_config.model_copy(update={
        "turn_deadline_at_monotonic": time.monotonic() - 1,
    })
    reports = []
    agent.config.compaction_outcome_reporter = reports.append
    summary = MagicMock(side_effect=AssertionError("expired turn started a summary"))
    monkeypatch.setattr("opensquilla.engine.agent.compact_context", summary)
    messages = tool_messages("Completed ordinary result " * 400)
    for recover in (
        lambda: agent._recover_local_request_window(
            messages, protected_turn_start_index=0, request_context_insert_index=0,
            runtime_context_insert_index=0, config=parent,
        ),
        lambda: agent._require_compaction_parent_time(parent),
    ):
        with pytest.raises(_CompactionParentDeadlineError):
            recover()
    with pytest.raises(_CompactionParentDeadlineError):
        await agent._recover_live_turn_request_overflow(
            messages, protected_turn_start_index=0, context_window_tokens=8000,
            request_context_insert_index=0, runtime_context_insert_index=0,
            consumer_chat_config=parent,
        )
    assert summary.call_count == 0
    assert reports == []


@pytest.mark.parametrize("entrypoint", ["execute", "live_recovery"])
@pytest.mark.ci_serial
async def test_parent_expiry_cancels_inflight_summary_without_failure_or_window(
    monkeypatch, entrypoint,
):
    agent = make_agent()
    parent_deadline = time.monotonic() + 1
    parent = agent.build_compaction_request_context().chat_config.model_copy(update={
        "turn_deadline_at_monotonic": parent_deadline,
    })
    reports = []
    agent.config.compaction_outcome_reporter = reports.append
    cancelled = asyncio.Event()
    observed = []

    async def waiting_summary(request):
        observed.append(request.config.deadline_at_monotonic)
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr("opensquilla.engine.agent.compact_context", waiting_summary)
    monkeypatch.setattr(agent, "_recover_local_request_window", MagicMock(
        side_effect=AssertionError("parent expiry entered local fallback"),
    ))
    with pytest.raises(_CompactionParentDeadlineError):
        if entrypoint == "execute":
            await agent._execute_compaction_request(CompactionRequest(
                session_id="parent-deadline", entries=[], context_window_tokens=8000,
                config=CompactionConfig(request_context=CompactionRequestContext(chat_config=parent)),
            ))
        else:
            await agent._recover_live_turn_request_overflow(
                tool_messages("Completed ordinary result " * 400), protected_turn_start_index=0,
                context_window_tokens=8000, request_context_insert_index=0,
                runtime_context_insert_index=0, consumer_chat_config=parent,
            )
    assert observed == [parent_deadline]
    assert cancelled.is_set()
    assert reports == []


async def test_explicit_cancellation_remains_cancellation_without_failure(monkeypatch):
    agent = make_agent()
    reports = []
    agent.config.compaction_outcome_reporter = reports.append
    started = asyncio.Event()

    async def waiting_summary(request):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("opensquilla.engine.agent.compact_context", waiting_summary)
    task = asyncio.create_task(agent._execute_compaction_request(CompactionRequest(
        session_id="explicit-cancel", entries=[], context_window_tokens=8000,
        config=CompactionConfig(request_context=CompactionRequestContext(chat_config=ChatConfig(
            turn_deadline_at_monotonic=time.monotonic() + 30,
        ))),
    )))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert reports == []


@pytest.mark.parametrize("raised", [False, True])
async def test_pre_dispatch_refusal_does_not_count_as_a_service_failure(monkeypatch, raised):
    agent = make_agent()
    reports = []
    agent.config.compaction_outcome_reporter = reports.append

    async def refused(request):
        request.config.llm_calls_started = 1  # A reservation is not a provider dispatch.
        if raised:
            raise ValueError("local preparation failed")
        return CompactionResult(
            summary="", kept_entries=request.entries, removed_count=0, chunks_processed=0,
            skip_reason="summary_failed",
        )

    monkeypatch.setattr("opensquilla.engine.agent.compact_context", refused)
    request = CompactionRequest(session_id="no-dispatch", entries=[], context_window_tokens=8000)
    if raised:
        with pytest.raises(ValueError, match="local preparation"):
            await agent._execute_compaction_request(request)
    else:
        await agent._execute_compaction_request(request)
    assert reports == []


async def test_dispatched_operation_counts_once_and_preserves_existing_start_hook(monkeypatch):
    agent = make_agent()
    reports = []
    starts = []
    agent.config.compaction_outcome_reporter = reports.append

    async def failed(request):
        request.config.on_summary_call_started()
        request.config.on_summary_call_started()
        return CompactionResult(
            summary="", kept_entries=request.entries, removed_count=0, chunks_processed=0,
            skip_reason="summary_failed",
        )

    monkeypatch.setattr("opensquilla.engine.agent.compact_context", failed)
    def original():
        starts.append(True)
    config = CompactionConfig(on_summary_call_started=original)
    request = CompactionRequest(
        session_id="dispatched", entries=[], context_window_tokens=8000, config=config,
    )
    await agent._execute_compaction_request(request)
    await agent._execute_compaction_request(request)
    assert reports == [False]
    assert starts == [True] * 4
    assert config.on_summary_call_started is original


@pytest.mark.parametrize("error_type", [UsageAccountingBusyError, UsageAccountingUnavailableError])
@pytest.mark.parametrize("entry", ["live_turn", "inline"])
async def test_accounting_failure_is_not_converted_to_summary_failure(
    monkeypatch, error_type, entry,
):
    agent = make_agent()
    reports = []
    agent.config.compaction_outcome_reporter = reports.append
    failure = error_type("synthetic ledger admission failed")

    async def failed(request):
        request.config.on_summary_call_started()
        raise failure

    monkeypatch.setattr("opensquilla.engine.agent.compact_context", failed)
    monkeypatch.setattr(agent, "_recover_local_request_window", MagicMock(
        side_effect=AssertionError("ledger failure entered local fallback"),
    ))
    with pytest.raises(error_type) as raised:
        if entry == "live_turn":
            await agent._recover_live_turn_request_overflow(
                tool_messages("Completed ordinary result " * 400), protected_turn_start_index=0,
                context_window_tokens=8000, request_context_insert_index=0,
                runtime_context_insert_index=0,
            )
        else:
            await agent._check_context_overflow(
                [Message(role="user", content="completed question"),
                 Message(role="assistant", content="completed answer " * 5000),
                 Message(role="user", content=agent._current_turn_message)],
                estimated_context_tokens=20000, protected_turn_start_index=2,
                request_context_insert_index=2, runtime_context_insert_index=2,
                provider_overflow=True,
            )
    assert raised.value is failure
    assert reports == []


@pytest.mark.parametrize("failure", [
    OSError("synthetic storage commit failed"),
    UsageAccountingBusyError("synthetic ledger unavailable"),
])
async def test_preflight_owner_errors_remain_errors_without_a_window(monkeypatch, failure):
    from tests.test_engine.test_preflight_compaction import (
        _make_entry,
        _ResultCompactionSessionManager,
    )

    class BrokenOwner(_ResultCompactionSessionManager):
        async def compact_with_result(self, *args, **kwargs):
            raise failure

    manager = BrokenOwner([_make_entry("history " * 1000)])
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=manager)
    monkeypatch.setattr(runner, "_prepare_request_window", MagicMock(
        side_effect=AssertionError("owner failure entered local fallback"),
    ))
    with pytest.raises(type(failure)) as raised:
        await runner._maybe_preflight_compact("synthetic:owner", 1000)
    assert raised.value is failure
    assert "synthetic:owner" not in runner._compaction_failures


async def test_preflight_local_refusals_do_not_open_service_circuit():
    from tests.test_engine.test_preflight_compaction import (
        _make_entry,
        _ResultCompactionSessionManager,
    )

    class RefusingOwner(_ResultCompactionSessionManager):
        async def compact_with_result(self, *args, **kwargs):
            return CompactionResult(
                summary="", kept_entries=[], removed_count=0, chunks_processed=0,
                skip_reason="summary_call_budget_exceeded",
            )

    key = "synthetic:no-dispatch"
    manager = RefusingOwner([_make_entry("history " * 1000)])
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=manager)
    for _ in range(4):
        await runner._maybe_preflight_compact(key, 1000)
        runner.clear_compaction_turn_state(key)
    assert key not in runner._compaction_failures


@pytest.mark.parametrize("phase", ["checkpointing", "summarizing"])
@pytest.mark.parametrize("window_available", [False, True])
async def test_preflight_timeout_emits_only_the_selected_terminal(
    monkeypatch, phase, window_available,
):
    from tests.test_engine.test_preflight_compaction import (
        _make_entry,
        _ResultCompactionSessionManager,
    )

    class TimedOutOwner(_ResultCompactionSessionManager):
        async def compact_with_result(self, session_key, window, config=None, **kwargs):
            config.on_summary_call_started()
            raise CompactionTimeoutError("summarizing", 0.01)

    manager = TimedOutOwner([
        _make_entry("old background " * 2000), _make_entry("completed old reply", "assistant"),
    ])
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=manager)
    if phase == "checkpointing":
        monkeypatch.setattr(runner, "_record_checkpoint_before_compaction", AsyncMock(
            side_effect=CompactionTimeoutError("checkpointing", 0.01),
        ))
    if not window_available:
        monkeypatch.setattr(runner, "_prepare_request_window", AsyncMock(return_value=None))
    events = []
    monkeypatch.setattr("opensquilla.engine.runtime.notify_compaction",
                        lambda key, **payload: events.append(payload))

    prepared = await runner._maybe_preflight_compact("synthetic:terminal", 1000)

    assert (prepared is not None) is window_available
    terminal = "emergency_ephemeral" if window_available else "timed_out"
    assert [event["status"] for event in events] == ["started", terminal]
    assert events[-1]["reason"] == "compaction_deadline_exceeded"
    assert events[-1]["compaction_id"] == events[0]["compaction_id"]


@pytest.mark.parametrize("failure", ["timeout", "exception", "empty_applied_result"])
async def test_inline_recovery_emits_only_the_selected_window_terminal(monkeypatch, failure):
    agent = make_agent()
    agent._session_key = "synthetic:inline-terminal"
    messages = [
        Message(role="user", content="old background " * 6000),
        Message(role="assistant", content="completed old work"),
        Message(role="user", content=agent._current_turn_message),
    ]

    async def failed_summary(request):
        if failure == "timeout":
            raise CompactionTimeoutError("summarizing", 0.01)
        if failure == "exception":
            raise ValueError("synthetic local summary failure")
        return CompactionResult(
            summary="", kept_entries=request.entries[2:], removed_count=2, chunks_processed=1,
        )

    monkeypatch.setattr("opensquilla.engine.agent.compact_context", failed_summary)
    events = []
    monkeypatch.setattr("opensquilla.engine.agent.notify_compaction",
                        lambda key, **payload: events.append(payload))

    outcome = await agent._check_context_overflow(
        messages, 20000, protected_turn_start_index=2, request_context_insert_index=2,
        runtime_context_insert_index=2, provider_overflow=True,
    )

    assert outcome and outcome.ephemeral_only
    assert [event["status"] for event in events] == ["started", "emergency_ephemeral"]
    assert events[-1]["compaction_id"] == events[0]["compaction_id"]


@pytest.mark.ci_serial
async def test_real_turn_reports_parent_timeout_without_auxiliary_failure():
    from tests.test_engine.test_compaction_continuation import ContinuingProvider

    class WaitingSummaryProvider(ContinuingProvider):
        summary_started = False
        summary_cancelled = False

        async def chat(self, messages, tools=None, config=None):
            if config.candidate_output_mode == "inert_artifact":
                self.summary_started = True
                try:
                    await asyncio.Event().wait()
                finally:
                    self.summary_cancelled = True
            else:
                async for event in super().chat(messages, tools, config):
                    yield event

    provider = WaitingSummaryProvider()
    reports = []
    agent = Agent(provider=provider, config=AgentConfig(
        context_window_tokens=16000, max_tokens=1024, model_id="synthetic", timeout=1,
        compaction_outcome_reporter=reports.append,
    ))
    original = [Message(
        role="user" if index % 2 == 0 else "assistant",
        content=f"old item {index}: " + "a b c d " * 700,
    ) for index in range(8)]
    before = [message.model_copy(deep=True) for message in original]
    agent.set_history(original)
    events = [event async for event in agent.run_turn("continue the task")]
    assert provider.summary_started and provider.summary_cancelled
    assert any(isinstance(event, AgentErrorEvent) and event.code == "agent_runtime_timeout"
               for event in events)
    assert reports == []
    assert original == before
    assert agent.history_snapshot()[:len(original)] == before


def test_ensemble_takeover_rebinds_circuit_without_selector_changes():
    aggregator = ProviderConfig(provider="openai", model="aggregator", api_key="synthetic-a")
    fixed = ProviderConfig(provider="openai", model="fixed", api_key="synthetic-b")
    ensemble = EnsembleProvider(
        profile_name="test", proposers=[],
        aggregator=EnsembleMemberConfig(provider_config=aggregator),
        fallback_provider=OpenAIProvider(api_key="synthetic-b", model="fixed"),
        fallback_provider_name="openai", fallback_model="fixed",
        _fallback_request_budget_member=EnsembleMemberConfig(provider_config=fixed),
    )
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=None)
    key = "synthetic-physical-scope"

    def bind():
        identity = runner._compaction_failure_identity(
            provider=ensemble, provider_config=aggregator,
            chat_config=ChatConfig(max_tokens=1024), policy=(),
        )
        runner._bind_compaction_failure_scope(key, identity)

    bind()
    for _ in range(3):
        runner._record_compaction_failure(key)
    bind()
    assert runner._compaction_circuit_open(key)
    ensemble._fixed_takeover_active = True
    ensemble._fixed_takeover_role = "fixed_direct"
    bind()
    assert not runner._compaction_circuit_open(key)
    assert aggregator.model == "aggregator"


@pytest.mark.parametrize("change", [
    {"max_tokens": 2048}, {"thinking": True}, {"provider_request_max_chars_explicit_cap": 1234},
])
def test_effective_control_changes_rebind_circuit_but_parent_deadline_does_not(change):
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=None)
    provider = OpenAIProvider(api_key="unused-synthetic", model="synthetic")
    base = ChatConfig(max_tokens=1024)

    def identity(config):
        return runner._compaction_failure_identity(
            provider=provider, provider_config=None, chat_config=config, policy=(),
        )

    assert identity(base) == identity(base.model_copy(update={"turn_deadline_at_monotonic": 123.0}))
    assert identity(base) != identity(base.model_copy(update=change))


def test_same_model_credential_rotation_changes_circuit_identity_without_exposing_keys():
    config = ChatConfig(max_tokens=1024)
    identities = [TurnRunner._compaction_failure_identity(
        provider=OpenAIProvider(api_key=key, model="synthetic"),
        provider_config=None, chat_config=config, policy=(),
    ) for key in ("synthetic-old-credential", "synthetic-new-credential")]
    assert identities[0] != identities[1]
    assert "synthetic-old-credential" not in repr(identities)
    assert "synthetic-new-credential" not in repr(identities)
