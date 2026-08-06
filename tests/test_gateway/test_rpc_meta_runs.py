"""Read-only meta-skill run history RPC handlers."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from opensquilla.engine.steps.meta_command import (
    pending_meta_replay_count,
    pending_meta_replay_pop,
)
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.protocol import ERROR_INVALID_REQUEST, ERROR_UNAUTHORIZED
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
    _handle_meta_runs_recovery,
    _handle_meta_runs_replay,
    _handle_meta_runs_show,
    _handle_meta_runs_validate,
)
from opensquilla.gateway.scopes import ADMIN_SCOPE, METHOD_SCOPES, READ_SCOPE
from opensquilla.persistence.meta_run_writer import open_meta_run_writer
from opensquilla.persistence.migrator import apply_pending
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage
from opensquilla.skills.meta.inputs import make_meta_inputs
from opensquilla.skills.meta.scheduler import _preflight_missing_fields
from opensquilla.skills.meta.types import MetaPlan, MetaResult, MetaStep

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


def _seed_writer(
    migrated_db: Path,
    *,
    final_status: str = "ok",
    final_result: MetaResult | None = None,
):
    db_path = migrated_db
    if migrated_db.is_dir():
        db_path = migrated_db / "runs.db"
        apply_pending(str(db_path), MIGRATIONS_DIR)
    writer = open_meta_run_writer(str(db_path))
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


def _seed_paid_failed_writer(tmp_path: Path, *, safe_no_submit: bool):
    from opensquilla.skills.meta.replay_safety import encode_paid_replay_safety

    db = str(tmp_path / "paid-runs.db")
    apply_pending(db, MIGRATIONS_DIR)
    writer = open_meta_run_writer(db)
    plan = MetaPlan(
        name="meta-paid-test",
        triggers=("paid",),
        priority=10,
        steps=(
            MetaStep(
                id="paid",
                skill="seedance-2-prompt",
                kind="skill_exec",
                side_effect="external_paid_submit",
            ),
        ),
    )
    run_id = writer.begin_run_sync(
        meta_skill_name=plan.name,
        meta_plan=plan,
        triggered_by="manual_command",
        inputs={"user_message": "make a synthetic clip"},
        session_key="sess-1",
        turn_id="turn-paid",
    )
    writer.begin_step_sync(
        run_id=run_id,
        step=plan.steps[0],
        effective_skill="seedance-2-prompt",
        rendered_inputs={"prompt": "synthetic clip"},
    )
    writer.finish_step_sync(
        run_id=run_id,
        step_id="paid",
        status="failed",
        output_text=None,
        error=encode_paid_replay_safety(
            "provider response was lost",
            safe_no_submit=safe_no_submit,
        ),
    )
    writer.finish_run_sync(
        run_id=run_id,
        status="failed",
        result=MetaResult(ok=False, error="generation failed", failed_step_id="paid"),
    )
    return writer, run_id


def test_meta_runs_list_rpc_returns_summary(tmp_path: Path) -> None:
    writer, run_id = _seed_writer(tmp_path)
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


def test_meta_runs_recovery_rehydrates_failed_ribbon_after_writer_reopen(
    tmp_path: Path,
) -> None:
    db = str(tmp_path / "recovery-runs.db")
    apply_pending(db, MIGRATIONS_DIR)
    writer = open_meta_run_writer(db)
    plan = MetaPlan(
        name="meta-paper-write",
        triggers=("paper",),
        priority=10,
        steps=(
            MetaStep(
                id="draft",
                skill="writer",
                kind="agent",
                label="Draft",
                label_by_language={"zh": "撰写初稿"},
            ),
            MetaStep(
                id="publication_quality_gate",
                skill="quality-gate",
                kind="skill_exec",
                label="Publication quality gate",
                label_by_language={"zh": "出版质量检查"},
                depends_on=("draft",),
            ),
        ),
    )
    run_id = writer.begin_run_sync(
        meta_skill_name=plan.name,
        meta_plan=plan,
        triggered_by="manual_command",
        inputs={
            "user_message": "撰写论文",
            "user_language": "zh-CN",
            "replay_token": "ticket-secret-canary",
        },
        session_key="agent:main:paper-session",
        turn_id="turn-paper",
    )
    writer.begin_step_sync(
        run_id=run_id,
        step=plan.steps[0],
        effective_skill="writer",
        rendered_inputs={"task": "draft"},
    )
    writer.finish_step_sync(
        run_id=run_id,
        step_id="draft",
        status="ok",
        output_text="persisted draft artifact",
    )
    writer.begin_step_sync(
        run_id=run_id,
        step=plan.steps[1],
        effective_skill="quality-gate",
        rendered_inputs={"task": "validate"},
    )
    writer.finish_step_sync(
        run_id=run_id,
        step_id="publication_quality_gate",
        status="failed",
        output_text=None,
        error="PDF length gate rejected the one-page artifact; Bearer sk-test-secret",
    )
    writer.finish_run_sync(
        run_id=run_id,
        status="failed",
        result=MetaResult(
            ok=False,
            error="PDF length gate rejected the one-page artifact; Bearer sk-test-secret",
            failed_step_id="publication_quality_gate",
        ),
    )
    writer.close()

    # A new writer models a gateway process restart: no stream registry state
    # survives, but the recovery projection must remain complete and scoped.
    reopened = open_meta_run_writer(db)
    try:
        ctx = RpcContext(conn_id="test", meta_run_writer=reopened)
        payload = asyncio.run(_handle_meta_runs_recovery({
            "sessionKey": "agent:main:paper-session",
        }, ctx))
        other_session = asyncio.run(_handle_meta_runs_recovery({
            "sessionKey": "agent:main:other-session",
        }, ctx))
    finally:
        reopened.close()

    recovery = payload["recovery"]
    assert recovery["run_id"] == run_id
    assert recovery["announced"] == {
        "run_id": run_id,
        "meta_skill_name": "meta-paper-write",
        "language": "zh-CN",
        "steps": [
            {"id": "draft", "label": "撰写初稿", "kind": "agent", "depends_on": []},
            {
                "id": "publication_quality_gate",
                "label": "出版质量检查",
                "kind": "skill_exec",
                "depends_on": ["draft"],
            },
        ],
        "total": 2,
        "parent_run_id": None,
    }
    states = {item["step_id"]: item for item in recovery["step_states"]}
    assert states["draft"]["state"] == "succeeded"
    assert states["publication_quality_gate"]["state"] == "failed"
    assert states["publication_quality_gate"]["error"] == (
        "This step failed in a previous run. Review its tool result before retrying."
    )
    assert "sk-test-secret" not in str(payload)
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "persisted draft artifact" not in serialized
    assert "撰写论文" not in serialized
    assert "ticket-secret-canary" not in serialized
    assert "inputs_json" not in serialized
    assert "plan_snapshot_json" not in serialized
    assert "output_text" not in serialized
    assert "retry-step" in {
        action["id"] for action in states["publication_quality_gate"]["rescue"]["actions"]
    }
    assert recovery["completed"]["completed_steps"] == ["draft"]
    assert recovery["completed"]["failed_steps"] == ["publication_quality_gate"]
    assert other_session == {"recovery": None}


def test_meta_runs_recovery_infers_resolved_missing_dependencies_as_skipped(
    tmp_path: Path,
) -> None:
    db = str(tmp_path / "skipped-recovery-runs.db")
    apply_pending(db, MIGRATIONS_DIR)
    writer = open_meta_run_writer(db)
    step_ids = [f"paper_step_{index}" for index in range(1, 37)]
    step_ids[0] = "paper_collect"
    step_ids[1] = "paper_clarify"
    step_ids[25] = "paper_contract"
    step_ids[26] = "final_manuscript_package"
    step_ids[30] = "publication_quality_gate"
    step_ids[31] = "compile_pdf"
    steps = tuple(
        MetaStep(
            id=step_id,
            skill="paper-worker",
            kind="agent",
            label=step_id.replace("_", " ").title(),
            depends_on=((step_ids[index - 1],) if index > 0 else ()),
            when=(
                "'NEEDS_CLARIFICATION: yes' in outputs.paper_collect"
                if step_id == "paper_clarify"
                else (
                    "'PAPER_MODE: COMPACT_SKELETON' in outputs.paper_contract"
                    if step_id == "final_manuscript_package"
                    else (
                        "'PAPER_MODE: FULL_MANUSCRIPT' in outputs.paper_contract "
                        "or 'PAPER_MODE: COMPACT_SKELETON' in outputs.paper_contract"
                        if step_id == "compile_pdf"
                        else ""
                    )
                )
            ),
        )
        for index, step_id in enumerate(step_ids)
    )
    plan = MetaPlan(
        name="meta-paper-write",
        triggers=("paper",),
        priority=10,
        steps=steps,
    )
    run_id = writer.begin_run_sync(
        meta_skill_name=plan.name,
        meta_plan=plan,
        triggered_by="manual_command",
        inputs={"user_message": "synthetic paper request"},
        session_key="paper-skipped-session",
        turn_id="turn-skipped",
    )
    skipped_ids = {"paper_clarify", "final_manuscript_package"}
    for index, step in enumerate(steps[:30], start=1):
        if step.id in skipped_ids:
            continue
        writer.begin_step_sync(
            run_id=run_id,
            step=step,
            effective_skill=step.skill,
            rendered_inputs={"step": index},
        )
        writer.finish_step_sync(
            run_id=run_id,
            step_id=step.id,
            status="ok",
            output_text=(
                "NEEDS_CLARIFICATION: no"
                if step.id == "paper_collect"
                else (
                    "PAPER_MODE: FULL_MANUSCRIPT"
                    if step.id == "paper_contract"
                    else f"completed-{index}"
                )
            ),
        )
    failed_step = steps[30]
    writer.begin_step_sync(
        run_id=run_id,
        step=failed_step,
        effective_skill=failed_step.skill,
        rendered_inputs={"step": 31},
    )
    writer.finish_step_sync(
        run_id=run_id,
        step_id=failed_step.id,
        status="failed",
        output_text=None,
        error="synthetic gate failure",
    )
    writer.finish_run_sync(
        run_id=run_id,
        status="failed",
        result=MetaResult(
            ok=False,
            error="synthetic gate failure",
            failed_step_id=failed_step.id,
        ),
    )
    try:
        ctx = RpcContext(conn_id="test", meta_run_writer=writer)
        payload = asyncio.run(_handle_meta_runs_recovery({
            "sessionKey": "paper-skipped-session",
        }, ctx))
    finally:
        writer.close()

    recovery = payload["recovery"]
    states = {item["step_id"]: item["state"] for item in recovery["step_states"]}
    assert len(recovery["announced"]["steps"]) == 36
    assert sum(state == "succeeded" for state in states.values()) == 28
    assert sum(state == "skipped" for state in states.values()) == 2
    assert states["paper_clarify"] == "skipped"
    assert states["final_manuscript_package"] == "skipped"
    assert states["publication_quality_gate"] == "failed"
    assert states["compile_pdf"] == "pending"
    assert sum(state == "pending" for state in states.values()) == 5
    assert recovery["completed"]["skipped_steps"] == [
        "paper_clarify",
        "final_manuscript_package",
    ]


def test_meta_runs_recovery_does_not_call_fail_open_row_gaps_skipped(
    tmp_path: Path,
) -> None:
    db = str(tmp_path / "fail-open-gap-runs.db")
    apply_pending(db, MIGRATIONS_DIR)
    writer = open_meta_run_writer(db)
    plan = MetaPlan(
        name="meta-gap",
        triggers=("gap",),
        priority=10,
        steps=(
            MetaStep(id="missing_success", skill="worker", kind="agent"),
            MetaStep(
                id="later_success",
                skill="worker",
                kind="agent",
                depends_on=("missing_success",),
            ),
            MetaStep(
                id="failed_gate",
                skill="gate",
                kind="agent",
                depends_on=("later_success",),
            ),
        ),
    )
    run_id = writer.begin_run_sync(
        meta_skill_name=plan.name,
        meta_plan=plan,
        triggered_by="manual_command",
        inputs={"user_message": "synthetic gap"},
        session_key="gap-session",
        turn_id="turn-gap",
    )
    writer.begin_step_sync(
        run_id=run_id,
        step=plan.steps[1],
        effective_skill="worker",
        rendered_inputs={},
    )
    writer.finish_step_sync(
        run_id=run_id,
        step_id="later_success",
        status="ok",
        output_text="later output",
    )
    writer.begin_step_sync(
        run_id=run_id,
        step=plan.steps[2],
        effective_skill="gate",
        rendered_inputs={},
    )
    writer.finish_step_sync(
        run_id=run_id,
        step_id="failed_gate",
        status="failed",
        output_text=None,
        error="failed",
    )
    writer.finish_run_sync(
        run_id=run_id,
        status="failed",
        result=MetaResult(ok=False, error="failed", failed_step_id="failed_gate"),
    )
    try:
        payload = asyncio.run(_handle_meta_runs_recovery({"sessionKey": "gap-session"}, RpcContext(
            conn_id="test",
            meta_run_writer=writer,
        )))
    finally:
        writer.close()

    states = {
        item["step_id"]: item["state"]
        for item in payload["recovery"]["step_states"]
    }
    assert states == {
        "missing_success": "pending",
        "later_success": "succeeded",
        "failed_gate": "failed",
    }
    assert payload["recovery"]["completed"]["skipped_steps"] == []


def test_meta_runs_recovery_hides_a_source_run_superseded_by_replay(
    tmp_path: Path,
) -> None:
    writer, source_run_id = _seed_writer(
        tmp_path,
        final_status="failed",
        final_result=MetaResult(ok=False, error="failed", failed_step_id="s1"),
    )
    plan = MetaPlan(
        name="alpha-skill",
        triggers=("alpha request",),
        priority=10,
        steps=(MetaStep(id="s1", skill="writer", kind="agent", label="Write"),),
    )
    replay_run_id = writer.begin_run_sync(
        meta_skill_name="alpha-skill",
        meta_plan=plan,
        triggered_by="manual_command",
        inputs={
            "user_message": "Write an alpha brief",
            "meta_replay_source_run_id": source_run_id,
            "meta_replay_mode": "failed-step",
        },
        session_key="sess-1",
        turn_id="turn-replay",
    )
    writer.finish_run_sync(
        run_id=replay_run_id,
        status="ok",
        result=MetaResult(ok=True, final_text="recovered"),
    )
    try:
        ctx = RpcContext(conn_id="test", meta_run_writer=writer)
        payload = asyncio.run(_handle_meta_runs_recovery({"sessionKey": "sess-1"}, ctx))
    finally:
        writer.close()

    assert payload == {"recovery": None}


def test_meta_runs_recovery_ignores_spoofed_and_cancelled_lineage(
    tmp_path: Path,
) -> None:
    writer, source_run_id = _seed_writer(
        tmp_path,
        final_status="failed",
        final_result=MetaResult(ok=False, error="failed", failed_step_id="s1"),
    )
    beta_plan = MetaPlan(
        name="beta-skill",
        triggers=("beta",),
        priority=10,
        steps=(MetaStep(id="s1", skill="writer", kind="agent"),),
    )
    spoofed = writer.begin_run_sync(
        meta_skill_name="beta-skill",
        meta_plan=beta_plan,
        triggered_by="manual_command",
        inputs={
            "meta_replay_source_run_id": source_run_id,
            "meta_replay_mode": "failed-step",
        },
        session_key="sess-1",
        turn_id="turn-spoofed",
    )
    writer.finish_run_sync(
        run_id=spoofed,
        status="ok",
        result=MetaResult(ok=True, final_text="unrelated"),
    )
    alpha_plan = MetaPlan(
        name="alpha-skill",
        triggers=("alpha",),
        priority=10,
        steps=(MetaStep(id="s1", skill="writer", kind="agent"),),
    )
    cancelled = writer.begin_run_sync(
        meta_skill_name="alpha-skill",
        meta_plan=alpha_plan,
        triggered_by="manual_command",
        inputs={
            "meta_replay_source_run_id": source_run_id,
            "meta_replay_mode": "failed-step",
        },
        session_key="sess-1",
        turn_id="turn-cancelled",
    )
    writer.finish_run_sync(run_id=cancelled, status="cancelled", result=None)
    try:
        ctx = RpcContext(conn_id="test", meta_run_writer=writer)
        payload = asyncio.run(_handle_meta_runs_recovery({"sessionKey": "sess-1"}, ctx))
    finally:
        writer.close()

    assert payload["recovery"]["run_id"] == source_run_id


def test_meta_runs_recovery_returns_the_latest_failed_replay_descendant(
    tmp_path: Path,
) -> None:
    writer, source_run_id = _seed_writer(
        tmp_path,
        final_status="failed",
        final_result=MetaResult(ok=False, error="first failure", failed_step_id="s1"),
    )
    plan = MetaPlan(
        name="alpha-skill",
        triggers=("alpha",),
        priority=10,
        steps=(MetaStep(id="s1", skill="writer", kind="agent", label="Write"),),
    )
    descendant_run_id = writer.begin_run_sync(
        meta_skill_name="alpha-skill",
        meta_plan=plan,
        triggered_by="manual_command",
        inputs={
            "meta_replay_source_run_id": source_run_id,
            "meta_replay_mode": "failed-step",
        },
        session_key="sess-1",
        turn_id="turn-descendant",
    )
    writer.finish_run_sync(
        run_id=descendant_run_id,
        status="failed",
        result=MetaResult(ok=False, error="second failure", failed_step_id="s1"),
    )
    try:
        ctx = RpcContext(conn_id="test", meta_run_writer=writer)
        payload = asyncio.run(_handle_meta_runs_recovery({"sessionKey": "sess-1"}, ctx))
    finally:
        writer.close()

    assert payload["recovery"]["run_id"] == descendant_run_id


def test_meta_runs_show_rpc_returns_steps(tmp_path: Path) -> None:
    writer, run_id = _seed_writer(tmp_path)
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
    assert replay["message"] == "/meta alpha-skill -- Write an alpha brief"
    assert "prior failed step" in replay["context_message"]
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

    message = payload["replay"]["context_message"]
    assert "Original request: Write an alpha brief" in message
    assert message.index("Original request") < message.index("Prior successful outputs")
    assert "...[truncated for replay]" in message


def test_meta_runs_live_replay_prepare_commit_is_one_time_and_token_free_on_turn(
    tmp_path: Path,
) -> None:
    writer, run_id = _seed_writer(
        tmp_path,
        final_status="failed",
        final_result=MetaResult(ok=False, error="failed", failed_step_id="s1"),
    )
    ctx = RpcContext(conn_id="test", meta_run_writer=writer)
    replay_nonce = ""
    try:
        prepared = asyncio.run(_handle_meta_runs_replay({
            "runId": run_id,
            "mode": "failed-step",
            "sessionKey": "sess-1",
            "prepareLive": True,
        }, ctx))["replay"]
        token = prepared["live_replay"]["replay_token"]
        assert token
        assert prepared["live_replay"]["prepared"] is True

        committed = asyncio.run(_handle_meta_runs_replay({
            "runId": run_id,
            "mode": "failed-step",
            "sessionKey": "sess-1",
            "replayToken": token,
        }, ctx))["replay"]
        launch_text = committed["launch_text"]
        assert launch_text.startswith("/meta-replay ")
        replay_nonce = launch_text.removeprefix("/meta-replay ")
        assert len(replay_nonce) == 32
        assert all(character in "0123456789abcdef" for character in replay_nonce)
        assert committed["live_replay"]["committed"] is True
        assert token not in str(committed)
        assert pending_meta_replay_count("sess-1") == 1

        with pytest.raises(Exception, match="expired|already used|does not match"):
            asyncio.run(_handle_meta_runs_replay({
                "runId": run_id,
                "mode": "failed-step",
                "sessionKey": "sess-1",
                "replayToken": token,
            }, ctx))
    finally:
        if replay_nonce:
            pending_meta_replay_pop("sess-1", replay_nonce)
        writer.close()


@pytest.mark.asyncio
async def test_meta_runs_live_replay_commit_is_durable_across_gateway_reopen(
    tmp_path: Path,
) -> None:
    writer, run_id = _seed_writer(
        tmp_path,
        final_status="failed",
        final_result=MetaResult(ok=False, error="failed", failed_step_id="s1"),
    )
    session_db = tmp_path / "sessions.db"
    storage = await SessionStorage.open(str(session_db))
    manager = SessionManager(storage, inject_time_prefix=False)
    await manager.create("sess-1", agent_id="main")
    ctx = RpcContext(
        conn_id="test",
        meta_run_writer=writer,
        session_manager=manager,
    )
    try:
        prepared = await _handle_meta_runs_replay({
            "runId": run_id,
            "mode": "failed-step",
            "sessionKey": "sess-1",
            "prepareLive": True,
        }, ctx)
        token = prepared["replay"]["live_replay"]["replay_token"]
        committed = await _handle_meta_runs_replay({
            "runId": run_id,
            "mode": "failed-step",
            "sessionKey": "sess-1",
            "replayToken": token,
        }, ctx)
        nonce = committed["replay"]["launch_text"].removeprefix("/meta-replay ")
        assert pending_meta_replay_count("sess-1") == 0
    finally:
        await storage.close()
        writer.close()

    reopened = await SessionStorage.open(str(session_db))
    try:
        intent = await reopened.get_meta_control_intent(
            session_key="sess-1",
            control_kind="replay",
            correlation_id=f"nonce:{nonce}",
        )
        assert intent is not None
        assert intent.status == "staged"
        assert intent.replay_run_id == run_id
        assert intent.replay_mode == "failed-step"
    finally:
        await reopened.close()


def test_live_replay_prepare_rejects_possibly_billed_paid_submit(tmp_path: Path) -> None:
    writer, run_id = _seed_paid_failed_writer(tmp_path, safe_no_submit=False)
    ctx = RpcContext(conn_id="test", meta_run_writer=writer)
    try:
        with pytest.raises(Exception, match="accepted or billed"):
            asyncio.run(_handle_meta_runs_replay({
                "runId": run_id,
                "mode": "failed-step",
                "sessionKey": "sess-1",
                "prepareLive": True,
            }, ctx))
        assert pending_meta_replay_count("sess-1") == 0
    finally:
        writer.close()


def test_fresh_run_replay_does_not_return_paid_resubmit_command(tmp_path: Path) -> None:
    writer, run_id = _seed_paid_failed_writer(tmp_path, safe_no_submit=False)
    ctx = RpcContext(conn_id="test", meta_run_writer=writer)
    try:
        replay = asyncio.run(_handle_meta_runs_replay({
            "runId": run_id,
            "mode": "run",
        }, ctx))["replay"]
    finally:
        writer.close()

    assert replay["message"] == ""
    assert replay["replay_kind"] == "blocked-paid-fresh-run"
    assert replay["live_replay"]["available"] is False
    assert "paid" in replay["live_replay"]["reason"].lower()


def test_live_replay_commit_revalidates_paid_submit_safety(tmp_path: Path) -> None:
    from opensquilla.skills.meta.replay_safety import encode_paid_replay_safety

    writer, run_id = _seed_paid_failed_writer(tmp_path, safe_no_submit=True)
    ctx = RpcContext(conn_id="test", meta_run_writer=writer)
    try:
        prepared = asyncio.run(_handle_meta_runs_replay({
            "runId": run_id,
            "mode": "failed-step",
            "sessionKey": "sess-1",
            "prepareLive": True,
        }, ctx))["replay"]
        token = prepared["live_replay"]["replay_token"]

        # Simulate a corrected durable classification before commit. The
        # second phase must validate current persisted evidence, not trust the
        # earlier UI/preparation response.
        writer._conn.execute(
            "UPDATE meta_skill_run_steps SET error=? WHERE run_id=? AND step_id='paid'",
            (
                encode_paid_replay_safety("response lost", safe_no_submit=False),
                run_id,
            ),
        )
        writer._conn.commit()

        with pytest.raises(Exception, match="accepted or billed"):
            asyncio.run(_handle_meta_runs_replay({
                "runId": run_id,
                "mode": "failed-step",
                "sessionKey": "sess-1",
                "replayToken": token,
            }, ctx))
        assert pending_meta_replay_count("sess-1") == 0
    finally:
        writer.close()


def test_meta_runs_live_replay_ticket_rejects_forged_cross_session_and_mode(
    tmp_path: Path,
) -> None:
    writer, run_id = _seed_writer(
        tmp_path,
        final_status="failed",
        final_result=MetaResult(ok=False, error="failed", failed_step_id="s1"),
    )
    ctx = RpcContext(conn_id="test", meta_run_writer=writer)
    replay_nonce = ""
    try:
        prepared = asyncio.run(_handle_meta_runs_replay({
            "runId": run_id,
            "mode": "failed-step",
            "sessionKey": "sess-1",
            "prepareLive": True,
        }, ctx))["replay"]
        token = prepared["live_replay"]["replay_token"]

        with pytest.raises(Exception, match="does not belong"):
            asyncio.run(_handle_meta_runs_replay({
                "runId": run_id,
                "mode": "failed-step",
                "sessionKey": "sess-other",
                "replayToken": token,
            }, ctx))
        with pytest.raises(Exception, match="expired|already used|does not match"):
            asyncio.run(_handle_meta_runs_replay({
                "runId": run_id,
                "mode": "partial-context",
                "sessionKey": "sess-1",
                "replayToken": token,
            }, ctx))
        with pytest.raises(Exception, match="expired|already used|does not match"):
            asyncio.run(_handle_meta_runs_replay({
                "runId": run_id,
                "mode": "failed-step",
                "sessionKey": "sess-1",
                "replayToken": "0" * 32,
            }, ctx))

        # Invalid claims did not burn the owner's correctly bound ticket.
        committed = asyncio.run(_handle_meta_runs_replay({
            "runId": run_id,
            "mode": "failed-step",
            "sessionKey": "sess-1",
            "replayToken": token,
        }, ctx))["replay"]
        assert committed["live_replay"]["committed"] is True
        replay_nonce = committed["launch_text"].removeprefix("/meta-replay ")
    finally:
        if replay_nonce:
            pending_meta_replay_pop("sess-1", replay_nonce)
        writer.close()


def test_meta_runs_live_replay_ticket_expires_before_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opensquilla.gateway.rpc_meta_runs as rpc_meta_runs

    writer, run_id = _seed_writer(
        tmp_path,
        final_status="failed",
        final_result=MetaResult(ok=False, error="failed", failed_step_id="s1"),
    )
    ctx = RpcContext(conn_id="test", meta_run_writer=writer)
    monkeypatch.setattr(rpc_meta_runs, "_META_REPLAY_TICKET_TTL_SECONDS", 0.0)
    try:
        prepared = asyncio.run(_handle_meta_runs_replay({
            "runId": run_id,
            "mode": "failed-step",
            "sessionKey": "sess-1",
            "prepareLive": True,
        }, ctx))["replay"]
        token = prepared["live_replay"]["replay_token"]

        with pytest.raises(Exception, match="expired|already used|does not match"):
            asyncio.run(_handle_meta_runs_replay({
                "runId": run_id,
                "mode": "failed-step",
                "sessionKey": "sess-1",
                "replayToken": token,
            }, ctx))
        assert pending_meta_replay_count("sess-1") == 0
    finally:
        writer.close()


@pytest.mark.parametrize("mode", ["failed-step", "partial-context"])
def test_meta_runs_live_replay_refuses_modified_saved_inputs(
    tmp_path: Path,
    mode: str,
) -> None:
    writer, run_id = _seed_writer(
        tmp_path,
        final_status="failed",
        final_result=MetaResult(ok=False, error="failed", failed_step_id="s1"),
    )
    writer._conn.execute(
        "UPDATE meta_skill_runs SET inputs_json=?, truncated_fields=? WHERE run_id=?",
        ('{"user_message":"[REDACTED]"}', "inputs_json_modified", run_id),
    )
    writer._conn.commit()
    ctx = RpcContext(conn_id="test", meta_run_writer=writer)
    try:
        with pytest.raises(Exception, match="saved request was redacted or truncated"):
            asyncio.run(_handle_meta_runs_replay({
                "runId": run_id,
                "mode": mode,
                "sessionKey": "sess-1",
                "prepareLive": True,
            }, ctx))
        assert pending_meta_replay_count("sess-1") == 0
    finally:
        writer.close()


def test_meta_runs_diff_rpc_compares_runs(tmp_path: Path) -> None:
    writer, left_run_id = _seed_writer(tmp_path)
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
    assert METHOD_SCOPES["meta.runs.recovery"] == ADMIN_SCOPE
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
        for method, params in (
            ("meta.runs.show", {"runId": run_id}),
            ("meta.runs.draft", {"runId": run_id}),
            ("meta.runs.recovery", {"sessionKey": "sess-1"}),
        ):
            res = await dispatcher.dispatch("r1", method, params, ctx)
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


@pytest.mark.asyncio
async def test_meta_runs_recovery_requires_explicit_session_key(tmp_path: Path) -> None:
    writer, _run_id = _seed_writer(tmp_path)
    try:
        ctx = RpcContext(conn_id="test", meta_run_writer=writer)
        res = await get_dispatcher().dispatch("r1", "meta.runs.recovery", {}, ctx)
        assert res.error is not None
        assert res.error.code == ERROR_INVALID_REQUEST
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
