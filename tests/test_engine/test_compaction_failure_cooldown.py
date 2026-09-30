"""A spent summary budget is not immediately spent again on the next turn."""

from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from opensquilla.compaction_status import compaction_failure_status
from opensquilla.engine.runtime import TurnRunner
from opensquilla.session.compaction import CompactionRequest, CompactionResult
from opensquilla.session.compaction_lifecycle import CompactionTimeoutError
from tests.test_engine.test_preflight_compaction import _make_entry, _ResultCompactionSessionManager


@pytest.mark.parametrize("append_message", [False, True])
async def test_full_budget_timeout_suppresses_next_auto_summary_after_optional_append(
    monkeypatch, append_message,
):
    class TimedOutOwner(_ResultCompactionSessionManager):
        async def compact_with_result(self, session_key, window, config=None, **kwargs):
            self.compact_with_result_calls.append((session_key, window, config))
            config.on_summary_call_started()
            raise CompactionTimeoutError("summarizing", 600)

    manager = TimedOutOwner([
        _make_entry("old history " * 2000),
        _make_entry("completed old answer", "assistant"),
    ])
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=manager)
    key = "test:spent-budget"
    recovery = AsyncMock(return_value=object())
    monkeypatch.setattr(runner, "_prepare_request_window", recovery)

    await runner._maybe_preflight_compact(key, 1000)
    runner.clear_compaction_turn_state(key)
    if append_message:
        manager._transcript.append(_make_entry("continue normally"))
    await runner._maybe_preflight_compact(key, 1000)

    assert len(manager.compact_with_result_calls) == 1
    assert recovery.await_count == 2
    assert recovery.await_args.kwargs["reason"] == "durable_compaction_circuit_open"
    assert runner._compaction_failures[key].count == 1
    assert runner._compaction_failures[key].failure_kind == "operation_timeout"


@pytest.mark.parametrize("failure_kind", ["provider_error", "incomplete", "summary_failed"])
async def test_quick_candidate_failures_keep_bounded_opportunities_before_cooling(
    monkeypatch, failure_kind,
):
    class FailedOwner(_ResultCompactionSessionManager):
        async def compact_with_result(self, session_key, window, config=None, **kwargs):
            self.compact_with_result_calls.append((session_key, window, config))
            config.on_summary_call_started()
            return SimpleNamespace(
                summary="", kept_entries=[], removed_count=0, chunks_processed=1,
                skip_reason="summary_failed", failure_kind=failure_kind,
            )

    manager = FailedOwner([_make_entry("old history " * 2000)])
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=manager)
    monkeypatch.setattr(runner, "_prepare_request_window", AsyncMock(return_value=object()))
    key = "test:quick-failure"
    for _ in range(4):
        await runner._maybe_preflight_compact(key, 1000)
        runner.clear_compaction_turn_state(key)
    assert len(manager.compact_with_result_calls) == 3
    assert runner._compaction_failures[key].count == 3
    assert runner._compaction_failures[key].failure_kind == failure_kind


def test_timeout_cooldown_recovers_by_time_success_or_effective_condition_change(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("opensquilla.engine.runtime.time.monotonic", lambda: clock[0])
    runner = TurnRunner(provider_selector=None)
    key = "test:recovery"
    scope = ("provider", "model", 16000, 4096, 600)
    runner._bind_compaction_failure_scope(key, scope)
    runner._record_compaction_failure(key, failure_kind="operation_timeout")
    assert runner._compaction_circuit_open(key)
    runner._bind_compaction_failure_scope(key, scope)
    assert runner._compaction_circuit_open(key)
    clock[0] += 301
    assert not runner._compaction_circuit_open(key)
    runner._record_compaction_failure(key, failure_kind="operation_timeout")
    runner._record_compaction_success(key)
    assert not runner._compaction_circuit_open(key)
    runner._record_compaction_failure(key, failure_kind="operation_timeout")
    runner._bind_compaction_failure_scope(key, ("provider", "model", 32000, 4096, 600))
    assert not runner._compaction_circuit_open(key)


def test_failed_explicit_probe_does_not_close_a_timeout_circuit(monkeypatch):
    monkeypatch.setattr("opensquilla.engine.runtime.time.monotonic", lambda: 1000.0)
    runner = TurnRunner(provider_selector=None)
    key = "test:manual-retry"
    runner._record_compaction_failure(key, failure_kind="operation_timeout")
    runner._record_compaction_failure(key, failure_kind="provider_error")
    assert runner._compaction_failures[key].count == 2
    assert runner._compaction_circuit_open(key)


@pytest.mark.parametrize("failure_kind", [
    "cancelled", "parent_deadline", "turn_deadline_exceeded", "usage_error", "storage_error",
    "commit_failed", "stale_source", "stale_preimage", "stale_context_state",
    "consumer_admission_stale",
])
def test_owner_and_durability_failures_are_not_auxiliary_service_failures(failure_kind):
    runner = TurnRunner(provider_selector=None)
    runner._record_compaction_failure("test:owner", failure_kind=failure_kind)
    assert "test:owner" not in runner._compaction_failures
    assert "test:owner" not in runner._turn_compaction_failed_sessions


@pytest.mark.parametrize("failure_kind", ["provider_error", "idle_timeout", "incomplete"])
def test_confirmed_overflow_gets_one_probe_for_quick_failures(failure_kind):
    runner = TurnRunner(provider_selector=None)
    key = "test:overflow"
    for _ in range(3):
        runner._record_compaction_failure(key, failure_kind=failure_kind)
    assert runner._compaction_circuit_open(key)
    assert not runner._compaction_circuit_open(key, provider_overflow=True)
    assert runner._compaction_circuit_open(key, provider_overflow=True)
    runner._record_compaction_failure(key, failure_kind=failure_kind)
    assert runner._compaction_circuit_open(key, provider_overflow=True)


def test_timeout_then_failed_manual_probe_cannot_unlock_overflow_retry():
    runner = TurnRunner(provider_selector=None)
    key = "test:spent-budget-overflow"
    runner._record_compaction_failure(key, failure_kind="operation_timeout")
    runner._record_compaction_failure(key, failure_kind="provider_error")
    assert runner._compaction_circuit_open(key, provider_overflow=True)


@pytest.mark.parametrize("failure_kind", [
    "provider_error", "idle_timeout", "incomplete", "operation_timeout",
])
async def test_agent_reports_classified_dispatched_failure_once(monkeypatch, failure_kind):
    from tests.test_engine.test_compaction_runtime_boundaries import make_agent

    agent = make_agent()
    runner = TurnRunner(provider_selector=None)
    key = "test:agent-classification"
    reports = []

    def report(success, *, failure_kind=""):
        reports.append((success, failure_kind))
        runner._record_compaction_failure(key, failure_kind=failure_kind)

    agent.config.compaction_outcome_reporter = report

    async def failed(request):
        request.config.on_summary_call_started()
        if failure_kind == "operation_timeout":
            raise CompactionTimeoutError("summarizing", 600)
        return CompactionResult(
            summary="", kept_entries=[], removed_count=0, chunks_processed=0,
            skip_reason="summary_failed", failure_kind=failure_kind,
        )

    monkeypatch.setattr("opensquilla.engine.agent.compact_context", failed)
    request = CompactionRequest(session_id=key, entries=[], context_window_tokens=16000)
    for _ in range(2):
        if failure_kind == "operation_timeout":
            with pytest.raises(CompactionTimeoutError):
                await agent._execute_compaction_request(request)
        else:
            await agent._execute_compaction_request(request)
    assert reports == [(False, failure_kind)]
    assert runner._compaction_circuit_open(key) is (failure_kind == "operation_timeout")


async def test_manual_timeout_cools_auto_but_explicit_manual_retry_can_succeed(monkeypatch):
    from opensquilla.application.session_maintenance import (
        CompactSession,
        SessionCompactionExecutionResult,
        SessionCompactionPhaseTimeoutError,
    )
    from tests.test_gateway.test_manual_compaction_current_controls import _manual

    ports, runtime, plan, runner, _, agent, current, raw = _manual(monkeypatch)
    key = raw.session_key
    attempts = []

    async def manual_attempt(*_):
        runtime.config.on_summary_call_started()
        attempts.append(True)
        if len(attempts) == 1:
            raise SessionCompactionPhaseTimeoutError("summarizing", definitively_uncommitted=True)
        return SessionCompactionExecutionResult(applied=True, summary_len=20)

    monkeypatch.setattr(ports, "_compact", manual_attempt)
    with pytest.raises(SessionCompactionPhaseTimeoutError):
        await ports.compact(CompactSession(key), plan)
    # The following ordinary turn rebinds the same effective envelope. Its
    # changing prompt, appended transcript and parent deadline are not a new
    # model or summary budget and must not erase the manual timeout.
    ordinary_scope = runner._compaction_failure_identity(
        provider=runtime.budget.provider, provider_config=current,
        chat_config=agent.build_compaction_request_context().chat_config,
        policy=runner._compaction_failure_policy(agent.config),
    )
    runner._bind_compaction_failure_scope(key, ordinary_scope)
    runner.clear_compaction_turn_state(key)
    assert runner._compaction_circuit_open(key)
    assert runner._compaction_failures[key].count == 1
    outcome = await ports.compact(CompactSession(key), plan)
    assert outcome.applied and len(attempts) == 2
    assert not runner._compaction_circuit_open(key)


@pytest.mark.parametrize("failure_kind", ["provider_error", "idle_timeout", "incomplete"])
async def test_manual_classified_failure_is_shared_with_auto(monkeypatch, failure_kind):
    from opensquilla.application.session_maintenance import (
        CompactSession,
        SessionCompactionExecutionResult,
    )
    from tests.test_gateway.test_manual_compaction_current_controls import _manual

    ports, runtime, plan, runner, _, _, _, raw = _manual(monkeypatch)

    async def failed(*_):
        runtime.config.on_summary_call_started()
        return SessionCompactionExecutionResult(
            applied=False, summary_len=0, skip_reason="summary_failed", failure_kind=failure_kind,
        )

    monkeypatch.setattr(ports, "_compact", failed)
    await ports.compact(CompactSession(raw.session_key), plan)
    state = runner._compaction_failures[raw.session_key]
    assert state.count == 1 and state.failure_kind == failure_kind
    assert not runner._compaction_circuit_open(raw.session_key)


@pytest.mark.parametrize("failure_kind", ["provider_error", "operation_timeout"])
async def test_agent_pressure_cools_while_confirmed_overflow_gets_only_quick_probe(
    monkeypatch, failure_kind,
):
    from opensquilla.engine.agent import CompactionOutcome
    from opensquilla.provider import Message
    from tests.test_engine.test_compaction_runtime_boundaries import make_agent

    agent = make_agent()
    runner = TurnRunner(provider_selector=None)
    key = "test:actual-overflow-probe"
    for _ in range(3):
        runner._record_compaction_failure(key, failure_kind=failure_kind)
    agent.config.compaction_circuit_open = (
        lambda **kwargs: runner._compaction_circuit_open(key, **kwargs)
    )

    def report(success, *, failure_kind=""):
        assert not success
        runner._record_compaction_failure(key, failure_kind=failure_kind)

    agent.config.compaction_outcome_reporter = report
    attempts = []

    async def failed(request):
        attempts.append(request)
        request.config.on_summary_call_started()
        return CompactionResult(
            summary="", kept_entries=request.entries, removed_count=0, chunks_processed=0,
            skip_reason="summary_failed", failure_kind="provider_error",
        )

    monkeypatch.setattr("opensquilla.engine.agent.compact_context", failed)
    messages = [
        Message(role="user", content="completed question"),
        Message(role="assistant", content="completed answer " * 5000),
        Message(role="user", content=agent._current_turn_message),
    ]
    before = [message.model_copy(deep=True) for message in messages]
    window = CompactionOutcome(messages=[messages[-1]], compacted=True, ephemeral_only=True)
    recovery = MagicMock(return_value=window)
    monkeypatch.setattr(agent, "_recover_local_request_window", recovery)
    for provider_overflow in (False, True, True):
        agent._compaction_failed_this_turn = False
        outcome = await agent._check_context_overflow(
            messages, estimated_context_tokens=20000, protected_turn_start_index=2,
            request_context_insert_index=2, runtime_context_insert_index=2,
            provider_overflow=provider_overflow,
        )
        assert outcome is window
    assert len(attempts) == (1 if failure_kind == "provider_error" else 0)
    assert recovery.call_count == 3
    assert messages == before


@pytest.mark.parametrize("outcome_kind", ["cancelled", "storage", "usage", "stale_preimage"])
async def test_manual_owner_failures_preserve_existing_auxiliary_failure_state(
    monkeypatch, outcome_kind,
):
    from opensquilla.application.session_maintenance import (
        CompactSession,
        SessionCompactionExecutionResult,
    )
    from opensquilla.engine.usage_accounting import UsageAccountingUnavailableError
    from tests.test_gateway.test_manual_compaction_current_controls import _manual

    ports, runtime, plan, runner, _, _, _, raw = _manual(monkeypatch)
    key = raw.session_key
    runner._bind_compaction_failure_scope(key, runtime.failure_scope)
    runner._record_compaction_failure(key, failure_kind="provider_error")
    existing = runner._compaction_failures[key]
    failures = {
        "cancelled": asyncio.CancelledError(),
        "storage": sqlite3.OperationalError("synthetic storage unavailable"),
        "usage": UsageAccountingUnavailableError("synthetic usage ledger unavailable"),
    }

    async def fail_after_dispatch(*_):
        runtime.config.on_summary_call_started()
        if outcome_kind in failures:
            raise failures[outcome_kind]
        return SessionCompactionExecutionResult(
            applied=False, summary_len=0, skip_reason="stale_preimage",
        )

    monkeypatch.setattr(ports, "_compact", fail_after_dispatch)
    if outcome_kind in failures:
        with pytest.raises(type(failures[outcome_kind])) as raised:
            await ports.compact(CompactSession(key), plan)
        assert raised.value is failures[outcome_kind]
    else:
        outcome = await ports.compact(CompactSession(key), plan)
        assert outcome.applied is False and outcome.skip_reason == "stale_preimage"
    assert runner._compaction_failures[key] is existing
    assert existing.count == 1 and existing.failure_kind == "provider_error"


@pytest.mark.parametrize("reason", ["no_compression_benefit", "no_progress"])
async def test_paid_unproductive_preflight_stays_skipped_but_cools_after_three_turns(
    monkeypatch, reason,
):
    class UnproductiveOwner(_ResultCompactionSessionManager):
        async def compact_with_result(self, session_key, window, config=None, **kwargs):
            self.compact_with_result_calls.append((session_key, window, config))
            config.on_summary_call_started()
            return CompactionResult(
                summary="unused candidate", kept_entries=[], removed_count=0,
                chunks_processed=1, skip_reason=reason,
            )

    # Cached usage can keep reporting pressure after the actual source has
    # become short. Do not buy another unproductive summary on every turn.
    entry = _make_entry("Small old fact with inflated cached usage.")
    entry.token_count = 20_000
    manager = UnproductiveOwner([entry])
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=manager)
    key = "test:unproductive-preflight"
    notifications = MagicMock()
    recovery = AsyncMock(return_value=object())
    monkeypatch.setattr("opensquilla.engine.runtime.notify_compaction", notifications)
    monkeypatch.setattr(runner, "_prepare_request_window", recovery)

    for expected_count in (1, 2, 3):
        assert await runner._maybe_preflight_compact(key, 1000) is None
        assert await runner._maybe_preflight_compact(key, 1000) is None
        assert len(manager.compact_with_result_calls) == expected_count
        assert runner._compaction_failures[key].count == expected_count
        assert runner._compaction_failures[key].failure_kind == "unproductive"
        assert key in runner._turn_compaction_failed_sessions
        runner.clear_compaction_turn_state(key)

    skipped = [call.kwargs for call in notifications.call_args_list
               if call.kwargs.get("reason") == reason]
    assert len(skipped) == 3 and all(event["status"] == "skipped" for event in skipped)
    assert not any(call.kwargs.get("status") == "failed"
                   for call in notifications.call_args_list)
    assert runner._compaction_circuit_open(key)
    await runner._maybe_preflight_compact(key, 1000)
    assert len(manager.compact_with_result_calls) == 3
    recovery.assert_awaited_once()
    assert recovery.await_args.kwargs["reason"] == "durable_compaction_circuit_open"
    assert manager._transcript == [entry]


@pytest.mark.parametrize("reason", ["no_compression_benefit", "no_progress", "within_budget"])
@pytest.mark.parametrize("entrypoint", ["preflight", "agent", "manual"])
async def test_undispatched_benign_skips_do_not_cool_or_erase_existing_state(
    monkeypatch, reason, entrypoint,
):
    key = "test:undispatched-benign"
    result = CompactionResult(
        summary="", kept_entries=[], removed_count=0, chunks_processed=0, skip_reason=reason,
    )
    runner = TurnRunner(provider_selector=None)
    if entrypoint == "preflight":
        class NoDispatchOwner(_ResultCompactionSessionManager):
            async def compact_with_result(self, *args, **kwargs):
                return result

        runner = TurnRunner(provider_selector=MagicMock(), session_manager=NoDispatchOwner([
            _make_entry("old history " * 2000),
        ]))
        async def run():
            return await runner._maybe_preflight_compact(key, 1000)
    elif entrypoint == "agent":
        from tests.test_engine.test_compaction_runtime_boundaries import make_agent

        agent = make_agent()
        agent.config.compaction_outcome_reporter = lambda success, **kwargs: (
            runner._record_compaction_failure(key, **kwargs)
        )
        monkeypatch.setattr(
            "opensquilla.engine.agent.compact_context", AsyncMock(return_value=result),
        )
        request = CompactionRequest(session_id=key, entries=[], context_window_tokens=16000)

        async def run():
            return await agent._execute_compaction_request(request)
    else:
        from opensquilla.application.session_maintenance import (
            CompactSession,
            SessionCompactionExecutionResult,
        )
        from tests.test_gateway.test_manual_compaction_current_controls import _manual

        ports, _, plan, runner, _, _, _, raw = _manual(monkeypatch)
        key = raw.session_key
        monkeypatch.setattr(ports, "_compact", AsyncMock(
            return_value=SessionCompactionExecutionResult(
                applied=False, summary_len=0, skip_reason=reason,
            ),
        ))

        async def run():
            return await ports.compact(CompactSession(key), plan)

    runner._record_compaction_failure(key, failure_kind="provider_error")
    runner.clear_compaction_turn_state(key)
    existing = runner._compaction_failures[key]
    await run()
    assert compaction_failure_status(reason) == "skipped"
    assert runner._compaction_failures[key] is existing
    assert existing.count == 1 and existing.failure_kind == "provider_error"
    assert key not in runner._turn_compaction_failed_sessions


@pytest.mark.parametrize("reason", ["no_compression_benefit", "no_progress"])
async def test_agent_unproductive_attempt_does_not_dispatch_again_in_same_turn(monkeypatch, reason):
    from opensquilla.engine.agent import CompactionOutcome
    from opensquilla.provider import Message
    from tests.test_engine.test_compaction_runtime_boundaries import make_agent

    agent = make_agent()
    runner = TurnRunner(provider_selector=None)
    key = "test:agent-unproductive"
    reports = []

    def report(success, *, failure_kind=""):
        reports.append((success, failure_kind))
        runner._record_compaction_failure(key, failure_kind=failure_kind)

    agent.config.compaction_outcome_reporter = report
    agent.config.compaction_circuit_open = (
        lambda **kwargs: runner._compaction_circuit_open(key, **kwargs)
    )
    attempts = []

    async def unproductive(request):
        attempts.append(request)
        request.config.on_summary_call_started()
        return CompactionResult(
            summary="candidate must not be replayed", kept_entries=request.entries,
            removed_count=0, chunks_processed=1, skip_reason=reason,
        )

    monkeypatch.setattr("opensquilla.engine.agent.compact_context", unproductive)
    messages = [
        Message(role="user", content="completed question"),
        Message(role="assistant", content="completed answer " * 5000),
        Message(role="user", content=agent._current_turn_message),
    ]
    before = [message.model_copy(deep=True) for message in messages]
    window = CompactionOutcome(messages=[messages[-1]], compacted=True, ephemeral_only=True)
    recovery = MagicMock(return_value=window)
    monkeypatch.setattr(agent, "_recover_local_request_window", recovery)
    for _ in range(2):
        assert await agent._check_context_overflow(
            messages, estimated_context_tokens=20000, protected_turn_start_index=2,
            request_context_insert_index=2, runtime_context_insert_index=2,
        ) is window
    assert len(attempts) == 1 and reports == [(False, "unproductive")]
    assert agent._compaction_failed_this_turn
    assert not runner._compaction_circuit_open(key)
    assert recovery.call_count == 2 and messages == before
    assert compaction_failure_status(reason) == "skipped"


@pytest.mark.parametrize("reason", ["no_compression_benefit", "no_progress"])
async def test_manual_unproductive_cooldown_allows_explicit_retry_and_clears_on_success(
    monkeypatch, reason,
):
    from opensquilla.application.session_maintenance import (
        CompactSession,
        SessionCompactionExecutionResult,
    )
    from tests.test_gateway.test_manual_compaction_current_controls import _manual

    ports, runtime, plan, runner, _, _, _, raw = _manual(monkeypatch)
    key = raw.session_key
    attempts = []

    async def compact(*_):
        attempts.append(True)
        runtime.config.on_summary_call_started()
        if len(attempts) <= 3:
            return SessionCompactionExecutionResult(
                applied=False, summary_len=0, skip_reason=reason,
            )
        return SessionCompactionExecutionResult(applied=True, summary_len=20)

    monkeypatch.setattr(ports, "_compact", compact)
    for expected_count in (1, 2, 3):
        outcome = await ports.compact(CompactSession(key), plan)
        assert not outcome.applied and outcome.skip_reason == reason
        assert compaction_failure_status(outcome.skip_reason) == "skipped"
        assert runner._compaction_failures[key].count == expected_count
        assert runner._compaction_failures[key].failure_kind == "unproductive"
        assert runner._compaction_circuit_open(key) is (expected_count == 3)
    outcome = await ports.compact(CompactSession(key), plan)
    assert outcome.applied and len(attempts) == 4
    assert not runner._compaction_circuit_open(key)


@pytest.mark.parametrize("changed_field", [0, 1, 2, 3, 4, 5])
def test_unproductive_cooldown_resets_only_when_effective_scope_changes(monkeypatch, changed_field):
    clock = [1000.0]
    monkeypatch.setattr("opensquilla.engine.runtime.time.monotonic", lambda: clock[0])
    runner = TurnRunner(provider_selector=None)
    key = "test:unproductive-scope"
    scope = ["provider", "model", 16000, 4096, 600, 120]
    runner._bind_compaction_failure_scope(key, tuple(scope))
    for _ in range(3):
        runner._record_compaction_failure(key, failure_kind="unproductive")
    runner.clear_compaction_turn_state(key)
    runner._bind_compaction_failure_scope(key, tuple(scope))
    assert runner._compaction_circuit_open(key)
    clock[0] += 299
    assert runner._compaction_circuit_open(key)
    scope[changed_field] = f"changed-{changed_field}"
    runner._bind_compaction_failure_scope(key, tuple(scope))
    assert not runner._compaction_circuit_open(key)


def test_unproductive_cooldown_expires_after_300_seconds(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("opensquilla.engine.runtime.time.monotonic", lambda: clock[0])
    runner = TurnRunner(provider_selector=None)
    for _ in range(3):
        runner._record_compaction_failure("test:unproductive-expiry", failure_kind="unproductive")
    clock[0] += 299
    assert runner._compaction_circuit_open("test:unproductive-expiry")
    clock[0] += 1
    assert not runner._compaction_circuit_open("test:unproductive-expiry")
