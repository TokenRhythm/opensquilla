"""Durable compaction diagnostics follow the settled writer without exposing history."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from types import SimpleNamespace

import pytest
import pytest_asyncio
from structlog.testing import capture_logs

from opensquilla.observability.log_privacy import PrivateLogFormatter, scrub_log_artifact
from opensquilla.session import compaction_diagnostics
from opensquilla.session.compaction import CompactionConfig, CompactionResult
from opensquilla.session.compaction_state import StructuredCompactionSummary
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage, StorageBusyError

_COMPACTION_ID = "cmp_synthetic_commit_diagnostics"
_PRIVATE_HISTORY = "Synthetic private history must not enter operational diagnostics."


@pytest_asyncio.fixture
async def candidate(monkeypatch):
    storage = await SessionStorage.open(":memory:")
    manager = SessionManager(storage, inject_time_prefix=False)
    node = await manager.create("agent:main:synthetic-commit-diagnostics")
    for role in ("user", "assistant", "user", "assistant"):
        await manager.append_message(node.session_key, role, _PRIVATE_HISTORY)
    raw = await manager.get_transcript(node.session_key)
    source = await manager.capture_compaction_source(node.session_key)
    state = SimpleNamespace(
        storage=storage, manager=manager, node=node, raw=raw, source=source,
        generated=False, invalid=False,
    )

    async def summarize(request):
        state.generated = True
        return CompactionResult(
            summary="Synthetic checkpoint.",
            summary_payload=StructuredCompactionSummary(
                current_status="Synthetic checkpoint.",
            ).model_dump(mode="json"),
            summary_format="structured_v1", kept_entries=request.entries[2:],
            kept_start_index=0 if state.invalid else 2, removed_count=2,
            chunks_processed=1,
        )

    monkeypatch.setattr("opensquilla.session.manager.compact_context", summarize)
    try:
        yield state
    finally:
        await storage.close()


async def _invoke(candidate, writer):
    node = candidate.node
    kwargs = {
        "compaction_id": _COMPACTION_ID,
        "expected_session_id": node.session_id,
        "expected_session_epoch": node.epoch,
    }
    if writer == "manual":
        return await candidate.manager.compact_with_result(
            node.session_key, 32768, CompactionConfig(), trigger_reason="manual", **kwargs,
        )
    candidate.generated = True
    return await candidate.manager.persist_compaction_result(
        node.session_key, "Synthetic checkpoint.",
        [entry.model_dump() for entry in candidate.raw[2:]],
        summary_payload=StructuredCompactionSummary(
            current_status="Synthetic checkpoint.",
        ).model_dump(mode="json"),
        summary_format="structured_v1", removed_count=2,
        source_snapshot=candidate.source, **kwargs,
    )


def _events(logs, event):
    return [
        record for record in logs
        if record.get("event") == event and record.get("compaction_id") == _COMPACTION_ID
    ]


@pytest.mark.parametrize("writer", ["manual", "inline"])
async def test_durable_phase_order_and_correlation_survive_private_log_projection(
    candidate, writer,
):
    with capture_logs() as logs:
        result = await _invoke(candidate, writer)
    assert result.removed_count == 2 if writer == "manual" else result.applied
    starts = _events(logs, "compaction.phase_started")
    finishes = _events(logs, "compaction.phase_finished")
    expected = (
        ["precommit_validation", "precommit_consumer_admission", "precommit_session_read",
         "precommit_transcript_read", "precommit_context_read"]
        if writer == "manual" else
        ["precommit_session_read", "precommit_source_validation", "precommit_context_read",
         "precommit_validation", "precommit_replay_validation"]
    ) + ["precommit_canonical_read", "precommit_attachment_manifest", "commit_barrier"]
    assert [record["step_name"] for record in starts] == expected
    assert [record["step_name"] for record in finishes] == expected
    assert all(record["status"] == "completed" for record in finishes)
    assert all(record["duration_ms"] >= 0 for record in finishes)
    assert all(record["pid"] == os.getpid() for record in [*starts, *finishes])
    assert _events(logs, "session_compaction.commit_started")
    assert _events(logs, "session_compaction.commit_completed")
    assert len(await candidate.manager.get_summaries(candidate.node.session_key)) == 1

    formatter = PrivateLogFormatter()
    rendered = []
    for fields in [*starts, *finishes, *_events(logs, "session_compaction.commit_completed")]:
        record = logging.LogRecord("synthetic", logging.INFO, "", 0, "", (), None)
        record._opensquilla_log_metadata = fields
        rendered.append(formatter.format(record))
    safe = scrub_log_artifact("\n".join(rendered))
    assert _PRIVATE_HISTORY not in safe
    assert "Synthetic checkpoint." not in safe
    for step in expected:
        assert f'"step_name": "{step}"' in safe
    assert f'"pid": {os.getpid()}' in safe
    assert '"cancellation_reconciled": false' in safe


async def test_invalid_candidate_is_rejected_before_the_writer(candidate):
    candidate.invalid = True
    with capture_logs() as logs:
        result = await _invoke(candidate, "manual")
    assert result.skip_reason == "invalid_source_boundary"
    assert _events(logs, "session_compaction.precommit_rejected")[0]["reason_code"] == (
        "invalid_source_boundary"
    )
    assert not _events(logs, "session_compaction.commit_started")
    assert await candidate.manager.get_transcript(candidate.node.session_key) == candidate.raw


@pytest.mark.parametrize("writer", ["manual", "inline"])
@pytest.mark.parametrize("method,step", [
    ("get_all_summaries", "precommit_context_read"),
    ("get_canonical_transcript", "precommit_canonical_read"),
])
async def test_precommit_read_failure_keeps_its_phase_and_source(
    candidate, monkeypatch, writer, method, step,
):
    original = getattr(candidate.storage, method)

    async def fail_after_summary(*args, **kwargs):
        if candidate.generated:
            raise sqlite3.OperationalError("database is locked")
        return await original(*args, **kwargs)

    monkeypatch.setattr(candidate.storage, method, fail_after_summary)
    with capture_logs() as logs:
        result = await _invoke(candidate, writer)
    assert result.skip_reason == "precommit_read_failed" if writer == "manual" else (
        result.reason == "precommit_read_failed" and not result.applied
    )
    failed = [
        record for record in _events(logs, "compaction.phase_finished")
        if record["step_name"] == step
    ]
    assert len(failed) == 1
    assert failed[0]["status"] == "failed"
    assert failed[0]["error_type"] == "OperationalError"
    assert not _events(logs, "session_compaction.commit_started")
    assert await candidate.manager.get_transcript(candidate.node.session_key) == candidate.raw


@pytest.mark.parametrize("writer", ["manual", "inline"])
async def test_manifest_failure_is_content_free_and_precedes_commit(candidate, monkeypatch, writer):
    def fail_manifest(**kwargs):
        raise RuntimeError(_PRIVATE_HISTORY)

    monkeypatch.setattr(
        "opensquilla.session.manager._merge_attachment_manifest_state", fail_manifest,
    )
    with capture_logs() as logs, pytest.raises(RuntimeError, match="Synthetic private"):
        await _invoke(candidate, writer)
    assert _PRIVATE_HISTORY not in json.dumps(logs)
    failed = _events(logs, "compaction.phase_finished")[-1]
    assert failed["step_name"] == "precommit_attachment_manifest"
    assert failed["status"] == "failed" and failed["error_type"] == "RuntimeError"
    assert not _events(logs, "session_compaction.commit_started")


@pytest.mark.parametrize("writer", ["manual", "inline"])
@pytest.mark.parametrize("outcome", ["success", "stale", "failure"])
async def test_commit_diagnostics_wait_for_cancellation_reconciliation(
    candidate, monkeypatch, writer, outcome,
):
    entered, release = asyncio.Event(), asyncio.Event()
    original = candidate.storage.rewrite_compacted_session

    async def blocked_commit(**kwargs):
        entered.set()
        await release.wait()
        if outcome == "failure":
            raise sqlite3.OperationalError(_PRIVATE_HISTORY)
        if outcome == "stale":
            return False
        return await original(**kwargs)

    monkeypatch.setattr(candidate.storage, "rewrite_compacted_session", blocked_commit)
    with capture_logs() as logs:
        task = asyncio.create_task(_invoke(candidate, writer))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            assert not _events(logs, "session_compaction.commit_completed")
            release.set()
            if outcome == "failure":
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                result = await task
                assert result.cancellation_reconciled
                if outcome == "stale":
                    assert result.skip_reason == "stale_preimage" if writer == "manual" else (
                        not result.applied and result.reason == "stale_preimage"
                    )
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
    assert _PRIVATE_HISTORY not in json.dumps(logs)
    finished = _events(logs, "compaction.phase_finished")[-1]
    assert finished["step_name"] == "commit_barrier"
    if outcome == "failure":
        assert finished["status"] == "cancelled"
        assert _events(logs, "session_compaction.commit_failed")[0]["status"] == "cancelled"
    else:
        event = "commit_completed" if outcome == "success" else "commit_rejected"
        assert _events(logs, f"session_compaction.{event}")[0]["cancellation_reconciled"]
    assert len(await candidate.manager.get_summaries(candidate.node.session_key)) == (
        1 if outcome == "success" else 0
    )


@pytest.mark.parametrize("writer", ["manual", "inline"])
@pytest.mark.parametrize("stage", ["begin", "after_commit"])
async def test_only_pretransaction_busy_is_logged_as_rejection(
    candidate, monkeypatch, writer, stage,
):
    async def busy(**kwargs):
        raise StorageBusyError("rewrite_compacted_session", waited_ms=1,
                               retry_after_ms=1, stage=stage)

    monkeypatch.setattr(candidate.storage, "rewrite_compacted_session", busy)
    with capture_logs() as logs:
        if stage == "begin":
            result = await _invoke(candidate, writer)
            assert result.skip_reason == "precommit_write_busy" if writer == "manual" else (
                not result.applied and result.reason == "precommit_write_busy"
            )
        else:
            with pytest.raises(StorageBusyError):
                await _invoke(candidate, writer)
    event = "commit_rejected" if stage == "begin" else "commit_failed"
    terminal = _events(logs, f"session_compaction.{event}")
    assert len(terminal) == 1
    assert terminal[0]["duration_ms"] >= 0
    assert not _events(logs, "session_compaction.commit_completed")


@pytest.mark.parametrize("writer", ["manual", "inline"])
@pytest.mark.parametrize("outcome", ["success", "stale", "failure", "busy"])
async def test_diagnostic_sink_failure_preserves_the_actual_commit_result(
    candidate, monkeypatch, writer, outcome,
):
    attempted = []

    def unavailable_sink(event, **fields):
        attempted.append(event)
        raise OSError("Synthetic diagnostic sink unavailable")

    monkeypatch.setattr(compaction_diagnostics, "log", SimpleNamespace(info=unavailable_sink))
    original_rewrite = candidate.storage.rewrite_compacted_session
    failure = RuntimeError("Synthetic writer failure")

    async def rewrite(**kwargs):
        if outcome == "failure":
            raise failure
        if outcome == "stale":
            return False
        if outcome == "busy":
            raise StorageBusyError("rewrite_compacted_session", waited_ms=1,
                                   retry_after_ms=1, stage="begin")
        return await original_rewrite(**kwargs)

    monkeypatch.setattr(candidate.storage, "rewrite_compacted_session", rewrite)
    if outcome == "failure":
        with pytest.raises(RuntimeError) as caught:
            await _invoke(candidate, writer)
        assert caught.value is failure
    else:
        result = await _invoke(candidate, writer)
        if outcome == "success":
            assert result.removed_count == 2 if writer == "manual" else result.applied
        else:
            expected = "stale_preimage" if outcome == "stale" else "precommit_write_busy"
            assert result.skip_reason == expected if writer == "manual" else (
                not result.applied and result.reason == expected
            )
    assert "compaction.phase_started" in attempted
    assert "compaction.phase_finished" in attempted
    if outcome == "success":
        terminal = "commit_completed" if writer == "inline" else None
    else:
        terminal = "commit_failed" if outcome == "failure" else "commit_rejected"
    if terminal is not None:
        assert f"session_compaction.{terminal}" in attempted
    assert len(await candidate.manager.get_summaries(candidate.node.session_key)) == (
        1 if outcome == "success" else 0
    )
    if outcome != "success":
        assert await candidate.manager.get_transcript(candidate.node.session_key) == candidate.raw
