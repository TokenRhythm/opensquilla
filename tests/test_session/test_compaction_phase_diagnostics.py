"""Local summary work is timed without retaining source or exception prose."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
from dataclasses import replace
from types import SimpleNamespace

import pytest
from structlog.testing import capture_logs

from opensquilla.engine.usage_accounting import (
    UsageAccountingScope,
    UsageExecutionContext,
    account_provider_stream,
    bind_usage_accounting_scope,
)
from opensquilla.observability.log_privacy import PrivateLogFormatter, scrub_log_artifact
from opensquilla.provider.types import DoneEvent, TextDeltaEvent
from opensquilla.session import compaction
from opensquilla.session.compaction_diagnostics import compaction_diagnostic_phase
from opensquilla.session.compaction_lifecycle import CompactionTimeoutError
from opensquilla.session.tokenizer import estimate_tokens
from tests.helpers.compaction import synthetic_compaction_config

PRIVATE = "synthetic private source must remain absent from phase diagnostics"


def _request():
    return compaction.CompactionRequest(
        session_id="synthetic-phase-session",
        entries=[
            {"role": role, "content": f"{PRIVATE} round {index}"}
            for index in range(3)
            for role in ("user", "assistant")
        ],
        context_window_tokens=4000,
        forced_prefix_cut=4,
        trigger="message_count",
        consumer_admission=lambda *_: True,
        config=synthetic_compaction_config(operation_id="cmp_synthetic_phases"),
    )


def _bundle_record(event):
    record = logging.LogRecord("opensquilla.session.compaction", logging.INFO, "", 0, "", (), None)
    record._opensquilla_log_metadata = event
    formatted = PrivateLogFormatter().format(record)
    return json.loads(scrub_log_artifact(formatted))


async def test_summary_completion_precedes_local_phase_pairs_and_terminal():
    with capture_logs() as events:
        result = await compaction.compact_context(_request())
    assert result.removed_count == 4
    assert result.skip_reason is None
    model_done = next(i for i, row in enumerate(events)
                      if row["event"] == "compaction.llm_call_completed")
    assert events[model_done]["duration_ms"] >= 0
    terminal = next(i for i, row in enumerate(events)
                    if row["event"] == "compaction.operation_terminal")
    expected = [
        "summary_stream_cleanup", "attachment_projection", "coverage_extraction",
        "retained_source_measurement", "retained_replay_measurement", "summary_validation",
        "consumer_admission",
    ]
    phases = [(i, row) for i, row in enumerate(events)
              if row["event"] in {"compaction.phase_started", "compaction.phase_finished"}
              and row.get("step_name") not in {
                  "summary_attempt_cleanup", "summary_response_processing",
              }]
    assert [row["step_name"] for _, row in phases[::2]] == expected
    for index, step in enumerate(expected):
        (begin_index, begin), (end_index, end) = phases[index * 2:index * 2 + 2]
        assert model_done < begin_index < end_index < terminal
        assert begin["status"] == "started"
        assert end["step_name"] == step
        assert end["status"] == "completed"
        assert end["duration_ms"] >= 0
        for row in (begin, end):
            archived = _bundle_record(row)
            assert archived["step_name"] == step
            assert archived["compaction_id"] == "cmp_synthetic_phases"
            assert archived["pid"] == os.getpid()
            assert archived["phase"] in {"summarizing", "validating"}
            assert archived["status"] == row["status"]
            assert PRIVATE not in json.dumps(archived)


@pytest.mark.parametrize("cancel", [False, True])
async def test_actual_attempt_cleanup_is_visible_after_provider_done(monkeypatch, cancel):
    entered, release = asyncio.Event(), asyncio.Event()

    class Stream:
        def __init__(self):
            self.events = iter([
                TextDeltaEvent(text="Synthetic checkpoint."), DoneEvent(stop_reason="end_turn"),
            ])

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self.events)
            except StopIteration:
                raise StopAsyncIteration from None

        async def aclose(self):
            entered.set()
            await release.wait()

    class Provider:
        accounts_physical_usage = True

        def chat(self, *args, **kwargs):
            return Stream()

    cfg = synthetic_compaction_config()
    target = replace(cfg.llm_plan.primary, provider=Provider())
    plan = replace(cfg.llm_plan, candidates=(target,))
    monkeypatch.setattr(compaction, "_COMPACTION_STREAM_CLOSE_TIMEOUT_SECONDS", 5)
    with capture_logs() as events:
        task = asyncio.create_task(compaction.call_compaction_provider(
            "Synthetic source.", "", plan, compaction_id="cmp_stream_cleanup",
        ))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            assert any(row["event"] == "compaction.summary_response_received" for row in events)
            steps = [row for row in events if row.get("step_name") == "summary_attempt_cleanup"]
            assert len(steps) == 1 and steps[0]["status"] == "started"
            assert not any(row["event"] == "compaction.llm_call_completed" for row in events)
            if cancel:
                task.cancel()
                await asyncio.sleep(0)
            release.set()
            if cancel:
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                assert await task == "Synthetic checkpoint."
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
    finishes = [row for row in events if row.get("step_name") == "summary_attempt_cleanup"
                and row["event"] == "compaction.phase_finished"]
    assert len(finishes) == 1
    assert finishes[0]["status"] == ("cancelled" if cancel else "completed")
    assert finishes[0]["duration_ms"] >= 0


@pytest.mark.parametrize("stage", ["finalization", "iterator_exhaustion", "transport_cleanup"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_raw_completion_phase_covers_real_accounting_and_transport_cleanup(stage, cancel):
    entered, release = asyncio.Event(), asyncio.Event()
    finalized = []
    closed = []
    unknown = []

    class Sink:
        async def start(self, call):
            pass

        async def finalize(self, call, result):
            if stage == "finalization":
                entered.set()
                await release.wait()
            finalized.append(result)

        async def mark_unknown(self, call, reason):
            unknown.append(reason)

    class Stream:
        def __init__(self):
            self.events = iter([
                TextDeltaEvent(text="Synthetic checkpoint."), DoneEvent(stop_reason="end_turn"),
            ])

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self.events)
            except StopIteration:
                if stage == "iterator_exhaustion":
                    entered.set()
                    await release.wait()
                raise StopAsyncIteration from None

        async def aclose(self):
            if stage == "transport_cleanup":
                entered.set()
                await release.wait()
            closed.append(True)

    class Provider:
        def chat(self, *args, **kwargs):
            return Stream()

    cfg = synthetic_compaction_config()
    target = replace(cfg.llm_plan.primary, provider=Provider())
    plan = replace(cfg.llm_plan, candidates=(target,))
    scope = UsageAccountingScope(Sink(), UsageExecutionContext(
        execution_id="synthetic-response-timing", agent_run_id="synthetic-run",
    ))
    with capture_logs() as events, bind_usage_accounting_scope(scope):
        task = asyncio.create_task(compaction.call_compaction_provider(
            "Synthetic source.", "", plan, compaction_id="cmp_accounted_response",
        ))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            received = [row for row in events
                        if row["event"] == "compaction.summary_response_received"]
            phases = [row for row in events
                      if row.get("step_name") == "summary_response_processing"]
            assert len(received) == 1
            assert len(phases) == 1 and phases[0]["status"] == "started"
            assert not any(row["event"] == "compaction.llm_call_completed" for row in events)
            assert not closed
            if stage == "finalization":
                assert not finalized
            if cancel:
                task.cancel()
                await asyncio.sleep(0)
                if stage == "finalization":
                    # The existing ledger cancellation barrier must still settle.
                    assert not task.done() and not finalized
                    assert not any(row["event"] == "compaction.phase_finished"
                                   and row.get("step_name") == "summary_response_processing"
                                   for row in events)
            release.set()
            if cancel:
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                assert await task == "Synthetic checkpoint."
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
    finishes = [row for row in events if row.get("step_name") == "summary_response_processing"
                and row["event"] == "compaction.phase_finished"]
    assert len(finishes) == 1
    assert finishes[0]["status"] == ("cancelled" if cancel else "completed")
    if not cancel:
        finished_index = events.index(finishes[0])
        completed_index = next(index for index, row in enumerate(events)
                               if row["event"] == "compaction.llm_call_completed")
        assert finished_index < completed_index
    assert finalized and closed and not unknown
    assert PRIVATE not in json.dumps(events)


@pytest.mark.parametrize("scoped", [False, True])
async def test_raw_event_observer_failure_preserves_event_identity_and_accounting(scoped):
    source = [TextDeltaEvent(text="Synthetic checkpoint."), DoneEvent(stop_reason="end_turn")]
    observed, finalized = [], []

    async def stream():
        for event in source:
            yield event

    def broken_observer(event):
        observed.append(event)
        raise RuntimeError(PRIVATE)

    class Sink:
        async def start(self, call):
            pass

        async def finalize(self, call, result):
            finalized.append(result)

        async def mark_unknown(self, call, reason):
            pytest.fail("A completed stream must retain its terminal usage receipt")

    scope = UsageAccountingScope(Sink(), UsageExecutionContext(
        execution_id="synthetic-observer", agent_run_id="synthetic-observer-run",
    )) if scoped else None
    with bind_usage_accounting_scope(scope):
        delivered = [event async for event in account_provider_stream(
            stream, provider="synthetic", model="synthetic/model",
            on_provider_event=broken_observer,
        )]
    assert len(delivered) == len(observed) == len(source)
    assert all(actual is expected for actual, expected in zip(delivered, source, strict=True))
    assert all(actual is expected for actual, expected in zip(observed, source, strict=True))
    assert len(finalized) == int(scoped)


async def test_admission_replan_records_source_and_attachment_work(monkeypatch):
    entries = [
        {"role": "user" if index % 2 == 0 else "assistant",
         "content": f"Source round {index}. " + "Completed prior discussion. " * 100}
        for index in range(12)
    ]
    monkeypatch.setattr(compaction, "_find_turn_boundary_cut", lambda *args, **kwargs: 2)
    cfg = synthetic_compaction_config(
        operation_id="cmp_replan", attachment_path_resolver=lambda *args, **kwargs: None,
    )
    with capture_logs() as events:
        result = await compaction.compact_context(compaction.CompactionRequest(
            session_id="synthetic-replan", entries=entries, config=cfg,
            context_window_tokens=sum(estimate_tokens(entry["content"]) for entry in entries),
            consumer_admission=lambda _, kept: len(kept) <= 3,
        ))
    assert result.quality_report["replan_count"] > 0
    assert result.removed_count > 2
    expected = {
        "replan_source_boundaries", "replan_source_selection", "replan_attachment_projection",
        "replan_tail_measurement",
    }
    finishes = [row for row in events if row["event"] == "compaction.phase_finished"
                and row.get("step_name") in expected]
    assert {row["step_name"] for row in finishes} == expected
    assert all(row["status"] == "completed" and row["duration_ms"] >= 0 for row in finishes)
    assert all(row["compaction_id"] == "cmp_replan" for row in finishes)


async def test_coverage_failure_has_final_timing_without_exception_message(monkeypatch):
    def fail(*_args, **_kwargs):
        raise ValueError(PRIVATE)

    monkeypatch.setattr(compaction, "build_coverage_obligations", fail)
    with capture_logs() as events, pytest.raises(ValueError, match=PRIVATE):
        await compaction.compact_context(_request())
    phases = [row for row in events if row.get("step_name") == "coverage_extraction"]
    assert len(phases) == 2
    assert phases[-1]["status"] == "failed"
    assert phases[-1]["error_type"] == "ValueError"
    assert phases[-1]["duration_ms"] >= 0
    assert PRIVATE not in json.dumps(phases)
    assert _bundle_record(phases[-1])["error_type"] == "ValueError"
    assert not any(row.get("step_name") == "summary_validation" for row in events)


async def test_cancelled_local_phase_is_not_reported_completed():
    entered = asyncio.Event()

    async def work():
        entered.set()
        await asyncio.Event().wait()

    with capture_logs() as events:
        task = asyncio.create_task(compaction.await_compaction_phase(
            work(), synthetic_compaction_config(operation_id="cmp_cancelled"),
            phase="validating", diagnostic_step="coverage_extraction",
        ))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert events[-1]["status"] == "cancelled"
    assert events[-1]["error_type"] == "CancelledError"
    assert events[-1]["compaction_id"] == "cmp_cancelled"


@pytest.mark.parametrize("remaining", [0.0, 0.01])
async def test_expired_local_phase_is_timed_and_preserves_timeout_classification(
    monkeypatch, remaining,
):
    monkeypatch.setattr(compaction, "compaction_remaining_seconds", lambda _: remaining)
    pending = asyncio.Event().wait()
    with capture_logs() as events, pytest.raises(CompactionTimeoutError) as caught:
        await compaction.await_compaction_phase(
            pending, synthetic_compaction_config(operation_id="cmp_timeout"),
            phase="validating", diagnostic_step="coverage_extraction",
        )
    assert caught.value.phase == "validating"
    assert events[-1]["status"] == "timed_out"
    assert events[-1]["error_type"] == "CompactionTimeoutError"
    assert inspect.getcoroutinestate(pending) == inspect.CORO_CLOSED


def test_phase_duration_uses_monotonic_clock_and_never_renders_exception(monkeypatch):
    from opensquilla.session import compaction_diagnostics

    ticks = iter((100.0, 100.125))
    monkeypatch.setattr(compaction_diagnostics.time, "monotonic", lambda: next(ticks))
    with capture_logs() as events, pytest.raises(RuntimeError):
        with compaction_diagnostic_phase(
            compaction_id="cmp_clock", phase="validating", step="summary_validation",
        ):
            raise RuntimeError(PRIVATE)
    assert events[-1]["duration_ms"] == 125
    assert events[-1]["status"] == "failed"
    assert PRIVATE not in json.dumps(events)


@pytest.mark.parametrize("fail_work", [False, True])
def test_diagnostic_sink_failure_preserves_work_and_original_exception(monkeypatch, fail_work):
    from opensquilla.session import compaction_diagnostics

    def failed_sink(*args, **kwargs):
        raise OSError("Synthetic logging failure")

    monkeypatch.setattr(compaction_diagnostics, "log", SimpleNamespace(info=failed_sink))
    executed = False
    original = RuntimeError(PRIVATE)
    try:
        with compaction_diagnostic_phase(
            compaction_id="cmp_sink", phase="validating", step="summary_validation",
        ):
            executed = True
            if fail_work:
                raise original
    except RuntimeError as caught:
        assert caught is original and fail_work
    else:
        assert not fail_work
    assert executed
