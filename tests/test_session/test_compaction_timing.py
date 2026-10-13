"""Compaction progress/operation budgets use a virtual clock, never live APIs."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from opensquilla.compaction_timing import (
    CompactionIdleTimeoutError,
    CompactionOperationTimeoutError,
    compaction_progress_timeout,
    resolve_compaction_idle_timeout,
    resolve_compaction_total_timeout,
)


@pytest.mark.parametrize("value", [None, 0, -1, True, "invalid", float("nan"), float("inf")])
def test_invalid_operation_budget_uses_one_bounded_default(value):
    assert resolve_compaction_total_timeout(value) == 600.0


@pytest.mark.parametrize("value", [90.0, 120.0, 900.0, "17.5"])
def test_explicit_old_operation_budget_is_preserved(value):
    assert resolve_compaction_total_timeout(value) == float(value)


def test_idle_inherits_normal_request_policy_unless_explicitly_overridden():
    assert resolve_compaction_idle_timeout(240.0) == 240.0
    assert resolve_compaction_idle_timeout(240.0, 90.0) == 90.0
    assert resolve_compaction_idle_timeout(240.0, float("nan")) == 240.0
    assert resolve_compaction_idle_timeout(None) == 120.0


def test_manual_and_automatic_defaults_and_legacy_config_agree(monkeypatch):
    from opensquilla.application.session_maintenance import SessionCompactionTiming
    from opensquilla.engine.types import AgentConfig
    from opensquilla.gateway.adapters.session_maintenance import GatewaySessionMaintenancePorts
    from opensquilla.gateway.config import CompactionLlmConfig

    monkeypatch.delenv("OPENSQUILLA_COMPACTION_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("OPENSQUILLA_COMPACTION_TOTAL_TIMEOUT_SECONDS", raising=False)
    defaults = CompactionLlmConfig()
    assert defaults.timeout_seconds is None
    assert defaults.total_timeout_seconds == AgentConfig().compaction_total_timeout_seconds == 600.0
    assert SessionCompactionTiming().total_timeout_seconds == 600.0
    invalid = AgentConfig(compaction_total_timeout_seconds=float("nan"))
    assert invalid.compaction_total_timeout_seconds == 600.0
    for total in (None, 120.0, 900.0):
        settings = CompactionLlmConfig(**({"total_timeout_seconds": total} if total else {}))
        ports = GatewaySessionMaintenancePorts(SimpleNamespace(
            config=SimpleNamespace(compaction=settings), session_manager=None,
        ))
        assert ports.timing().total_timeout_seconds == (total or 600.0)
    explicit = CompactionLlmConfig(timeout_seconds=90.0, total_timeout_seconds=120.0)
    restored = CompactionLlmConfig.model_validate(explicit.model_dump())
    assert restored.timeout_seconds == 90.0
    assert restored.total_timeout_seconds == 120.0


async def _tick() -> None:
    # Let runnable tasks and then due timer callbacks settle without sleeping.
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_semantic_progress_outlives_old_wall_timeout_without_extending_total(monkeypatch):
    loop = asyncio.get_running_loop()
    now = loop.time()
    deadline = now + 600.0
    monkeypatch.setattr(loop, "time", lambda: now)
    events = asyncio.Queue()
    entered = asyncio.Event()

    async def stream():
        async with compaction_progress_timeout(
            idle_timeout_seconds=120.0, deadline_at_monotonic=deadline,
        ) as progress:
            entered.set()
            while True:
                event = await events.get()
                if event is None:
                    return "complete"
                progress.observe(event)

    task = asyncio.create_task(stream())
    try:
        await entered.wait()
        for kind in ("reasoning_delta", "text_delta", "reasoning_delta", "text_delta"):
            now += 80.0
            events.put_nowait(SimpleNamespace(kind=kind, text="real progress"))
            await _tick()
            assert not task.done()
        events.put_nowait(None)
        assert await task == "complete"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("event", [
    SimpleNamespace(kind="provider_heartbeat", text="ping"),
    SimpleNamespace(kind="provider_activity", text="receiving"),
    SimpleNamespace(kind="text_delta", text=""),
    SimpleNamespace(kind="reasoning_delta", text=" \n\t"),
    SimpleNamespace(kind="usage", text="metered"),
])
async def test_heartbeat_and_empty_chunks_do_not_renew_idle(monkeypatch, event):
    loop = asyncio.get_running_loop()
    now = loop.time()
    monkeypatch.setattr(loop, "time", lambda: now)
    events = asyncio.Queue()
    entered = asyncio.Event()

    async def stream():
        async with compaction_progress_timeout(idle_timeout_seconds=120.0) as progress:
            entered.set()
            while True:
                progress.observe(await events.get())

    task = asyncio.create_task(stream())
    try:
        await entered.wait()
        now += 80.0
        events.put_nowait(event)
        await _tick()
        assert not task.done()
        now += 41.0
        await _tick()
        with pytest.raises(CompactionIdleTimeoutError):
            await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_progress_never_renews_the_absolute_parent_or_operation_deadline(monkeypatch):
    loop = asyncio.get_running_loop()
    now = loop.time()
    deadline = now + 200.0
    monkeypatch.setattr(loop, "time", lambda: now)
    events = asyncio.Queue()
    entered = asyncio.Event()

    async def stream():
        async with compaction_progress_timeout(
            idle_timeout_seconds=120.0, deadline_at_monotonic=deadline,
        ) as progress:
            entered.set()
            while True:
                progress.observe(await events.get())

    task = asyncio.create_task(stream())
    try:
        await entered.wait()
        for _ in range(3):
            now += 50.0
            events.put_nowait(SimpleNamespace(kind="reasoning_delta", text="working"))
            await _tick()
            assert not task.done()
        now += 51.0
        await _tick()
        with pytest.raises(CompactionOperationTimeoutError):
            await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_synchronous_late_progress_cannot_revive_expired_idle(monkeypatch):
    loop = asyncio.get_running_loop()
    now = loop.time()
    monkeypatch.setattr(loop, "time", lambda: now)
    with pytest.raises(CompactionIdleTimeoutError):
        async with compaction_progress_timeout(idle_timeout_seconds=120.0) as progress:
            now += 121.0
            progress.observe(SimpleNamespace(kind="text_delta", text="too late"))


@pytest.mark.asyncio
async def test_user_cancellation_is_not_a_compaction_timeout():
    entered = asyncio.Event()

    async def stream():
        async with compaction_progress_timeout(idle_timeout_seconds=120.0):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(stream())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_provider_timeout_retains_its_identity():
    error = TimeoutError("provider transport timed out")
    with pytest.raises(TimeoutError) as caught:
        async with compaction_progress_timeout(idle_timeout_seconds=120.0):
            raise error
    assert caught.value is error


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [
    "progress", "heartbeat", "operation_deadline",
    "default_progress", "default_operation_deadline", "default_parent_deadline",
])
async def test_real_compaction_call_uses_idle_guard_and_keeps_normal_io_policy(monkeypatch, mode):
    from opensquilla.provider.types import (
        ChatConfig,
        DoneEvent,
        ReasoningDeltaEvent,
        TextDeltaEvent,
    )
    from opensquilla.session.compaction import CompactionRequestContext, call_compaction_provider
    from tests.helpers.compaction import synthetic_compaction_config

    loop = asyncio.get_running_loop()
    now = loop.time()
    deadline = now + (
        200.0 if mode in {"operation_deadline", "default_parent_deadline"} else 600.0
    )
    monkeypatch.setattr(loop, "time", lambda: now)
    monkeypatch.setattr("opensquilla.session.compaction.time.monotonic", lambda: now)
    config = synthetic_compaction_config()
    provider = config.llm_plan.primary.provider
    sent_configs = []
    closed = []
    failures = []

    async def chat(messages, tools=None, config=None):
        nonlocal now
        sent_configs.append(config)
        try:
            for _ in range(8 if mode == "default_operation_deadline" else 4):
                now += 80.0
                await asyncio.sleep(0)
                yield (
                    SimpleNamespace(kind="provider_heartbeat")
                    if mode == "heartbeat"
                    else ReasoningDeltaEvent(text="Still deriving the summary.")
                )
            yield TextDeltaEvent(text="Earlier work completed.")
            yield DoneEvent(stop_reason="end_turn")
        finally:
            closed.append(True)

    monkeypatch.setattr(provider, "chat", chat)
    request = call_compaction_provider(
        "Source context", "", config.llm_plan,
        # Explicit old 90s is now an idle guard; normal I/O remains 245s.
        timeout=90.0,
        request_context=CompactionRequestContext(chat_config=ChatConfig(
            timeout=245.0,
            turn_deadline_at_monotonic=(deadline if mode == "default_parent_deadline" else None),
        )),
        on_summary_failure=failures.append,
        # Direct compatibility calls omit the operation deadline. They must
        # still use the shared 600s default, constrained by an earlier parent.
        **({} if mode.startswith("default_") else {"deadline_at_monotonic": deadline}),
    )
    if mode.endswith("deadline"):
        with pytest.raises(CompactionOperationTimeoutError):
            await request
    else:
        result = await request
        assert result == ("Earlier work completed." if mode.endswith("progress") else None)
    assert len(sent_configs) == 1
    assert sent_configs[0].timeout == 245.0
    assert sent_configs[0].turn_deadline_at_monotonic == deadline
    assert closed == [True]
    assert failures == (["idle_timeout"] if mode == "heartbeat" else [])


def test_gateway_reports_failure_kind_without_breaking_legacy_observers():
    from opensquilla.gateway.adapters.session_maintenance import GatewaySessionMaintenancePorts

    reporter = Mock()
    ports = GatewaySessionMaintenancePorts(SimpleNamespace(
        session_manager=None, turn_runner=SimpleNamespace(_record_compaction_failure=reporter),
    ))
    ports._report_summary_outcome(
        "session", summary_attempted=True, success=False, failure_kind="operation_timeout",
    )
    reporter.assert_called_once_with("session", failure_kind="operation_timeout")
    legacy = []
    ports._context.turn_runner._record_compaction_failure = lambda key: legacy.append(key)
    ports._report_summary_outcome(
        "session", summary_attempted=True, success=False, failure_kind="operation_timeout",
    )
    assert legacy == ["session"]
