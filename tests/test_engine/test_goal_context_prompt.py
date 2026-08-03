"""Unit tests for the Active Goal prompt block injected for Goal driver runs."""

from __future__ import annotations

from opensquilla.engine.runtime import TurnRunner
from opensquilla.session.models import GoalRunRecord, PlanRunRecord
from opensquilla.session.plans import new_plan_revision
from opensquilla.tools.types import CallerKind, ToolContext

SESSION_KEY = "agent:main:webchat:goal-context-source"

_MARKER_CONTRACT = (
    "Task contract: keep working toward the goal. End every reply with exactly "
    "one marker line: [goal:continue] | [goal:complete] | [goal:blocked:<reason>]"
)


def _tool_context(**overrides: object) -> ToolContext:
    values: dict[str, object] = {
        "caller_kind": CallerKind.WEB,
        "run_mode": "full",
        "workspace_dir": "/workspace/.opensquilla/workspace",
    }
    values.update(overrides)
    return ToolContext(**values)  # type: ignore[arg-type]


def _goal_run() -> GoalRunRecord:
    return GoalRunRecord(
        goal_id="goal-1",
        session_key=SESSION_KEY,
        agent_id="main",
        goal_text="Ship the goal mode.",
        status="running",
        progress=[{"summary": "Designed the marker protocol."}],
        turns=2,
        started_at=1_700_000_000_000,
        created_at=1_700_000_000_000,
        updated_at=1_700_000_000_000,
    )


def _goal_driver_run() -> PlanRunRecord:
    return PlanRunRecord(
        run_id="run-goal-1",
        session_key=SESSION_KEY,
        session_id="session-1",
        plan_revision_id="rev-goal-1",
        driver_kind="goal",
        driver_id="goal-1",
        status="running",
    )


def _manual_driver_run() -> PlanRunRecord:
    return PlanRunRecord(
        run_id="run-manual-1",
        session_key=SESSION_KEY,
        session_id="session-1",
        plan_revision_id="rev-manual-1",
        driver_kind="manual",
        status="running",
    )


def _manual_revision() -> object:
    return new_plan_revision(
        source_session_key=SESSION_KEY,
        source_session_id="session-1",
        source_epoch=0,
        title="Manual plan",
        markdown="Do the manual work.",
        steps=[{"step_id": "s1", "title": "Step one"}],
    )


def test_goal_run_injects_active_goal_block() -> None:
    ctx = _tool_context(
        plan_run_id="run-goal-1",
        plan_revision=_manual_revision(),
        plan_run=_goal_driver_run(),
        goal_run=_goal_run(),
    )

    extra = TurnRunner._extra_context_for_tool_context(ctx)

    assert "Approved Plan Execution" not in extra
    assert "PlanRun Progress" not in extra
    block = extra["Active Goal"]
    assert "Ship the goal mode." in block
    assert "Progress:" in block
    assert "Designed the marker protocol." in block
    assert "(no progress recorded yet)" not in block
    assert _MARKER_CONTRACT in block


def test_manual_plan_run_keeps_legacy_blocks() -> None:
    ctx = _tool_context(
        plan_run_id="run-manual-1",
        plan_revision=_manual_revision(),
        plan_run=_manual_driver_run(),
    )

    extra = TurnRunner._extra_context_for_tool_context(ctx)

    assert "Active Goal" not in extra
    assert "Approved Plan Execution" in extra
    assert "PlanRun Progress" in extra
    payload = extra["PlanRun Progress"]
    assert "run-manual-1" in payload


def test_goal_run_without_goal_entity_degrades_gracefully() -> None:
    ctx = _tool_context(
        plan_run_id="run-goal-1",
        plan_run=_goal_driver_run(),
        goal_run=None,
    )

    extra = TurnRunner._extra_context_for_tool_context(ctx)

    block = extra["Active Goal"]
    assert "temporarily unavailable" in block
    assert _MARKER_CONTRACT in block
