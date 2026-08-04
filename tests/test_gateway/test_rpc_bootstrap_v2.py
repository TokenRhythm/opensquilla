from __future__ import annotations

import pytest

from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.gateway.scopes import METHOD_SCOPES, READ_SCOPE
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import (
    AgentTaskRecord,
    AgentTaskStatus,
    PlanRunRecord,
    SessionSummary,
)
from opensquilla.session.plans import new_plan_revision
from opensquilla.session.storage import SessionStorage


def _wire_bytes(frame: object) -> int:
    return len(frame.model_dump_json().encode("utf-8"))  # type: ignore[attr-defined]


def test_sessions_bootstrap_v2_is_read_scoped() -> None:
    assert METHOD_SCOPES["sessions.bootstrap.v2"] == READ_SCOPE


@pytest.mark.asyncio
async def test_sessions_bootstrap_v2_budgets_the_whole_snapshot_and_keeps_legacy(
    tmp_path,
) -> None:
    storage = SessionStorage(str(tmp_path / "bootstrap-v2-budget.db"))
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    session_key = "agent:main:webchat:bootstrap-v2-budget"
    contents = [f"message-{index}:" + (chr(97 + index) * 22_000) for index in range(6)]
    config = GatewayConfig(workspace_dir=str(tmp_path / "workspace"))
    try:
        session = await manager.create(session_key, display_name="Budgeted session")
        for content in contents:
            await manager.append_message(session_key, "user", content)
        summary_text = "summary body " * 8_000
        await storage.save_summary(
            SessionSummary(
                session_id=session.session_id,
                session_key=session_key,
                compaction_id="bootstrap-v2-summary",
                summary_text=summary_text,
                removed_count=20,
                kept_count=2,
                covered_through_id=12,
            )
        )
        dispatcher = get_dispatcher()

        response = await dispatcher.dispatch(
            "bootstrap-v2-budget",
            "sessions.bootstrap.v2",
            {
                "key": session_key,
                "limit": 200,
                "maxResponseBytes": 64 * 1024,
            },
            RpcContext(
                conn_id="bootstrap-v2-budget",
                session_manager=manager,
                config=config,
            ),
        )

        assert response.ok is True
        payload = response.payload
        assert payload["session"]["session_key"] == session_key
        assert payload["byte_budget"] == 64 * 1024
        assert payload["envelope_reserve_bytes"] == 8 * 1024
        assert payload["wire_bytes"] == _wire_bytes(response)
        assert payload["wire_bytes"] <= 64 * 1024
        assert payload["truncated_by_bytes"] is True
        returned = payload["history"]["messages"]
        assert 0 < len(returned) < len(contents)
        assert [message["text"] for message in returned] == contents[-len(returned) :]
        assert payload["history"]["has_more"] is True
        assert payload["history"]["loaded_count"] == len(returned)
        summary = payload["history"]["compaction_summaries"][0]
        assert "summary_text" not in summary
        assert summary["summary_bytes"] == len(summary_text.encode("utf-8"))
        assert payload["epoch"] == 0
        assert "stream_cursor" in payload

        legacy = await dispatcher.dispatch(
            "bootstrap-v1-unchanged",
            "sessions.bootstrap",
            {"key": session_key, "limit": 200},
            RpcContext(
                conn_id="bootstrap-v1-unchanged",
                session_manager=manager,
                config=config,
            ),
        )
        assert legacy.ok is True
        assert [message["text"] for message in legacy.payload["history"]["messages"]] == (
            contents
        )
        assert legacy.payload["history"]["compaction_summaries"][0][
            "summary_text"
        ] == summary_text
        assert "wire_bytes" not in legacy.payload
        assert "truncated_by_bytes" not in legacy.payload
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_sessions_bootstrap_v2_bounds_legacy_unconstrained_display_name(tmp_path) -> None:
    storage = SessionStorage(str(tmp_path / "bootstrap-v2-fixed.db"))
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    session_key = "agent:main:webchat:bootstrap-v2-fixed"
    config = GatewayConfig(workspace_dir=str(tmp_path / "workspace"))
    try:
        await manager.create(session_key, display_name="x" * 70_000)

        response = await get_dispatcher().dispatch(
            "bootstrap-v2-fixed",
            "sessions.bootstrap.v2",
            {"key": session_key, "maxResponseBytes": 64 * 1024},
            RpcContext(
                conn_id="bootstrap-v2-fixed",
                session_manager=manager,
                config=config,
            ),
        )

        assert response.ok is True
        assert _wire_bytes(response) <= 64 * 1024
        assert response.payload["truncated_by_bytes"] is True
        assert len(response.payload["session"]["display_name"].encode("utf-8")) <= 4 * 1024
        assert response.payload["truncated_fields"] == [
            {
                "path": "session.display_name",
                "original_bytes": 70_000,
                "preview_bytes": 4 * 1024,
            }
        ]

        legacy = await get_dispatcher().dispatch(
            "bootstrap-fixed-legacy",
            "sessions.bootstrap",
            {"key": session_key},
            RpcContext(
                conn_id="bootstrap-fixed-legacy",
                session_manager=manager,
                config=config,
            ),
        )
        assert legacy.ok is True
        assert legacy.payload["session"]["display_name"] == "x" * 70_000

        follow_up = await get_dispatcher().dispatch(
            "bootstrap-v2-fixed-follow-up",
            "health",
            {},
            RpcContext(conn_id="bootstrap-v2-fixed-follow-up"),
        )
        assert follow_up.ok is True
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_sessions_bootstrap_v2_refits_history_around_large_fixed_metadata(
    tmp_path,
) -> None:
    storage = SessionStorage(str(tmp_path / "bootstrap-v2-refit.db"))
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    session_key = "agent:main:webchat:bootstrap-v2-refit"
    config = GatewayConfig(workspace_dir=str(tmp_path / "workspace"))
    content = "large-body-" + ("海🙂" * 8_000)
    try:
        await manager.create(session_key, display_name="metadata-" + ("m" * 40_000))
        await manager.append_message(session_key, "assistant", content)

        response = await get_dispatcher().dispatch(
            "bootstrap-v2-refit",
            "sessions.bootstrap.v2",
            {"key": session_key, "maxResponseBytes": 64 * 1024},
            RpcContext(
                conn_id="bootstrap-v2-refit",
                session_manager=manager,
                config=config,
            ),
        )

        assert response.ok is True
        assert _wire_bytes(response) <= 64 * 1024
        assert response.payload["truncated_by_bytes"] is True
        message = response.payload["history"]["messages"][0]
        assert message["preview"] == content[: len(message["preview"])]
        assert message["detail_ref"]["method"] == "chat.history.entry.v1"
        assert message["original_bytes"] > len(content.encode("utf-8"))
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_sessions_bootstrap_v2_never_loads_large_task_or_plan_bodies(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = SessionStorage(str(tmp_path / "bootstrap-v2-bounded-state.db"))
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    session_key = "agent:main:webchat:bootstrap-v2-bounded-state"
    config = GatewayConfig(workspace_dir=str(tmp_path / "workspace"))
    huge = "界" * 400_000
    try:
        session = await manager.create(session_key)
        await manager.append_message(session_key, "assistant", "session remains readable")
        await storage.create_agent_task(
            AgentTaskRecord(
                task_id="bounded-bootstrap-task",
                session_key=session_key,
                agent_id="main",
                source_kind="webchat",
                queue_mode="followup",
                run_kind="default",
                status=AgentTaskStatus.RUNNING,
                error_message=huge,
                terminal_reason=huge,
                details={"opaque": huge},
            )
        )
        revision = await storage.create_plan_revision(
            new_plan_revision(
                source_session_key=session_key,
                source_session_id=session.session_id,
                source_epoch=int(session.epoch or 0),
                title="Bounded bootstrap plan",
                markdown="界" * 100_000,
                steps=[
                    {
                        "step_id": f"step-{index}",
                        "title": f"Step {index}",
                        "details": "界" * 4_000,
                    }
                    for index in range(64)
                ],
            ),
            expected_parent_revision_id=None,
        )
        run = await storage.start_plan_run(
            PlanRunRecord(
                run_id="bounded-bootstrap-run",
                session_key=session_key,
                session_id=session.session_id,
                session_epoch=int(session.epoch or 0),
                plan_revision_id=revision.revision_id,
                status="queued",
                pause_reason=huge,
                terminal_reason=huge,
            )
        )

        async def _must_not_load_full_rows(*args, **kwargs):
            raise AssertionError("v2 bootstrap must use bounded state metadata")

        monkeypatch.setattr(storage, "list_recent_agent_tasks", _must_not_load_full_rows)
        monkeypatch.setattr(storage, "get_current_plan_revision", _must_not_load_full_rows)
        monkeypatch.setattr(storage, "get_active_plan_run", _must_not_load_full_rows)

        response = await get_dispatcher().dispatch(
            "bootstrap-v2-bounded-state",
            "sessions.bootstrap.v2",
            {"key": session_key, "maxResponseBytes": 64 * 1024},
            RpcContext(
                conn_id="bootstrap-v2-bounded-state",
                session_manager=manager,
                config=config,
            ),
        )

        assert response.ok is True
        assert _wire_bytes(response) <= 64 * 1024
        assert len(response.payload["tasks"]) == 1
        task = response.payload["tasks"][0]
        assert task["task_id"] == "bounded-bootstrap-task"
        assert task["status"] == "running"
        assert "details" not in task
        assert "terminal_message" not in task
        assert response.payload["currentPlan"]["revisionId"] == revision.revision_id
        assert response.payload["currentPlan"]["bodyDeferred"] is True
        assert response.payload["currentPlan"]["markdown"] == ""
        assert response.payload["activePlanRun"]["runId"] == run.run_id
        assert response.payload["activePlanRun"]["stateDeferred"] is True
        assert response.payload["activePlanRun"]["steps"] == []
        assert "currentPlan.markdown" in response.payload["truncated_fields"]
        assert "activePlanRun.steps" in response.payload["truncated_fields"]
        assert "tasks[].details" in response.payload["truncated_fields"]
    finally:
        await storage.close()
