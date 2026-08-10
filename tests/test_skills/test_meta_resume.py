"""End-to-end programmatic resume tests (PR3, design §8.3)."""

from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from yoyo import get_backend, read_migrations

from opensquilla.persistence.meta_run_writer import MetaRunWriter
from opensquilla.skills.loader import SkillLoader
from opensquilla.skills.meta.events import _StepDone
from opensquilla.skills.meta.orchestrator import MetaOrchestrator
from opensquilla.skills.meta.parser import parse_meta_plan
from opensquilla.skills.meta.plan_serde import to_jsonable
from opensquilla.skills.meta.templating import render_with_args
from opensquilla.skills.meta.types import (
    ClarifyField,
    ClarifyStepConfig,
    MetaMatch,
    MetaPlan,
    MetaResult,
    MetaStep,
)

BUNDLED = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "opensquilla"
    / "skills"
    / "bundled"
)


@pytest.fixture(scope="session")
def _migrated_writer_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    db = tmp_path_factory.mktemp("meta-resume-template") / "template.sqlite"
    backend = get_backend(f"sqlite:///{db}")
    try:
        backend.apply_migrations(read_migrations("migrations"))
    finally:
        backend.connection.close()
    return db


@pytest.fixture
def writer(tmp_path: Path, _migrated_writer_template: Path) -> MetaRunWriter:
    db = tmp_path / "test.sqlite"
    shutil.copyfile(_migrated_writer_template, db)
    conn = sqlite3.connect(db, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return MetaRunWriter(conn)


def _plan_with_collect_then_summary() -> MetaPlan:
    cfg = ClarifyStepConfig(
        mode="form",
        fields=(
            ClarifyField(name="destination", type="string", required=True),
            ClarifyField(name="days", type="int", required=True, min=1, max=14),
            ClarifyField(
                name="language",
                type="enum",
                required=False,
                choices=("en", "zh", "mixed"),
            ),
            ClarifyField(
                name="additional_notes",
                type="string",
                required=False,
                max_chars=2000,
            ),
        ),
        intro="Trip info needed.",
    )
    return MetaPlan(
        name="trip",
        triggers=("plan a trip",),
        priority=0,
        steps=(
            MetaStep(
                id="collect",
                skill="collect",
                kind="user_input",
                clarify_config=cfg,
            ),
            MetaStep(
                id="summary",
                skill="summarize",
                kind="agent",
                depends_on=("collect",),
                with_args={
                    "context": (
                        "destination={{ inputs.collected.collect.destination }} "
                        "days={{ inputs.collected.collect.days }}"
                    ),
                },
            ),
        ),
    )


def _seed_running_run(writer, plan, run_id="r1", session_key="S1"):
    inputs = {"user_message": "I want to plan a trip", "collected": {}}
    snapshot_json = json.dumps(to_jsonable(plan), sort_keys=True, ensure_ascii=False)
    with writer._lock:
        writer._conn.execute(
            "INSERT INTO meta_skill_runs "
            "(run_id, meta_skill_name, meta_skill_digest, plan_snapshot_json, "
            " triggered_by, session_key, status, started_at_ms, inputs_json) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (run_id, plan.name, "d", snapshot_json, "soft_meta_invoke",
             session_key, "running", 0, json.dumps(inputs)),
        )
        writer._conn.commit()
    return inputs


async def _sv(*_a):
    return
    yield  # type: ignore[unreachable]


@pytest.mark.asyncio
async def test_resume_updates_language_instruction_from_clarify_language(writer):
    plan = _plan_with_collect_then_summary()
    inputs = _seed_running_run(writer, plan)
    inputs["language_instruction"] = (
        "Output language rule: write final user-facing prose, headings, "
        "labels, and summaries in English only unless the user explicitly "
        "asks for another language."
    )

    dispatched_context: dict[str, dict] = {}
    orch = MetaOrchestrator(
        agent_runner=None,  # type: ignore[arg-type]
        skill_loader=None,
        dao=writer,
    )

    async def _dispatch(step, effective_skill, match_inputs, outputs):
        if step.kind == "user_input":
            async for ev in orch._dispatch_one_step(
                step, effective_skill, match_inputs, outputs,
                run_id="r1", session_id="S1",
            ):
                yield ev
            return
        if step.id == "summary":
            dispatched_context["summary"] = {
                "user_language": match_inputs.get("user_language"),
                "language_instruction": match_inputs.get("language_instruction"),
                "inputs_collected": match_inputs.get("collected"),
            }
            yield _StepDone(text="summary-done", status="ok")

    paused = await orch.run_once(
        MetaMatch(plan=plan, inputs=inputs),
        run_id="r1",
        session_id="S1",
        dispatch_step_stream=_dispatch,
        yield_skill_view_preface=_sv,
    )
    assert paused.paused is True

    await orch.resume(
        run_id="r1",
        session_id="S1",
        filled_fields={"destination": "纸盘动物", "days": 14, "language": "mixed"},
        dispatch_step_stream=_dispatch,
        yield_skill_view_preface=_sv,
    )

    summary = dispatched_context["summary"]
    assert summary["inputs_collected"]["collect"]["language"] == "mixed"
    assert summary["user_language"] == "zh"
    assert "English only" not in summary["language_instruction"]
    assert "Simplified Chinese" in summary["language_instruction"]


@pytest.mark.asyncio
async def test_resume_prefers_chinese_when_any_clarify_field_contains_chinese(writer):
    plan = _plan_with_collect_then_summary()
    inputs = _seed_running_run(writer, plan)
    inputs["language_instruction"] = (
        "Output language rule: write final user-facing prose, headings, "
        "labels, and summaries in English only unless the user explicitly "
        "asks for another language."
    )

    dispatched_context: dict[str, dict] = {}
    orch = MetaOrchestrator(
        agent_runner=None,  # type: ignore[arg-type]
        skill_loader=None,
        dao=writer,
    )

    async def _dispatch(step, effective_skill, match_inputs, outputs):
        if step.kind == "user_input":
            async for ev in orch._dispatch_one_step(
                step, effective_skill, match_inputs, outputs,
                run_id="r1", session_id="S1",
            ):
                yield ev
            return
        if step.id == "summary":
            dispatched_context["summary"] = {
                "user_language": match_inputs.get("user_language"),
                "language_instruction": match_inputs.get("language_instruction"),
            }
            yield _StepDone(text="summary-done", status="ok")

    paused = await orch.run_once(
        MetaMatch(plan=plan, inputs=inputs),
        run_id="r1",
        session_id="S1",
        dispatch_step_stream=_dispatch,
        yield_skill_view_preface=_sv,
    )
    assert paused.paused is True

    await orch.resume(
        run_id="r1",
        session_id="S1",
        filled_fields={"destination": "纸盘动物", "days": 14, "language": "en"},
        dispatch_step_stream=_dispatch,
        yield_skill_view_preface=_sv,
    )

    summary = dispatched_context["summary"]
    assert summary["user_language"] == "zh"
    assert "English only" not in summary["language_instruction"]
    assert "Simplified Chinese" in summary["language_instruction"]


@pytest.mark.asyncio
async def test_empty_form_resume_lets_llm_infer_all_clarify_fields(writer):
    plan = _plan_with_collect_then_summary()
    inputs = _seed_running_run(writer, plan)
    inputs["user_message"] = "帮我给孩子安排一个北京三日游。"

    async def fake_chat(_system: str, user: str) -> str:
        payload = json.loads(user)
        target_names = {
            field["name"] for field in payload["fields_to_infer"]
        }
        assert target_names == {
            "destination", "days", "language", "additional_notes",
        }
        return json.dumps(
            {
                "destination": "北京",
                "days": 3,
                "language": "zh",
                "additional_notes": "按亲子节奏安排，不要太赶。",
            },
            ensure_ascii=False,
        )

    dispatched_context: dict[str, dict] = {}
    orch = MetaOrchestrator(
        agent_runner=None,  # type: ignore[arg-type]
        skill_loader=None,
        dao=writer,
        llm_chat=fake_chat,
    )

    async def _dispatch(step, effective_skill, match_inputs, outputs):
        if step.kind == "user_input":
            async for ev in orch._dispatch_one_step(
                step, effective_skill, match_inputs, outputs,
                run_id="r1", session_id="S1",
            ):
                yield ev
            return
        if step.id == "summary":
            dispatched_context["summary"] = {
                "inputs_user_message": match_inputs.get("user_message"),
                "inputs_collected": match_inputs.get("collected"),
            }
            yield _StepDone(text="summary-done", status="ok")

    paused = await orch.run_once(
        MetaMatch(plan=plan, inputs=inputs),
        run_id="r1",
        session_id="S1",
        dispatch_step_stream=_dispatch,
        yield_skill_view_preface=_sv,
    )
    assert paused.paused is True

    await orch.resume(
        run_id="r1",
        session_id="S1",
        filled_fields={},
        dispatch_step_stream=_dispatch,
        yield_skill_view_preface=_sv,
    )

    collected = dispatched_context["summary"]["inputs_collected"]["collect"]
    assert collected == {
        "destination": "北京",
        "days": 3,
        "language": "zh",
        "additional_notes": "按亲子节奏安排，不要太赶。",
    }
    assert "按亲子节奏安排" in dispatched_context["summary"]["inputs_user_message"]


@pytest.mark.asyncio
async def test_pause_then_resume_completes_dag(writer):
    plan = _plan_with_collect_then_summary()
    inputs = _seed_running_run(writer, plan)

    dispatched_context: dict[str, dict] = {}

    orch = MetaOrchestrator(
        agent_runner=None,  # type: ignore[arg-type]
        skill_loader=None,
        dao=writer,
    )

    async def _dispatch(step, effective_skill, match_inputs, outputs):
        if step.kind == "user_input":
            async for ev in orch._dispatch_one_step(
                step, effective_skill, match_inputs, outputs,
                run_id="r1", session_id="S1",
            ):
                yield ev
            return
        if step.id == "summary":
            dispatched_context["summary"] = {
                "context_arg": step.with_args.get("context", ""),
                "inputs_user_message": match_inputs.get("user_message"),
                "inputs_collected": match_inputs.get("collected"),
            }
            yield _StepDone(text="summary-done", status="ok")

    match = MetaMatch(plan=plan, inputs=inputs)
    result = await orch.run_once(
        match,
        run_id="r1",
        session_id="S1",
        dispatch_step_stream=_dispatch,
        yield_skill_view_preface=_sv,
    )
    assert isinstance(result, MetaResult)
    assert result.paused is True
    assert result.paused_payload is not None
    assert result.paused_payload.step_id == "collect"

    result2 = await orch.resume(
        run_id="r1",
        session_id="S1",
        filled_fields={"destination": "Tokyo", "days": 5},
        dispatch_step_stream=_dispatch,
        yield_skill_view_preface=_sv,
    )
    assert result2.paused is False
    assert result2.ok is True

    sm = dispatched_context["summary"]
    assert sm["inputs_user_message"] == "I want to plan a trip"
    assert sm["inputs_collected"]["collect"]["destination"] == "Tokyo"
    assert sm["inputs_collected"]["collect"]["days"] == 5


@pytest.mark.asyncio
async def test_resume_injects_additional_notes_into_downstream_user_message(writer):
    plan = _plan_with_collect_then_summary()
    inputs = _seed_running_run(writer, plan)

    dispatched_context: dict[str, dict] = {}

    orch = MetaOrchestrator(
        agent_runner=None,  # type: ignore[arg-type]
        skill_loader=None,
        dao=writer,
    )

    async def _dispatch(step, effective_skill, match_inputs, outputs):
        if step.kind == "user_input":
            async for ev in orch._dispatch_one_step(
                step, effective_skill, match_inputs, outputs,
                run_id="r1", session_id="S1",
            ):
                yield ev
            return
        if step.id == "summary":
            dispatched_context["summary"] = {
                "inputs_user_message": match_inputs.get("user_message"),
                "inputs_collected": match_inputs.get("collected"),
            }
            yield _StepDone(text="summary-done", status="ok")

    paused = await orch.run_once(
        MetaMatch(plan=plan, inputs=inputs),
        run_id="r1",
        session_id="S1",
        dispatch_step_stream=_dispatch,
        yield_skill_view_preface=_sv,
    )
    assert paused.paused is True

    await orch.resume(
        run_id="r1",
        session_id="S1",
        filled_fields={
            "destination": "Tokyo",
            "days": 5,
            "additional_notes": "Please avoid museums; kid wants trains.",
        },
        dispatch_step_stream=_dispatch,
        yield_skill_view_preface=_sv,
    )

    sm = dispatched_context["summary"]
    assert sm["inputs_collected"]["collect"]["additional_notes"] == (
        "Please avoid museums; kid wants trains."
    )
    assert sm["inputs_user_message"].startswith("I want to plan a trip")
    assert "Additional user notes" in sm["inputs_user_message"]
    assert "Please avoid museums; kid wants trains." in sm["inputs_user_message"]


@pytest.mark.asyncio
async def test_short_drama_pause_resume_keeps_draft_and_reread_in_same_run_folder(
    writer: MetaRunWriter,
    tmp_path: Path,
) -> None:
    """Additional notes may change user_message, never the run output path."""

    loader = SkillLoader(
        bundled_dir=BUNDLED,
        snapshot_path=tmp_path / "skills-snapshot.json",
    )
    loader.invalidate_cache()
    spec = loader.get_by_name("meta-short-drama")
    assert spec is not None
    full_plan = parse_meta_plan(spec)
    assert full_plan is not None
    full_steps = {step.id: step for step in full_plan.steps}

    draft_text = "TITLE: Stable folder test\nSHOT 1: same file after resume"
    script_save_draft = replace(
        full_steps["script_save_draft"],
        depends_on=(),
        tool_args={
            **full_steps["script_save_draft"].tool_args,
            "content": draft_text,
        },
    )
    review_cfg = full_steps["review_gate"].clarify_config
    assert review_cfg is not None
    review_gate = replace(
        full_steps["review_gate"],
        depends_on=("script_save_draft",),
        clarify_config=replace(
            review_cfg,
            intro="Review the saved draft.",
            intro_by_language={},
            nl_extract=False,
        ),
    )
    script_reread = replace(
        full_steps["script_reread"],
        depends_on=("review_gate", "script_save_draft"),
    )
    plan = replace(
        full_plan,
        steps=(script_save_draft, review_gate, script_reread),
        final_text_mode="raw",
        output_contract={},
    )

    observed: dict[str, object] = {}

    async def unused_agent_runner(_system: str, _user: str):
        raise AssertionError("trimmed regression plan must not spawn an agent")
        yield  # type: ignore[unreachable]

    async def tool_invoker(tool: str, args: dict[str, object]) -> str:
        assert tool == "write_file"
        path = Path(str(args["path"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(args["content"]), encoding="utf-8")
        observed["draft_path"] = path
        return str(path)

    workspace = tmp_path / "workspace"
    orch = MetaOrchestrator(
        agent_runner=unused_agent_runner,
        skill_loader=loader,
        tool_invoker=tool_invoker,
        workspace_dir=str(workspace),
        run_writer=writer,
        session_key="S1",
    )
    inputs = {
        "user_message": "生成一个单镜头短剧",
        # Reserved runtime inputs must not be caller-controlled.
        "meta_run_id": "../../caller-controlled",
    }

    paused_result: MetaResult | None = None
    async for event in orch.iter_events(MetaMatch(plan=plan, inputs=inputs)):
        if isinstance(event, MetaResult):
            paused_result = event
    assert paused_result is not None and paused_result.paused is True
    assert paused_result.paused_payload is not None
    run_id = paused_result.paused_payload.run_id

    record = writer.get_run(run_id)
    assert record is not None
    persisted_inputs = json.loads(record.inputs_json)
    stable_id = persisted_inputs["meta_run_id"]
    assert stable_id == inputs["meta_run_id"]
    assert stable_id.startswith("run-")
    assert "/" not in stable_id and "\\" not in stable_id

    draft_path = observed["draft_path"]
    assert isinstance(draft_path, Path)
    assert draft_path == workspace / "meta_short_drama" / stable_id / "script.txt"

    async def resume_dispatch(step, effective_skill, match_inputs, outputs):
        if step.id == "script_reread":
            rendered = render_with_args(
                step.with_args,
                inputs=match_inputs,
                outputs=outputs,
            )
            observed["reread_path"] = Path(str(rendered["input"]))
            observed["resumed_meta_run_id"] = match_inputs["meta_run_id"]
            observed["resumed_user_message"] = match_inputs["user_message"]
        async for event in orch._dispatch_step_stream(
            step,
            effective_skill,
            match_inputs,
            outputs,
        ):
            yield event

    resumed = await orch.resume(
        run_id=run_id,
        session_id="S1",
        filled_fields={
            "review": "继续",
            "additional_notes": "让结尾更温暖。",
        },
        dispatch_step_stream=resume_dispatch,
        yield_skill_view_preface=orch._yield_skill_view_preface,
    )

    assert resumed.ok is True
    assert observed["reread_path"] == draft_path
    assert observed["resumed_meta_run_id"] == stable_id
    assert "让结尾更温暖" in str(observed["resumed_user_message"])
    assert resumed.step_outputs["script_reread"] == draft_text


def _short_drama_consent_e2e_plan(loader: SkillLoader) -> MetaPlan:
    """Trim the real manifest to its two review gates plus one paid probe."""

    spec = loader.get_by_name("meta-short-drama")
    assert spec is not None
    full_plan = parse_meta_plan(spec)
    assert full_plan is not None
    full_steps = {step.id: step for step in full_plan.steps}
    review_cfg = full_steps["review_gate"].clarify_config
    assert review_cfg is not None
    return replace(
        full_plan,
        steps=(
            replace(
                full_steps["script_draft"],
                depends_on=(),
                with_args={},
            ),
            replace(
                full_steps["review_gate"],
                depends_on=("script_draft",),
                clarify_config=replace(
                    review_cfg,
                    intro="Review the draft.",
                    intro_by_language={},
                ),
            ),
            replace(full_steps["review_intent"], depends_on=("review_gate",)),
            replace(
                full_steps["script_reread"],
                depends_on=("review_gate",),
                with_args={},
            ),
            replace(
                full_steps["script_revised"],
                depends_on=("review_intent", "script_reread"),
                with_args={},
            ),
            full_steps["revision_confirm_gate"],
            replace(
                full_steps["review_normalize"],
                depends_on=("review_intent", "revision_confirm_gate"),
            ),
            replace(
                full_steps["final_script"],
                depends_on=(
                    "review_normalize",
                    "script_reread",
                    "script_revised",
                ),
            ),
            replace(
                full_steps["reference_image"],
                depends_on=("review_normalize", "final_script"),
                with_args={},
            ),
        ),
        final_text_mode="raw",
        output_contract={},
    )


def _short_drama_consent_e2e_harness(
    writer: MetaRunWriter,
    tmp_path: Path,
    *,
    run_id: str,
):
    loader = SkillLoader(
        bundled_dir=BUNDLED,
        snapshot_path=tmp_path / f"{run_id}-skills-snapshot.json",
    )
    loader.invalidate_cache()
    plan = _short_drama_consent_e2e_plan(loader)
    inputs = _seed_running_run(writer, plan, run_id=run_id)
    paid_calls: list[str] = []
    orch = MetaOrchestrator(
        agent_runner=None,  # type: ignore[arg-type]
        skill_loader=loader,
        dao=writer,
        workspace_dir=str(tmp_path / "workspace"),
        session_key="S1",
    )

    draft_script = (
        "=== OVERVIEW ===\n"
        "DURATION_S: 8\n"
        "N_SHOTS: 2\n"
        "=== SHOT_1 ===\nDURATION_S: 4\n"
        "=== SHOT_2 ===\nDURATION_S: 4\n"
    )

    async def dispatch(step, effective_skill, match_inputs, outputs):
        if step.kind == "user_input":
            async for event in orch._dispatch_one_step(
                step,
                effective_skill,
                match_inputs,
                outputs,
                run_id=run_id,
                session_id="S1",
            ):
                yield event
            return
        if step.id in {"script_draft", "script_reread"}:
            yield _StepDone(text=draft_script)
            return
        if step.id in {"review_intent", "review_normalize", "final_script"}:
            async for event in orch._dispatch_step_stream(
                step,
                effective_skill,
                match_inputs,
                outputs,
            ):
                yield event
            return
        if step.id == "script_revised":
            yield _StepDone(
                text=(
                    "=== OVERVIEW ===\n"
                    "DURATION_S: 12\n"
                    "N_SHOTS: 3\n"
                    "=== SHOT_1 ===\nDURATION_S: 4\n"
                    "=== SHOT_2 ===\nDURATION_S: 4\n"
                    "=== SHOT_3 ===\nDURATION_S: 4\n"
                ),
            )
            return
        if step.id == "reference_image":
            paid_calls.append(step.id)
            yield _StepDone(text="provider-call-stubbed")

    return plan, inputs, orch, paid_calls, dispatch


@pytest.mark.asyncio
async def test_short_drama_adjustment_pauses_again_before_any_paid_provider_step(
    writer: MetaRunWriter,
    tmp_path: Path,
) -> None:
    """Edit-only reply produces a revised preview; explicit approval unlocks paid work."""

    plan, inputs, orch, paid_calls, dispatch = _short_drama_consent_e2e_harness(
        writer,
        tmp_path,
        run_id="short-drama-consent",
    )

    initial = await orch.run_once(
        MetaMatch(plan=plan, inputs=inputs),
        run_id="short-drama-consent",
        session_id="S1",
        dispatch_step_stream=dispatch,
        yield_skill_view_preface=_sv,
    )
    assert initial.paused is True
    assert initial.paused_payload is not None
    assert initial.paused_payload.step_id == "review_gate"

    after_adjustment = await orch.resume(
        run_id="short-drama-consent",
        session_id="S1",
        filled_fields={"review": "改成 3 个分镜，结尾更温暖"},
        dispatch_step_stream=dispatch,
        yield_skill_view_preface=_sv,
    )

    assert after_adjustment.paused is True
    assert after_adjustment.paused_payload is not None
    assert after_adjustment.paused_payload.step_id == "revision_confirm_gate"
    assert "N_SHOTS: 3" in after_adjustment.paused_payload.schema.intro
    assert "3 个镜头" in after_adjustment.paused_payload.schema.intro
    assert "计费剧情时长 12 秒" in after_adjustment.paused_payload.schema.intro
    assert "USD $2.00-$2.05" in after_adjustment.paused_payload.schema.intro
    assert paid_calls == []
    assert "DECISION: revise" in after_adjustment.step_outputs["review_intent"]

    after_confirmation = await orch.resume(
        run_id="short-drama-consent",
        session_id="S1",
        filled_fields={"review": "继续生成"},
        dispatch_step_stream=dispatch,
        yield_skill_view_preface=_sv,
    )

    assert after_confirmation.ok is True
    assert after_confirmation.paused is False
    assert paid_calls == ["reference_image"]
    assert "DECISION: proceed" in after_confirmation.step_outputs["review_normalize"]
    assert "explicit_approval_after_revision" in (
        after_confirmation.step_outputs["review_normalize"]
    )


@pytest.mark.asyncio
async def test_short_drama_direct_explicit_approval_keeps_single_pause_path(
    writer: MetaRunWriter,
    tmp_path: Path,
) -> None:
    plan, inputs, orch, paid_calls, dispatch = _short_drama_consent_e2e_harness(
        writer,
        tmp_path,
        run_id="short-drama-direct-approval",
    )
    initial = await orch.run_once(
        MetaMatch(plan=plan, inputs=inputs),
        run_id="short-drama-direct-approval",
        session_id="S1",
        dispatch_step_stream=dispatch,
        yield_skill_view_preface=_sv,
    )
    assert initial.paused is True
    assert initial.paused_payload is not None
    assert initial.paused_payload.step_id == "review_gate"

    approved = await orch.resume(
        run_id="short-drama-direct-approval",
        session_id="S1",
        filled_fields={"review": "继续生成"},
        dispatch_step_stream=dispatch,
        yield_skill_view_preface=_sv,
    )

    assert approved.ok is True
    assert approved.paused is False
    assert approved.step_outputs["revision_confirm_gate"] == ""
    assert paid_calls == ["reference_image"]
    assert "CONSENT_BASIS: explicit_approval" in approved.step_outputs[
        "review_normalize"
    ]


@pytest.mark.asyncio
async def test_short_drama_hold_keeps_canonical_snapshot_without_paid_calls(
    writer: MetaRunWriter,
    tmp_path: Path,
) -> None:
    plan, inputs, orch, paid_calls, dispatch = _short_drama_consent_e2e_harness(
        writer,
        tmp_path,
        run_id="short-drama-hold",
    )
    initial = await orch.run_once(
        MetaMatch(plan=plan, inputs=inputs),
        run_id="short-drama-hold",
        session_id="S1",
        dispatch_step_stream=dispatch,
        yield_skill_view_preface=_sv,
    )
    assert initial.paused is True

    held = await orch.resume(
        run_id="short-drama-hold",
        session_id="S1",
        filled_fields={"review": "谢谢"},
        dispatch_step_stream=dispatch,
        yield_skill_view_preface=_sv,
    )

    assert held.ok is True
    assert held.paused is False
    assert paid_calls == []
    assert "DECISION: hold" in held.step_outputs["review_normalize"]
    assert held.step_outputs["final_script"] == held.step_outputs["script_reread"].rstrip()


@pytest.mark.asyncio
async def test_short_drama_cancel_after_revised_preview_keeps_paid_calls_at_zero(
    writer: MetaRunWriter,
    tmp_path: Path,
) -> None:
    plan, inputs, orch, paid_calls, dispatch = _short_drama_consent_e2e_harness(
        writer,
        tmp_path,
        run_id="short-drama-cancel",
    )

    initial = await orch.run_once(
        MetaMatch(plan=plan, inputs=inputs),
        run_id="short-drama-cancel",
        session_id="S1",
        dispatch_step_stream=dispatch,
        yield_skill_view_preface=_sv,
    )
    assert initial.paused is True

    after_adjustment = await orch.resume(
        run_id="short-drama-cancel",
        session_id="S1",
        filled_fields={"review": "风格改成水墨"},
        dispatch_step_stream=dispatch,
        yield_skill_view_preface=_sv,
    )
    assert after_adjustment.paused is True
    assert paid_calls == []

    after_cancel = await orch.resume(
        run_id="short-drama-cancel",
        session_id="S1",
        filled_fields={"review": "取消"},
        dispatch_step_stream=dispatch,
        yield_skill_view_preface=_sv,
    )

    assert after_cancel.ok is True
    assert after_cancel.paused is False
    assert paid_calls == []
    assert "DECISION: cancel" in after_cancel.step_outputs["review_normalize"]


@pytest.mark.asyncio
async def test_non_persistent_orchestrator_seeds_safe_runtime_run_id() -> None:
    observed: dict[str, object] = {}
    plan = MetaPlan(
        name="non-persistent-stable-id",
        triggers=("test",),
        priority=0,
        steps=(
            MetaStep(
                id="capture",
                skill="",
                kind="tool_call",
                tool="capture",
                tool_allowlist=("capture",),
                tool_args={"meta_run_id": "{{ inputs.meta_run_id }}"},
            ),
        ),
        final_text_mode="raw",
    )

    async def unused_agent_runner(_system: str, _user: str):
        raise AssertionError("tool-call-only plan must not spawn an agent")
        yield  # type: ignore[unreachable]

    async def tool_invoker(tool: str, args: dict[str, object]) -> str:
        assert tool == "capture"
        observed.update(args)
        return "captured"

    inputs = {"user_message": "test", "meta_run_id": "../../unsafe"}
    result = await MetaOrchestrator(
        agent_runner=unused_agent_runner,
        skill_loader=None,
        tool_invoker=tool_invoker,
    ).run(MetaMatch(plan=plan, inputs=inputs))

    assert result.ok is True
    stable_id = str(observed["meta_run_id"])
    assert stable_id == inputs["meta_run_id"]
    assert stable_id.startswith("run-")
    assert "/" not in stable_id and "\\" not in stable_id


@pytest.mark.asyncio
async def test_failed_replay_seeds_success_and_reads_original_artifact_directory(
    tmp_path: Path,
    writer: MetaRunWriter,
) -> None:
    artifact_run_id = "run-original-short-drama-artifacts"
    workspace = tmp_path / "workspace"
    script_path = workspace / "meta_short_drama" / artifact_run_id / "script.txt"
    script_path.parent.mkdir(parents=True)
    script_path.write_text("persisted short-drama script", encoding="utf-8")
    observed: dict[str, object] = {}

    plan = MetaPlan(
        name="meta-short-drama-replay",
        triggers=(),
        priority=0,
        steps=(
            MetaStep(
                id="script_save",
                skill="",
                kind="tool_call",
                tool="must_not_run",
                tool_allowlist=("must_not_run",),
            ),
            MetaStep(
                id="script_reread",
                skill="",
                kind="tool_call",
                tool="read_original_script",
                tool_allowlist=("read_original_script",),
                tool_args={
                    "input": (
                        "{{ inputs.workspace_dir }}/meta_short_drama/"
                        "{{ inputs.meta_run_id }}/script.txt"
                    ),
                },
                depends_on=("script_save",),
            ),
        ),
        final_text_mode="raw",
    )

    async def unused_agent_runner(_system: str, _user: str):
        raise AssertionError("tool-call-only replay must not spawn an agent")
        yield  # type: ignore[unreachable]

    async def tool_invoker(tool: str, args: dict[str, object]) -> str:
        assert tool == "read_original_script"
        path = Path(str(args["input"]))
        observed["path"] = path
        return path.read_text(encoding="utf-8")

    inputs = {
        "user_message": "retry the failed delivery step",
        # The new invocation must never redirect a replay's artifact directory.
        "meta_run_id": "caller-spoofed-safe-id",
    }
    orchestrator = MetaOrchestrator(
        agent_runner=unused_agent_runner,
        skill_loader=None,
        tool_invoker=tool_invoker,
        workspace_dir=str(workspace),
        run_writer=writer,
        session_key="replay-session",
    )
    final: MetaResult | None = None
    async for event in orchestrator.iter_events(
        MetaMatch(plan=plan, inputs=inputs),
        seed_outputs={"script_save": str(script_path)},
        trusted_preflight_replay=True,
        trusted_replay_meta_run_id=artifact_run_id,
    ):
        if isinstance(event, MetaResult):
            final = event

    assert final is not None and final.ok is True
    assert final.step_outputs["script_save"] == str(script_path)
    assert final.step_outputs["script_reread"] == "persisted short-drama script"
    assert observed["path"] == script_path
    assert inputs["meta_run_id"] == artifact_run_id

    replay_record = writer.hydrate_runs(writer.list_runs(limit=1))[0]
    assert json.loads(replay_record.inputs_json)["meta_run_id"] == artifact_run_id
    replay_steps = {step.step_id: step for step in replay_record.steps}
    assert replay_steps["script_save"].status == "ok"
    assert replay_steps["script_reread"].status == "ok"


@pytest.mark.asyncio
async def test_resume_finalizes_run_to_ok_status(writer):
    plan = _plan_with_collect_then_summary()
    inputs = _seed_running_run(writer, plan)

    orch = MetaOrchestrator(
        agent_runner=None, skill_loader=None, dao=writer,  # type: ignore[arg-type]
    )

    async def _dispatch(step, effective_skill, match_inputs, outputs):
        if step.kind == "user_input":
            async for ev in orch._dispatch_one_step(
                step, effective_skill, match_inputs, outputs,
                run_id="r1", session_id="S1",
            ):
                yield ev
            return
        if step.id == "summary":
            yield _StepDone(text="summary-done", status="ok")

    await orch.run_once(
        MetaMatch(plan=plan, inputs=inputs),
        run_id="r1", session_id="S1",
        dispatch_step_stream=_dispatch, yield_skill_view_preface=_sv,
    )
    final = await orch.resume(
        run_id="r1", session_id="S1",
        filled_fields={"destination": "Tokyo", "days": 5},
        dispatch_step_stream=_dispatch, yield_skill_view_preface=_sv,
    )
    assert final.ok is True

    with writer._lock:
        row = writer._conn.execute(
            "SELECT status, ended_at_ms FROM meta_skill_runs WHERE run_id='r1'",
        ).fetchone()
    assert row["status"] == "ok"
    assert row["ended_at_ms"] is not None and row["ended_at_ms"] > 0


@pytest.mark.asyncio
async def test_resume_rejects_unknown_run(writer):
    orch = MetaOrchestrator(
        agent_runner=None, skill_loader=None, dao=writer,  # type: ignore[arg-type]
    )
    result = await orch.resume(
        run_id="does-not-exist",
        session_id="S1",
        filled_fields={},
        dispatch_step_stream=None,
        yield_skill_view_preface=None,
    )
    assert result.ok is False
    assert result.paused is False
    assert result.error is not None
    assert "not found" in result.error.lower() or "race" in result.error.lower()


@pytest.mark.asyncio
async def test_resume_writes_clarify_summary_into_outputs(writer):
    plan = _plan_with_collect_then_summary()
    inputs = _seed_running_run(writer, plan)

    observed_outputs: dict[str, dict] = {}

    orch = MetaOrchestrator(
        agent_runner=None, skill_loader=None, dao=writer,  # type: ignore[arg-type]
    )

    async def _dispatch(step, effective_skill, match_inputs, outputs):
        if step.kind == "user_input":
            async for ev in orch._dispatch_one_step(
                step, effective_skill, match_inputs, outputs,
                run_id="r1", session_id="S1",
            ):
                yield ev
            return
        if step.id == "summary":
            observed_outputs["summary"] = dict(outputs)
            yield _StepDone(text="ok", status="ok")

    paused = await orch.run_once(
        MetaMatch(plan=plan, inputs=inputs),
        run_id="r1", session_id="S1",
        dispatch_step_stream=_dispatch, yield_skill_view_preface=_sv,
    )
    assert paused.paused is True, "test fixture expected pause before resume"
    await orch.resume(
        run_id="r1", session_id="S1",
        filled_fields={"destination": "Tokyo", "days": 5},
        dispatch_step_stream=_dispatch, yield_skill_view_preface=_sv,
    )

    summary_outputs = observed_outputs["summary"]
    collect_md = summary_outputs.get("collect", "")
    assert "destination: Tokyo (from user)" in collect_md
    assert "days: 5 (from user)" in collect_md


@pytest.mark.asyncio
async def test_resume_persists_followup_step_lifecycle_and_usage(writer):
    from opensquilla.engine.usage import UsageTracker
    from opensquilla.persistence.meta_run_writer import summarize_run_record

    plan = _plan_with_collect_then_summary()
    inputs = _seed_running_run(writer, plan)
    tracker = UsageTracker()
    writer.begin_step_sync(
        run_id="r1",
        step=plan.steps[0],
        effective_skill="collect",
        rendered_inputs={},
    )

    orch = MetaOrchestrator(
        agent_runner=None,  # type: ignore[arg-type]
        skill_loader=None,
        dao=writer,
        usage_tracker=tracker,
        session_key="S1",
    )

    async def _dispatch(step, effective_skill, match_inputs, outputs):
        if step.kind == "user_input":
            async for ev in orch._dispatch_one_step(
                step, effective_skill, match_inputs, outputs,
                run_id="r1", session_id="S1",
            ):
                yield ev
            return
        if step.id == "summary":
            tracker.add(
                "S1",
                input_tokens=21,
                output_tokens=9,
                model_id="resume-model",
                billed_cost=0.021,
            )
            yield _StepDone(text="summary-done", status="ok")

    paused = await orch.run_once(
        MetaMatch(plan=plan, inputs=inputs),
        run_id="r1",
        session_id="S1",
        dispatch_step_stream=_dispatch,
        yield_skill_view_preface=_sv,
    )
    assert paused.paused is True, "test fixture expected pause before resume"

    final = await orch.resume(
        run_id="r1",
        session_id="S1",
        filled_fields={"destination": "Tokyo", "days": 5},
        dispatch_step_stream=_dispatch,
        yield_skill_view_preface=_sv,
    )

    assert final.ok is True
    record = writer.get_run("r1")
    assert record is not None
    raw_steps = {step.step_id: step for step in record.steps}
    assert raw_steps["collect"].status == "ok"
    assert "destination: Tokyo" in (raw_steps["collect"].output_text or "")
    summary = summarize_run_record(record)
    by_step = {step["step_id"]: step for step in summary["steps"]}
    assert by_step["collect"]["status"] == "ok"
    assert by_step["summary"]["status"] == "ok"
    assert by_step["summary"]["usage"]["available"] is True
    assert by_step["summary"]["usage"]["input_tokens"] == 21
    assert by_step["summary"]["usage"]["model"] == "resume-model"
