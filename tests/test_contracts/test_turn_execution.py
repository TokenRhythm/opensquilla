"""Focused tests for the pure turn execution contract."""

from __future__ import annotations

import ast
import asyncio
import copy
from pathlib import Path

import pytest

from opensquilla.contracts.turn_execution import (
    ProviderAdmissionError,
    RecoveryContext,
    StickyExecutionRole,
    TurnExecutionContext,
    TurnIdentity,
)


def test_turn_execution_contract_is_a_pure_leaf() -> None:
    source_path = (
        Path(__file__).parents[2]
        / "src"
        / "opensquilla"
        / "contracts"
        / "turn_execution.py"
    )
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported_modules = {
        node.module or ""
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
    }
    imported_modules.update(
        alias.name
        for node in tree.body
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    assert not any(
        module.startswith(("opensquilla.engine", "opensquilla.provider", "opensquilla.gateway"))
        for module in imported_modules
    )
    assert "pydantic" not in imported_modules


def test_turn_execution_context_rejects_copy_and_deepcopy() -> None:
    context = TurnExecutionContext.create(
        TurnIdentity(
            turn_id="turn-copy",
            assistant_message_id="assistant-copy",
            session_key="agent:main:copy",
        )
    )

    with pytest.raises(TypeError, match="cannot be copied"):
        copy.copy(context)
    with pytest.raises(TypeError, match="cannot be copied"):
        copy.deepcopy(context)


@pytest.mark.asyncio
async def test_context_tracks_sticky_attempts_generation_and_stale_events() -> None:
    context = TurnExecutionContext.create(
        TurnIdentity(
            turn_id="turn-ledger",
            assistant_message_id="assistant-ledger",
            session_key="agent:main:ledger",
            turn_start_sequence=10,
        )
    )

    lease = await context.admit_provider_call(
        StickyExecutionRole.PRIMARY_AGGREGATOR,
        logical_call_index=0,
        attempt_index=0,
        owner="primary-coordinator",
    )
    await context.record_request_start(lease, {"request_started": True})
    await context.finish_provider_call(lease, {"ok": True})
    assert context.counters["proposer_request_starts"] == 0

    assert context.accept_event(0, 11, True, "upstream") is True
    assert context.accept_event(0, 11, True, "upstream") is False
    assert context.accept_event(0, 10, True, "upstream") is False
    assert context.accept_event(0, 12, True, "synthetic_heartbeat") is True
    assert context.last_meaningful_progress_at is not None

    recovery = RecoveryContext(successful_drafts=([{"text": "draft"}],))
    activation = await context.activate_fixed(
        StickyExecutionRole.FIXED_AGGREGATOR,
        "primary_failed",
        recovery,
    )
    assert activation.activated is True
    assert context.current_role() is StickyExecutionRole.FIXED_AGGREGATOR
    assert context.fallback_activation_count == 1
    assert context.recovery_context == recovery
    assert dict(context.recovery_markers) == {
        "ensemble_fallback_consumed": True,
        "fallback_started": True,
        "fixed_started": True,
        "recovery_terminal": False,
        "suppress_selector_fallback": True,
    }

    reset = context.begin_generation_reset(
        StickyExecutionRole.PRIMARY_AGGREGATOR,
        StickyExecutionRole.FIXED_AGGREGATOR,
        "takeover",
    )
    assert reset.assistant_message_id == "assistant-ledger"
    assert reset.old_generation_epoch == 0
    assert reset.new_generation_epoch == 1
    assert context.accept_event(0, reset.sequence + 1, True, "upstream") is False
    assert context.accept_event(1, reset.sequence + 1, True, "upstream") is True


@pytest.mark.asyncio
async def test_generation_reset_preserves_completed_tools_and_drops_pending_tools() -> None:
    context = TurnExecutionContext.create(
        TurnIdentity("turn-tools-reset", "assistant-tools-reset", "agent:main:tools-reset")
    )
    pending = context.open_tool_buffer("pending-call")
    pending.events.append({"kind": "tool_use_start", "tool_use_id": "pending-call"})
    completed = context.open_tool_buffer("completed-call")
    completed.events.append({"kind": "tool_use_start", "tool_use_id": "completed-call"})
    await context.commit_tool_round("completed-call", {"kind": "done"})

    context.begin_generation_reset(
        StickyExecutionRole.PRIMARY_AGGREGATOR,
        StickyExecutionRole.FIXED_DIRECT,
        "provider_takeover",
    )

    assert context.pending_tool_buffers == {}
    assert [record.call_id for record in context.completed_tools] == ["completed-call"]


@pytest.mark.asyncio
async def test_attempt_one_requires_finished_attempt_zero_and_request_start_is_single_use() -> None:
    context = TurnExecutionContext.create(
        TurnIdentity("turn-attempt-order", "assistant-attempt-order", "agent:main:attempt-order")
    )
    first = await context.admit_provider_call(
        StickyExecutionRole.PRIMARY_AGGREGATOR,
        logical_call_index=0,
        attempt_index=0,
        owner="primary",
    )

    with pytest.raises(ProviderAdmissionError, match="finished attempt 0"):
        await context.admit_provider_call(
            StickyExecutionRole.PRIMARY_AGGREGATOR,
            logical_call_index=0,
            attempt_index=1,
            owner="primary-retry",
        )

    await context.record_request_start(first, {"request_started": True})
    with pytest.raises(ProviderAdmissionError, match="already recorded"):
        await context.record_request_start(first, {"request_started": True})
    await context.finish_provider_call(first, {"ok": False})

    second = await context.admit_provider_call(
        StickyExecutionRole.PRIMARY_AGGREGATOR,
        logical_call_index=0,
        attempt_index=1,
        owner="primary-retry",
    )
    await context.finish_provider_call(second, {"ok": True})


@pytest.mark.asyncio
async def test_terminal_reset_rejects_late_events_calls_and_post_fixed_proposers() -> None:
    context = TurnExecutionContext.create(
        TurnIdentity(
            "turn-terminal-boundary",
            "assistant-terminal-boundary",
            "agent:main:terminal-boundary",
        )
    )
    await context.activate_fixed(StickyExecutionRole.FIXED_DIRECT, "fixed takeover")
    with pytest.raises(ProviderAdmissionError, match="proposer admission"):
        await context.admit_provider_call(
            StickyExecutionRole.PROPOSER,
            logical_call_index=0,
            attempt_index=0,
            owner="late-proposer",
        )

    reset = context.begin_generation_reset(
        StickyExecutionRole.FIXED_DIRECT,
        StickyExecutionRole.FIXED_DIRECT,
        "fixed final failure",
        terminal=True,
    )
    assert context.terminal is True
    assert (
        context.accept_event(
            reset.new_generation_epoch,
            reset.sequence + 1,
            True,
            "upstream",
        )
        is False
    )
    with pytest.raises(ProviderAdmissionError, match="terminal"):
        await context.admit_provider_call(
            StickyExecutionRole.FIXED_DIRECT,
            logical_call_index=0,
            attempt_index=0,
            owner="late-fixed",
        )


@pytest.mark.asyncio
async def test_control_terminal_is_single_and_releases_unpublished_reservation() -> None:
    context = TurnExecutionContext.create(
        TurnIdentity(
            "turn-control-terminal",
            "assistant-control-terminal",
            "agent:main:control-terminal",
        )
    )

    sequence = context.begin_control_terminal("cancel")

    assert sequence is not None
    assert context.terminal is True
    assert context.publication_ledger.terminal is True
    assert context.publication_ledger.released is True
    assert context.begin_control_terminal("cancel") is None
    assert dict(context.recovery_markers) == {
        "ensemble_fallback_consumed": False,
        "fallback_started": False,
        "fixed_started": False,
        "recovery_terminal": False,
        "suppress_selector_fallback": True,
    }
    assert context.accept_event(
        context.generation_epoch,
        sequence + 1,
        True,
        "upstream",
    ) is False
    with pytest.raises(ProviderAdmissionError, match="terminal"):
        await context.admit_provider_call(
            StickyExecutionRole.PRIMARY_AGGREGATOR,
            logical_call_index=0,
            attempt_index=0,
            owner="late-primary",
        )


@pytest.mark.asyncio
async def test_failed_fixed_recovery_has_terminal_markers_without_fixed_start() -> None:
    context = TurnExecutionContext.create(
        TurnIdentity(
            "turn-missing-fixed",
            "assistant-missing-fixed",
            "agent:main:missing-fixed",
        )
    )

    await context.begin_failed_fixed_recovery(
        StickyExecutionRole.FIXED_DIRECT,
        "missing fixed provider",
    )

    assert context.current_role() is StickyExecutionRole.PRIMARY_AGGREGATOR
    assert dict(context.recovery_markers) == {
        "ensemble_fallback_consumed": True,
        "fallback_started": True,
        "fixed_started": False,
        "recovery_terminal": True,
        "suppress_selector_fallback": True,
    }


def test_deadline_is_context_owned(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 100.0
    monkeypatch.setattr(
        "opensquilla.contracts.turn_execution.time.monotonic",
        lambda: now,
    )
    context = TurnExecutionContext.create(
        TurnIdentity("turn-watchdog", "assistant-watchdog", "agent:main:watchdog")
    )

    assert context.set_deadline_if_missing(200.0) is True
    assert context.set_deadline_if_missing(300.0) is False
    assert context.deadline == 200.0


@pytest.mark.asyncio
async def test_context_close_joins_cleanup_and_releases_unpublished_reservation() -> None:
    context = TurnExecutionContext.create(
        TurnIdentity("turn-close", "assistant-close", "agent:main:close")
    )
    task = asyncio.create_task(asyncio.sleep(0))
    context.reserve_cleanup(task, "provider-cleanup")

    await context.close()
    await context.close()

    assert context.closed is True
    assert context.publication_ledger.released is True
    assert context.publication_ledger.visible_output is False
