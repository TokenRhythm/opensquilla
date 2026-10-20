from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from opensquilla.engine.usage_accounting import (
    UsageAccountingScope,
    UsageExecutionContext,
    bind_usage_accounting_scope,
    current_usage_accounting_scope,
)
from opensquilla.provider import media_jobs as jobs


def _scope():
    return UsageAccountingScope(
        sink=SimpleNamespace(_storage=SimpleNamespace(query_usage_settlements=AsyncMock())),
        context=UsageExecutionContext(execution_id="derivation-test", agent_run_id="test"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["submit", "observe", "settlement"])
async def test_derivation_yields_and_cancellation_precedes_paid_dispatch(monkeypatch, operation):
    scope = _scope()
    loop = asyncio.get_running_loop()
    main_thread = threading.get_ident()
    started = asyncio.Event()
    release = threading.Event()
    finished = asyncio.Event()
    observed = []

    def slow_fingerprint(_):
        observed.append((threading.get_ident(), current_usage_accounting_scope()))
        loop.call_soon_threadsafe(started.set)
        try:
            assert release.wait(10), "test must release the bounded worker"
            return "fingerprint"
        finally:
            loop.call_soon_threadsafe(finished.set)

    monkeypatch.setattr(jobs, "credential_fingerprint", slow_fingerprint)
    reserve = AsyncMock()
    monkeypatch.setattr(jobs, "reserve_direct_usage_call", reserve)
    submit = AsyncMock()
    wrapped = jobs.accounted_video_submission("fixture")(submit)
    with bind_usage_accounting_scope(scope):
        if operation == "submit":
            coroutine = wrapped(api_root="https://fixture.invalid", model="video", api_key="fake")
        elif operation == "observe":
            coroutine = jobs.observe_video_job("fixture", "url", "job", {}, api_key="fake")
        else:
            coroutine = jobs.video_job_needs_settlement("fixture", "url", "job", api_key="fake")
        task = asyncio.create_task(coroutine)
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            assert observed == [(observed[0][0], scope)]
            assert observed[0][0] != main_thread
            assert not task.done()
            reserve.assert_not_awaited()
            submit.assert_not_awaited()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
            await asyncio.wait_for(finished.wait(), timeout=5)
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    reserve.assert_not_awaited()
    submit.assert_not_awaited()


@pytest.mark.asyncio
async def test_submission_derives_once_and_commits_before_original_callback(monkeypatch):
    scope = _scope()
    calls = []

    def fingerprint(_):
        calls.append(threading.get_ident())
        return "derived"

    monkeypatch.setattr(jobs, "credential_fingerprint", fingerprint)
    call = scope.new_call(provider="fixture", model="video")
    reservation = SimpleNamespace(
        call=call, finalize_measurement=AsyncMock(), mark_unknown=AsyncMock(),
    )
    monkeypatch.setattr(jobs, "reserve_direct_usage_call", AsyncMock(return_value=reservation))
    key = jobs._key_from_fingerprint("fixture", "https://fixture.invalid/", "job", "derived")
    callback_records = []

    def callback(job_id):
        record = jobs.job_record("usage", key)
        assert record["job_id"] == job_id
        assert record["finalized"] is False
        callback_records.append(record)

    @jobs.accounted_video_submission("fixture")
    async def submit(**kwargs):
        kwargs["on_job_accepted"]("job")
        assert callback_records
        return "job"

    with bind_usage_accounting_scope(scope):
        assert await submit(
            api_root="https://fixture.invalid/", model="video", on_job_accepted=callback,
        ) == "job"
        assert jobs.job_record("usage", key)["finalized"] is True
    assert len(calls) == 1
    assert calls[0] != threading.get_ident()
    reservation.finalize_measurement.assert_awaited_once()
    reservation.mark_unknown.assert_not_awaited()


def test_precomputed_key_preserves_existing_encoding_and_salt():
    scope = _scope()
    with bind_usage_accounting_scope(scope):
        fingerprint = jobs.credential_fingerprint("synthetic-only")
        expected = hashlib.sha256(json.dumps([
            "fixture", "https://fixture.invalid", fingerprint, "job",
        ]).encode()).hexdigest()
        assert jobs._key("fixture", "https://fixture.invalid/", "job", "synthetic-only") == expected
        assert jobs._key_from_fingerprint(
            "fixture", "https://fixture.invalid/", "job", fingerprint,
        ) == expected


@pytest.mark.asyncio
async def test_no_accounting_scope_does_not_derive_or_reserve(monkeypatch):
    derive = AsyncMock()
    reserve = AsyncMock()
    monkeypatch.setattr(jobs, "credential_fingerprint", derive)
    monkeypatch.setattr(jobs, "reserve_direct_usage_call", reserve)
    submit = AsyncMock(return_value="job")
    with bind_usage_accounting_scope(None):
        assert await jobs.accounted_video_submission("fixture")(submit)(model="video") == "job"
    derive.assert_not_called()
    reserve.assert_not_called()
    submit.assert_awaited_once_with(model="video")


@pytest.mark.asyncio
async def test_concurrent_first_derivations_share_the_winning_volatile_salt(monkeypatch):
    scope = _scope()
    jobs._volatile.pop((id(scope.sink), "settings", "credential_salt"), None)
    serialize = json.dumps
    candidates = threading.Barrier(2)

    def overlapping_candidates(value, *args, **kwargs):
        encoded = serialize(value, *args, **kwargs)
        if isinstance(value, dict) and set(value) == {"salt"}:
            candidates.wait(timeout=5)
        return encoded

    monkeypatch.setattr(jobs.json, "dumps", overlapping_candidates)
    with bind_usage_accounting_scope(scope):
        first, second = await asyncio.gather(
            asyncio.to_thread(jobs.credential_fingerprint, "synthetic-only"),
            asyncio.to_thread(jobs.credential_fingerprint, "synthetic-only"),
        )
        assert first == second
        assert await asyncio.to_thread(jobs.credential_fingerprint, "synthetic-only") == first
