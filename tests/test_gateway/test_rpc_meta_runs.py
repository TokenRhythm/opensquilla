"""Read-only meta-skill run history RPC handlers."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from opensquilla.gateway.auth import Principal
from opensquilla.gateway.protocol import ERROR_UNAUTHORIZED
from opensquilla.gateway.rpc import get_dispatcher
from opensquilla.gateway.rpc.registry import RpcContext
from opensquilla.gateway.rpc_meta_runs import (
    _bounded_limit,
    _handle_meta_runs_confirm_preflight,
    _handle_meta_runs_cost,
    _handle_meta_runs_diff,
    _handle_meta_runs_draft,
    _handle_meta_runs_eval_baseline,
    _handle_meta_runs_failures,
    _handle_meta_runs_list,
    _handle_meta_runs_replay,
    _handle_meta_runs_show,
    _handle_meta_runs_validate,
)
from opensquilla.gateway.scopes import ADMIN_SCOPE, METHOD_SCOPES, READ_SCOPE
from opensquilla.persistence.meta_run_writer import open_meta_run_writer
from opensquilla.skills.meta.inputs import make_meta_inputs
from opensquilla.skills.meta.scheduler import _preflight_missing_fields
from opensquilla.skills.meta.types import MetaPlan, MetaResult, MetaStep


def _seed_writer(
    migrated_db: Path,
    *,
    final_status: str = "ok",
    final_result: MetaResult | None = None,
):
    writer = open_meta_run_writer(str(migrated_db))
    plan = MetaPlan(
        name="alpha-skill",
        triggers=("alpha request",),
        priority=10,
        steps=(MetaStep(id="s1", skill="writer", kind="agent", label="Write"),),
        request_template={
            "outcome": "Brief",
            "fields": [
                {"name": "audience", "required": True},
                {"name": "language", "required": True},
            ],
        },
        output_contract={"required_sections": ["Summary"]},
        eval_prompts=[{
            "name": "brief",
            "prompt": "Write an alpha brief",
            "rubric": ["Summary"],
        }],
    )
    run_id = writer.begin_run_sync(
        meta_skill_name="alpha-skill",
        meta_plan=plan,
        triggered_by="soft_meta_invoke",
        inputs={"user_message": "Write an alpha brief"},
        session_key="sess-1",
        turn_id="turn-1",
    )
    writer.begin_step_sync(
        run_id=run_id,
        step=plan.steps[0],
        effective_skill="writer",
        rendered_inputs={"task": "Write an alpha brief"},
    )
    writer.finish_step_sync(
        run_id=run_id,
        step_id="s1",
        status="ok",
        output_text="done",
    )
    # finish_run_sync is status-guarded (running → terminal), so the seed
    # helper finalizes exactly once with the caller's desired terminal state.
    writer.finish_run_sync(
        run_id=run_id,
        status=final_status,  # type: ignore[arg-type]
        result=final_result or MetaResult(ok=True, final_text="done"),
    )
    return writer, run_id


def test_meta_runs_list_rpc_returns_summary(migrated_db: Path) -> None:
    writer, run_id = _seed_writer(migrated_db)
    try:
        ctx = RpcContext(conn_id="test", meta_run_writer=writer)
        payload = asyncio.run(_handle_meta_runs_list({"limit": 5}, ctx))
    finally:
        writer.close()

    assert payload["runs"][0]["run_id"] == run_id
    assert payload["runs"][0]["summary"]["step_count"] == 1
    assert payload["runs"][0]["summary"]["usage"]["available"] is False
    assert "inputs_json" not in payload["runs"][0]
    assert "plan_snapshot_json" not in payload["runs"][0]
    assert "final_text" not in payload["runs"][0]
    assert "steps" not in payload["runs"][0]
    assert "output_text" not in payload["runs"][0]["summary"]["steps"][0]
    assert "rendered_inputs_json" not in payload["runs"][0]["summary"]["steps"][0]
    assert payload["runs"][0]["validation"] == {
        "available": True,
        "request_template": True,
        "output_contract": True,
        "eval_baseline": True,
        "field_count": 2,
        "required_field_count": 2,
        "eval_prompt_count": 1,
    }


def test_meta_runs_failures_rpc_returns_summary_only(migrated_db: Path) -> None:
    writer, run_id = _seed_writer(
        migrated_db,
        final_status="failed",
        final_result=MetaResult(
            ok=False,
            error="raw secret failure detail",
            failed_step_id="s1",
        ),
    )
    try:
        ctx = RpcContext(conn_id="test", meta_run_writer=writer)
        payload = asyncio.run(_handle_meta_runs_failures({"limit": 500}, ctx))
    finally:
        writer.close()

    run = payload["runs"][0]
    assert run["run_id"] == run_id
    assert run["error_present"] is True
    assert "error" not in run
    assert "inputs_json" not in run
    assert "final_text" not in run


def test_meta_runs_show_rpc_returns_steps(migrated_db: Path) -> None:
    writer, run_id = _seed_writer(migrated_db)
    try:
        ctx = RpcContext(conn_id="test", meta_run_writer=writer)
        payload = asyncio.run(_handle_meta_runs_show({"runId": run_id}, ctx))
    finally:
        writer.close()

    run = payload["run"]
    assert run["run_id"] == run_id
    assert run["steps"][0]["step_id"] == "s1"
    assert run["summary"]["steps"][0]["output_chars"] == 4


def test_meta_runs_draft_rpc_returns_author_seed(migrated_db: Path) -> None:
    writer, run_id = _seed_writer(migrated_db)
    try:
        ctx = RpcContext(conn_id="test", meta_run_writer=writer)
        payload = asyncio.run(_handle_meta_runs_draft({"runId": run_id}, ctx))
    finally:
        writer.close()

    draft = payload["draft"]
    assert draft["source_run"]["run_id"] == run_id
    assert draft["name"] == "alpha-skill-draft"
    assert draft["request_template"]["outcome"] == "Brief"
    assert draft["eval_prompts"][0]["name"] == "brief"


def test_meta_runs_confirm_preflight_requires_template_fields(migrated_db: Path) -> None:
    writer, run_id = _seed_writer(migrated_db)
    try:
        ctx = RpcContext(conn_id="test", meta_run_writer=writer)
        payload = asyncio.run(_handle_meta_runs_confirm_preflight({
            "runId": run_id,
            "fields": {"audience": "engineers", "language": "zh-CN"},
            "interpretedRequest": "Write an alpha brief for engineers in zh-CN.",
        }, ctx))
    finally:
        writer.close()

    assert payload["confirmed"] is True
    assert payload["run_id"] == run_id
    assert payload["fields"]["audience"] == "engineers"
    assert "opensquilla:meta_preflight_confirmed=1" in payload["message"]
    assert "opensquilla:meta_preflight_run_id=" in payload["message"]
    assert "opensquilla:meta_preflight_fields=" in payload["message"]

    replay_inputs = make_meta_inputs(user_message=payload["message"])
    assert replay_inputs["meta_preflight_fields"] == {
        "audience": "engineers",
        "language": "zh-CN",
    }
    assert replay_inputs["collected"]["preflight"]["audience"] == "engineers"
    assert _preflight_missing_fields(
        {
            "fields": [
                {"name": "audience", "required": True},
                {"name": "language", "required": True},
            ],
        },
        replay_inputs,
    ) == []


def test_meta_runs_confirm_preflight_rejects_missing_fields(migrated_db: Path) -> None:
    writer, run_id = _seed_writer(migrated_db)
    try:
        ctx = RpcContext(conn_id="test", meta_run_writer=writer)
        with pytest.raises(Exception) as exc_info:
            asyncio.run(_handle_meta_runs_confirm_preflight({
                "runId": run_id,
                "fields": {"audience": "engineers"},
            }, ctx))
    finally:
        writer.close()

    assert "language" in str(exc_info.value)


def test_meta_runs_replay_rpc_returns_bounded_replay_message(migrated_db: Path) -> None:
    writer, run_id = _seed_writer(
        migrated_db,
        final_status="failed",
        final_result=MetaResult(ok=False, error="failed at writer", failed_step_id="s1"),
    )
    try:
        ctx = RpcContext(conn_id="test", meta_run_writer=writer)
        payload = asyncio.run(_handle_meta_runs_replay({
            "runId": run_id,
            "mode": "failed-step",
        }, ctx))
    finally:
        writer.close()

    replay = payload["replay"]
    assert replay["run_id"] == run_id
    assert replay["mode"] == "failed-step"
    assert replay["failed_step_id"] == "s1"
    assert "prior failed step" in replay["message"]
    assert replay["request"]["user_message"] == "Write an alpha brief"


def test_meta_runs_replay_keeps_original_request_before_large_outputs(
    migrated_db: Path,
) -> None:
    writer, run_id = _seed_writer(
        migrated_db,
        final_status="failed",
        final_result=MetaResult(ok=False, error="failed late", failed_step_id="s1"),
    )
    writer.finish_step_sync(
        run_id=run_id,
        step_id="s1",
        status="ok",
        output_text="x" * 10_000,
    )
    try:
        ctx = RpcContext(conn_id="test", meta_run_writer=writer)
        payload = asyncio.run(_handle_meta_runs_replay({
            "runId": run_id,
            "mode": "partial-context",
        }, ctx))
    finally:
        writer.close()

    message = payload["replay"]["message"]
    assert "Original request: Write an alpha brief" in message
    assert message.index("Original request") < message.index("Prior successful outputs")
    assert "...[truncated for replay]" in message


def test_meta_runs_diff_rpc_compares_runs(migrated_db: Path) -> None:
    writer, left_run_id = _seed_writer(migrated_db)
    plan = MetaPlan(
        name="alpha-skill",
        triggers=("alpha request",),
        priority=10,
        steps=(MetaStep(id="s1", skill="writer", kind="agent", label="Write"),),
    )
    right_run_id = writer.begin_run_sync(
        meta_skill_name="alpha-skill",
        meta_plan=plan,
        triggered_by="soft_meta_invoke",
        inputs={"user_message": "Write a revised alpha brief"},
        session_key="sess-1",
        turn_id="turn-2",
    )
    writer.finish_run_sync(
        run_id=right_run_id,
        status="failed",
        result=MetaResult(ok=False, error="boom", failed_step_id="s1"),
    )
    try:
        ctx = RpcContext(conn_id="test", meta_run_writer=writer)
        payload = asyncio.run(_handle_meta_runs_diff({
            "leftRunId": left_run_id,
            "rightRunId": right_run_id,
        }, ctx))
    finally:
        writer.close()

    diff = payload["diff"]
    assert diff["left"]["run_id"] == left_run_id
    assert diff["right"]["run_id"] == right_run_id
    assert diff["status_changed"] is True
    assert diff["final_text_chars_delta"] == -4
    assert diff["steps"][0]["step_id"] == "s1"


def test_meta_runs_cost_rpc_aggregates_persisted_step_usage(migrated_db: Path) -> None:
    writer, run_id = _seed_writer(migrated_db)
    writer.finish_step_sync(
        run_id=run_id,
        step_id="s1",
        status="ok",
        output_text="done",
        usage={
            "input_tokens": 50,
            "output_tokens": 10,
            "total_tokens": 60,
            "cost_usd": 0.0123,
            "billed_cost_usd": 0.0123,
            "cost_source": "provider_billed",
            "model": "gpt-test",
        },
    )
    try:
        ctx = RpcContext(conn_id="test", meta_run_writer=writer)
        payload = asyncio.run(_handle_meta_runs_cost({"limit": 5}, ctx))
    finally:
        writer.close()

    assert payload["aggregate"]["run_count"] == 1
    assert payload["aggregate"]["usage"]["available"] is True
    assert payload["aggregate"]["usage"]["input_tokens"] == 50
    assert payload["aggregate"]["usage"]["total_tokens"] == 60
    assert payload["aggregate"]["usage"]["cost_usd"] == pytest.approx(0.0123)
    assert payload["aggregate"]["usage"]["cost_source"] == "provider_billed"
    assert payload["runs"][0]["run_id"] == run_id
    assert payload["runs"][0]["usage"]["available"] is True
    assert payload["runs"][0]["steps"][0]["usage"]["model"] == "gpt-test"


def test_meta_runs_validate_rpc_exposes_spec_metadata(migrated_db: Path) -> None:
    writer, run_id = _seed_writer(migrated_db)
    try:
        ctx = RpcContext(conn_id="test", meta_run_writer=writer)
        payload = asyncio.run(_handle_meta_runs_validate({"runId": run_id}, ctx))
    finally:
        writer.close()

    validation = payload["validation"]
    assert validation["run_id"] == run_id
    assert validation["request_template"]["field_names"] == ["audience", "language"]
    assert validation["output_contract"]["required_sections"] == ["Summary"]
    assert validation["eval_prompts"][0]["name"] == "brief"


def test_meta_runs_eval_baseline_rpc_returns_deterministic_rubric(
    migrated_db: Path,
) -> None:
    writer, run_id = _seed_writer(migrated_db)
    try:
        ctx = RpcContext(conn_id="test", meta_run_writer=writer)
        payload = asyncio.run(_handle_meta_runs_eval_baseline({"runId": run_id}, ctx))
    finally:
        writer.close()

    baseline = payload["baseline"]
    assert baseline["run_id"] == run_id
    assert baseline["available"] is True
    assert baseline["items"][0]["name"] == "brief"
    assert baseline["items"][0]["judge"]["mode"] == "deterministic_metadata"


def test_meta_runs_rpc_scope_contract() -> None:
    assert METHOD_SCOPES["meta.runs.list"] == READ_SCOPE
    assert METHOD_SCOPES["meta.runs.failures"] == READ_SCOPE
    assert METHOD_SCOPES["meta.runs.show"] == ADMIN_SCOPE
    assert METHOD_SCOPES["meta.runs.draft"] == ADMIN_SCOPE
    assert METHOD_SCOPES["meta.runs.confirm_preflight"] == ADMIN_SCOPE
    assert METHOD_SCOPES["meta.runs.diff"] == ADMIN_SCOPE
    assert METHOD_SCOPES["meta.runs.replay"] == ADMIN_SCOPE
    assert METHOD_SCOPES["meta.runs.cost"] == READ_SCOPE
    assert METHOD_SCOPES["meta.runs.validate"] == ADMIN_SCOPE
    assert METHOD_SCOPES["meta.runs.eval_baseline"] == ADMIN_SCOPE


@pytest.mark.asyncio
async def test_meta_runs_show_and_draft_deny_read_only_dispatch(
    migrated_db: Path,
) -> None:
    writer, run_id = _seed_writer(migrated_db)
    read_only = Principal(
        role="operator",
        scopes=frozenset({READ_SCOPE}),
        is_owner=False,
        authenticated=True,
    )
    try:
        ctx = RpcContext(conn_id="test", principal=read_only, meta_run_writer=writer)
        dispatcher = get_dispatcher()
        for method in ("meta.runs.show", "meta.runs.draft"):
            res = await dispatcher.dispatch("r1", method, {"runId": run_id}, ctx)
            assert res.error is not None
            assert res.error.code == ERROR_UNAUTHORIZED
    finally:
        writer.close()


@pytest.mark.asyncio
async def test_meta_runs_read_only_requires_session_key_for_history(
    migrated_db: Path,
) -> None:
    writer, _run_id = _seed_writer(migrated_db)
    read_only = Principal(
        role="operator",
        scopes=frozenset({READ_SCOPE}),
        is_owner=False,
        authenticated=True,
    )
    try:
        ctx = RpcContext(conn_id="test", principal=read_only, meta_run_writer=writer)
        dispatcher = get_dispatcher()
        for method in ("meta.runs.list", "meta.runs.failures"):
            res = await dispatcher.dispatch("r1", method, {"limit": 5}, ctx)
            assert res.error is not None
            assert res.error.code == ERROR_UNAUTHORIZED
    finally:
        writer.close()


@pytest.mark.asyncio
async def test_meta_runs_read_only_allows_session_scoped_history(
    migrated_db: Path,
) -> None:
    writer, run_id = _seed_writer(migrated_db)
    other_plan = MetaPlan(
        name="beta-skill",
        triggers=("beta request",),
        priority=10,
        steps=(MetaStep(id="s1", skill="writer", kind="agent", label="Write"),),
    )
    other_run_id = writer.begin_run_sync(
        meta_skill_name="beta-skill",
        meta_plan=other_plan,
        triggered_by="soft_meta_invoke",
        inputs={"user_message": "Write a beta brief"},
        session_key="sess-2",
        turn_id="turn-2",
    )
    writer.finish_run_sync(
        run_id=other_run_id,
        status="ok",
        result=MetaResult(ok=True, final_text="done"),
    )
    read_only = Principal(
        role="operator",
        scopes=frozenset({READ_SCOPE}),
        is_owner=False,
        authenticated=True,
    )
    try:
        ctx = RpcContext(conn_id="test", principal=read_only, meta_run_writer=writer)
        res = await get_dispatcher().dispatch(
            "r1",
            "meta.runs.list",
            {"sessionKey": "sess-1", "limit": 5},
            ctx,
        )
        assert res.error is None, res.error
        assert [run["run_id"] for run in res.payload["runs"]] == [run_id]
        assert all(run["session_key"] == "sess-1" for run in res.payload["runs"])
    finally:
        writer.close()


@pytest.mark.asyncio
async def test_meta_runs_failures_read_only_allows_session_scoped_history(
    migrated_db: Path,
) -> None:
    writer, run_id = _seed_writer(
        migrated_db,
        final_status="failed",
        final_result=MetaResult(ok=False, error="failed", failed_step_id="s1"),
    )
    other_plan = MetaPlan(
        name="beta-skill",
        triggers=("beta request",),
        priority=10,
        steps=(MetaStep(id="s1", skill="writer", kind="agent", label="Write"),),
    )
    other_run_id = writer.begin_run_sync(
        meta_skill_name="beta-skill",
        meta_plan=other_plan,
        triggered_by="soft_meta_invoke",
        inputs={"user_message": "Write a beta brief"},
        session_key="sess-2",
        turn_id="turn-2",
    )
    writer.finish_run_sync(
        run_id=other_run_id,
        status="failed",
        result=MetaResult(ok=False, error="other failed", failed_step_id="s1"),
    )
    read_only = Principal(
        role="operator",
        scopes=frozenset({READ_SCOPE}),
        is_owner=False,
        authenticated=True,
    )
    try:
        ctx = RpcContext(conn_id="test", principal=read_only, meta_run_writer=writer)
        res = await get_dispatcher().dispatch(
            "r1",
            "meta.runs.failures",
            {"sessionKey": "sess-1", "limit": 5},
            ctx,
        )
        assert res.error is None, res.error
        assert [run["run_id"] for run in res.payload["runs"]] == [run_id]
        assert all(run["session_key"] == "sess-1" for run in res.payload["runs"])
    finally:
        writer.close()


@pytest.mark.asyncio
async def test_meta_runs_owner_read_scope_allows_session_history(
    migrated_db: Path,
) -> None:
    writer, run_id = _seed_writer(migrated_db)
    owner_read = Principal(
        role="operator",
        scopes=frozenset({READ_SCOPE}),
        is_owner=True,
        authenticated=False,
    )
    try:
        ctx = RpcContext(conn_id="test", principal=owner_read, meta_run_writer=writer)
        res = await get_dispatcher().dispatch(
            "r1",
            "meta.runs.list",
            {"sessionKey": "sess-1", "limit": 5},
            ctx,
        )
        assert res.error is None, res.error
        assert res.payload["runs"][0]["run_id"] == run_id
    finally:
        writer.close()


def test_meta_runs_rpc_limit_is_bounded() -> None:
    assert _bounded_limit(None) == 50
    assert _bounded_limit(-1) == 50
    assert _bounded_limit("5000") == 100
    assert _bounded_limit("12") == 12


def test_meta_runs_rpc_does_not_import_cli_private_helpers() -> None:
    source = Path("src/opensquilla/gateway/rpc_meta_runs.py").read_text()
    assert "opensquilla.cli.skills_meta_cmd" not in source
    assert "_meta_run_writer" not in source


def test_meta_runs_cli_uses_neutral_report_helpers() -> None:
    source = Path("src/opensquilla/cli/skills_meta_cmd.py").read_text(encoding="utf-8")
    assert "opensquilla.gateway.rpc_meta_runs" not in source
    assert "opensquilla.skills.meta.run_reports" in source
