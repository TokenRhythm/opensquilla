"""End-to-end: creator pipeline with stubbed LLMs produces a valid proposal."""

from __future__ import annotations

import json
from pathlib import Path

# creator_fixtures is on sys.path via tests/test_skills/conftest.py
from creator_fixtures import INTENT_PDF_DIGEST, INTENT_TRIP_PLANNER, synth_decision_log

from opensquilla.engine.types import TextDeltaEvent
from opensquilla.skills.creator.lint_runtime import lint_meta_skill
from opensquilla.skills.loader import SkillLoader
from opensquilla.skills.meta.orchestrator import MetaOrchestrator
from opensquilla.skills.meta.parser import parse_meta_plan
from opensquilla.skills.meta.types import MetaMatch, MetaResult
from opensquilla.skills.proposals_lib import write_proposal

REPO = Path(__file__).resolve().parents[2]
_BUNDLED_BASE = REPO / "src" / "opensquilla" / "skills" / "bundled"
BUNDLED = _BUNDLED_BASE


def test_creator_catalog_excludes_outer_creator_helper_skills() -> None:
    """The generated candidate DAG must not be allowed to call creator gates.

    Gate, judge, and proposal persistence steps belong to meta-skill-creator
    itself. If those helper skills leak into the slot-filling catalog, the LLM
    can put proposal persistence inside the candidate meta-skill and runtime
    E2E will correctly fail.
    """
    from opensquilla.skills.creator import proposer

    catalog = proposer._build_catalog_summary()

    assert "history-explorer" in catalog
    assert "docx" in catalog
    assert "ai-video-script" not in catalog
    assert "skill-creator-proposals" not in catalog
    assert "skill-creator-linter" not in catalog
    assert "skill-creator-smoke-test" not in catalog
    assert "meta-skill-creator" not in catalog


def test_e2e_p1_proposal_lint_pass(tmp_path, monkeypatch) -> None:
    """Stub each LLM step + run the full pipeline; verify proposal is
    auto_enable_eligible."""
    home = tmp_path / ".opensquilla"
    log_dir = home / "logs"
    synth_decision_log(log_dir, INTENT_PDF_DIGEST["co_occurrence_seed"])

    from opensquilla.skills.creator import proposer

    canned_slots = {
        "name": "synth-pdf-digest-pipeline",
        "description": "Synthetic PDF digest: extract then create two reviewed briefs.",
        "meta_priority": 50,
        "triggers": ["synth pdf digest"],
        "steps": [
            {"id": "extract", "skill": "pdf-toolkit", "task": "extract", "with_keys": {}},
            {"id": "digest", "skill": "docx", "task": "docx", "with_keys": {}},
            {"id": "save", "skill": "pdf-toolkit", "task": "persist", "with_keys": {}},
        ],
    }
    monkeypatch.setattr(
        proposer, "_call_llm_for_slots", lambda prompt, **_: json.dumps(canned_slots),
    )

    skill_md = proposer.meta_skill_assemble("p1_sequential", json.dumps(canned_slots))
    assert "synth-pdf-digest-pipeline" in skill_md

    lint_result = lint_meta_skill(skill_md)
    assert lint_result["G1"]["passed"]
    assert lint_result["G2"]["passed"]

    smoke_result = proposer.run_smoke_gates(
        skill_md=skill_md,
        fixture_gen_fn=lambda md, kind: {
            "positive": "please use synth pdf digest now",
            "negative": "tell me a joke unrelated",
        }[kind],
        classifier_model="stub",
    )
    assert smoke_result["G3"]["passed"]
    assert smoke_result["G4"]["passed"]
    # ``classifier_model="stub"`` makes ``run_smoke_gates`` flag the
    # result as degraded — no cross-vendor classification actually ran,
    # so G3/G4 pass by stub-fixture construction only.
    assert smoke_result.get("degraded") is True

    persist = write_proposal(
        home,
        skill_md,
        lint_result,
        smoke_result,
    )
    # D1: degraded smoke must NOT yield ``auto_enable_eligible``. The
    # proposal still persists (operators can review it on disk), but
    # the unattended creator pipeline cannot promote a candidate that
    # was never validated against a real classifier model.
    assert persist["auto_enable_eligible"] is False

    proposal_dir = home / "proposals" / persist["proposal_id"]
    assert (proposal_dir / "SKILL.md").is_file()
    assert (proposal_dir / "gates.json").is_file()
    gates_payload = json.loads((proposal_dir / "gates.json").read_text())
    assert gates_payload["smoke"].get("degraded") is True
    assert gates_payload["auto_enable_eligible"] is False


def test_creator_preserves_required_triggers_and_prior_step_context(monkeypatch) -> None:
    """Creator output must keep explicit trigger requirements and complete
    the evidence chain for sequential templates."""
    from opensquilla.skills.creator import proposer

    canned_slots = {
        "name": "traceback-debug-orchestrator",
        "description": (
            "Diagnose traceback root causes by chaining history, diff, and summary."
        ),
        "meta_priority": 55,
        "triggers": [
            "diagnose this traceback",
            "debug this stack trace with history and diff",
        ],
        "steps": [
            {
                "id": "history_scan",
                "skill": "history-explorer",
                "task": "Find related traceback history",
                "with_keys": {},
            },
            {
                "id": "diff_capture",
                "skill": "github",
                "task": "Capture current diff",
                "with_keys": {},
            },
            {
                "id": "synthesize_report",
                "skill": "docx",
                "task": "Produce a Chinese root-cause report from all evidence",
                "with_keys": {},
            },
        ],
    }
    monkeypatch.setattr(
        proposer,
        "_call_llm_for_slots",
        lambda prompt, **_: json.dumps(canned_slots),
    )

    slots_json = proposer.meta_skill_fill_slots(
        "p1_sequential",
        history_summary="history-explorer -> github -> docx freq=5",
        user_intent=(
            "请创建中文 traceback 根因诊断 meta-skill。"
            "触发短语要包含：诊断 traceback、traceback 根因、stack trace root cause。"
        ),
    )

    slots = json.loads(slots_json)
    assert slots["triggers"][:3] == [
        "诊断 traceback",
        "traceback 根因",
        "stack trace root cause",
    ]

    skill_md = proposer.meta_skill_assemble("p1_sequential", slots_json)
    assert "kind: skill_exec\n      skill: \"history-explorer\"" in skill_md
    assert 'skill: "github"' in skill_md
    assert "kind: skill_exec\n      skill: \"docx\"" in skill_md
    assert "outputs.history_scan" in skill_md
    assert "outputs.diff_capture" in skill_md


def test_creator_dag_passes_raw_user_request_to_slot_filling(tmp_path) -> None:
    """Slot filling must see raw user requirements, not only clarification
    summaries, so hard constraints compete fairly with the baseline gate."""
    loader = SkillLoader(bundled_dir=BUNDLED, snapshot_path=tmp_path / "snap.json")
    loader.invalidate_cache()
    creator_spec = loader.get_by_name("meta-skill-creator")
    assert creator_spec is not None
    plan = parse_meta_plan(creator_spec)
    assert plan is not None

    fill_slots = {step.id: step for step in plan.steps}["fill_slots"]
    user_intent = str(fill_slots.tool_args["user_intent"])
    assert "inputs.user_message" in user_intent
    assert "outputs.clarify_intent" in user_intent


def test_creator_runtime_e2e_uses_candidate_trigger_prompt(tmp_path) -> None:
    """Runtime E2E should exercise the candidate skill, not the outer creator
    request that produced it."""
    loader = SkillLoader(bundled_dir=BUNDLED, snapshot_path=tmp_path / "snap.json")
    loader.invalidate_cache()
    creator_spec = loader.get_by_name("meta-skill-creator")
    assert creator_spec is not None
    plan = parse_meta_plan(creator_spec)
    assert plan is not None

    runtime_e2e = {step.id: step for step in plan.steps}["runtime_e2e"]
    assert runtime_e2e.tool_args["skill_md"] == "{{ outputs.assemble }}"
    assert runtime_e2e.tool_args["eval_prompts"] == ""


def test_manual_creator_persist_auto_enables_when_setting_is_on(tmp_path) -> None:
    """The manual meta-skill-creator persist tool should use the same
    conservative auto-enable path as cron/dream auto-propose when the
    operator has enabled it in runtime settings."""
    home = tmp_path / ".opensquilla"

    from opensquilla.skills import proposals_lib
    from opensquilla.skills.creator import proposer

    proposals_lib.write_auto_propose_settings(
        home,
        {"auto_enable": True, "auto_enable_max_risk": "medium"},
    )
    skill_md = """---
name: synth-manual-auto-enable
description: "Manual creator output that is safe to auto-enable."
kind: meta
meta_priority: 50
triggers:
  - "manual auto enable"
composition:
  steps:
    - id: explore
      skill: history-explorer
      with:
        query: "{{ inputs.user_message | xml_escape | truncate(512) }}"
    - id: digest
      skill: docx
      depends_on: [explore]
      with:
        text: "{{ outputs.explore | truncate(2000) }}"
---
"""
    lint_result = {"G1": {"passed": True}, "G2": {"passed": True}}
    smoke_result = {"G3": {"passed": True}, "G4": {"passed": True}}

    out = json.loads(proposer.meta_skill_persist_proposal(
        skill_md,
        json.dumps(lint_result),
        json.dumps(smoke_result),
        home=str(home),
    ))

    assert out["status"] == "ok"
    assert out["auto_enable"]["status"] == "enabled"
    assert out["auto_enable"]["triggered_by"] == "manual"
    assert not (home / "proposals" / out["proposal_id"]).exists()
    assert (home / "skills" / "synth-manual-auto-enable" / "SKILL.md").is_file()


def test_auto_propose_persist_can_defer_manual_auto_enable(tmp_path) -> None:
    """Cron/dream auto-propose must own provenance and auto-enable decisions.

    The persist tool supports manual auto-enable for user-active creator runs,
    but auto-propose injects ``auto_enable_manual=False`` so it can patch
    auto_cron/auto_dream provenance before attempting promotion.
    """
    home = tmp_path / ".opensquilla"

    from opensquilla.skills import proposals_lib
    from opensquilla.skills.creator import proposer

    proposals_lib.write_auto_propose_settings(
        home,
        {"auto_enable": True, "auto_enable_max_risk": "medium"},
    )
    skill_md = """---
name: synth-deferred-auto-enable
description: "Safe creator output whose promotion is deferred to auto_propose."
kind: meta
meta_priority: 50
triggers:
  - "deferred auto enable"
composition:
  steps:
    - id: explore
      skill: history-explorer
      with:
        query: "{{ inputs.user_message | xml_escape | truncate(512) }}"
    - id: digest
      skill: docx
      depends_on: [explore]
      with:
        text: "{{ outputs.explore | truncate(2000) }}"
---
"""
    lint_result = {"G1": {"passed": True}, "G2": {"passed": True}}
    smoke_result = {"G3": {"passed": True}, "G4": {"passed": True}}

    out = json.loads(proposer.meta_skill_persist_proposal(
        skill_md,
        json.dumps(lint_result),
        json.dumps(smoke_result),
        home=str(home),
        auto_enable_manual=False,
    ))

    assert out["status"] == "ok"
    assert "auto_enable" not in out
    assert (home / "proposals" / out["proposal_id"] / "SKILL.md").is_file()
    assert not (home / "skills" / "synth-deferred-auto-enable").exists()


async def test_orchestrator_drives_creator_dag_end_to_end(tmp_path, monkeypatch) -> None:
    """Full DAG through MetaOrchestrator with stubbed downstream runners."""
    home = tmp_path / ".opensquilla"
    log_dir = home / "logs"
    synth_decision_log(log_dir, INTENT_PDF_DIGEST["co_occurrence_seed"])
    monkeypatch.setenv("OPENSQUILLA_LOG_DIR", str(log_dir))

    loader = SkillLoader(bundled_dir=BUNDLED, snapshot_path=tmp_path / "snap.json")
    loader.invalidate_cache()
    creator_spec = loader.get_by_name("meta-skill-creator")
    assert creator_spec is not None, "meta-skill-creator not loaded; check Task 6"
    plan = parse_meta_plan(creator_spec)
    assert plan is not None

    async def stub_agent_runner(system_prompt: str, user_prompt: str):
        if "Clarify whether the user wants a meta-skill" in user_prompt:
            yield TextDeltaEvent(text=(
                "Route: Meta-Skill\n"
                "WORKFLOW_GOAL: compose X then Y\n"
                "OUTPUT_SHAPE: SKILL.md proposal\n"
                "TRIGGERS: orch e2e trigger\n"
                "HUMAN_PREFERENCE_BRANCH: no\n"
                "NEEDS_CLARIFICATION: no\n"
                "MISSING_FIELDS:\n"
                "  - none\n"
                "CLARIFY_REASON: none"
            ))
            return
        yield TextDeltaEvent(text="<stub:agent>")

    async def stub_llm_chat(system_prompt: str, user_prompt: str) -> str:
        if "Clarify whether the user wants a meta-skill" in user_prompt:
            return (
                "Route: Meta-Skill\n"
                "WORKFLOW_GOAL: compose X then Y\n"
                "OUTPUT_SHAPE: SKILL.md proposal\n"
                "TRIGGERS: orch e2e trigger\n"
                "HUMAN_PREFERENCE_BRANCH: no\n"
                "NEEDS_CLARIFICATION: no\n"
                "MISSING_FIELDS:\n"
                "  - none\n"
                "CLARIFY_REASON: none"
            )
        return "p1_sequential"

    async def stub_tool_invoker(tool_name: str, args: dict) -> str:
        if tool_name == "emit_text":
            return str(args.get("text", ""))
        if tool_name == "meta_skill_fill_slots":
            return json.dumps({
                "name": "synth-orch-e2e", "description": "x" * 50,
                "meta_priority": 50, "triggers": ["orch e2e trigger"],
                "steps": [
                    {"id": "a", "skill": "docx", "task": "t", "with_keys": {}},
                    {"id": "b", "skill": "pdf-toolkit", "task": "t", "with_keys": {}},
                ],
            })
        if tool_name == "meta_skill_assemble":
            from opensquilla.skills.creator.proposer import meta_skill_assemble
            return meta_skill_assemble(args["pattern_id"], args["slots_json"])
        return f"<stub:{tool_name}>"

    orchestrator = MetaOrchestrator(
        agent_runner=stub_agent_runner,
        skill_loader=loader,
        llm_chat=stub_llm_chat,
        tool_invoker=stub_tool_invoker,
    )
    match = MetaMatch(
        plan=plan,
        inputs={
            "user_message": "compose a meta-skill that does X then Y",
            "system_prompt": "Unattended meta-skill auto-propose run.",
        },
    )

    final_result = None
    async for event in orchestrator.iter_events(match):
        if isinstance(event, MetaResult):
            final_result = event

    assert final_result is not None, "orchestrator did not yield a MetaResult"
    assert final_result.ok, f"orchestrator failed: {final_result.error}"
    assert set(final_result.step_outputs.keys()) >= {
        "harvest", "pick_pattern", "fill_slots", "assemble", "lint", "smoke", "persist"
    }
    # harvest now runs as skill_exec (history-explorer has an entrypoint:),
    # so it returns JSON from explore.py rather than a stub agent reply.
    harvest_output = final_result.step_outputs.get("harvest", "")
    assert harvest_output, "harvest step produced no output"
    harvest_json = json.loads(harvest_output)
    assert "co_occurrences" in harvest_json


async def test_creator_dag_stops_when_clarify_routes_normal_skill(tmp_path) -> None:
    """ROUTE: normal-skill must not reach assemble or proposal persistence."""
    loader = SkillLoader(bundled_dir=BUNDLED, snapshot_path=tmp_path / "snap.json")
    loader.invalidate_cache()
    creator_spec = loader.get_by_name("meta-skill-creator")
    assert creator_spec is not None
    plan = parse_meta_plan(creator_spec)
    assert plan is not None

    async def stub_agent_runner(system_prompt: str, user_prompt: str):
        raise AssertionError("normal-skill route should not start creator agents")

    async def stub_llm_chat(system_prompt: str, user_prompt: str) -> str:
        if "Clarify whether the user wants a meta-skill" in user_prompt:
            return (
                "Route: Normal-Skill\n"
                "WORKFLOW_GOAL: create a standalone skill\n"
                "OUTPUT_SHAPE: normal SKILL.md\n"
                "TRIGGERS: standalone helper\n"
                "HUMAN_PREFERENCE_BRANCH: no\n"
                "NEEDS_CLARIFICATION: no\n"
                "MISSING_FIELDS:\n"
                "  - none\n"
                "CLARIFY_REASON: not a meta-skill request"
            )
        raise AssertionError("normal-skill route should not call creator classifiers")

    async def stub_tool_invoker(tool_name: str, args: dict) -> str:
        if tool_name == "emit_text":
            return str(args.get("text", ""))
        raise AssertionError(f"normal-skill route should not call {tool_name}")

    orchestrator = MetaOrchestrator(
        agent_runner=stub_agent_runner,
        skill_loader=loader,
        llm_chat=stub_llm_chat,
        tool_invoker=stub_tool_invoker,
    )
    match = MetaMatch(
        plan=plan,
        inputs={"user_message": "please create a normal standalone skill"},
    )

    final_result = None
    async for event in orchestrator.iter_events(match):
        if isinstance(event, MetaResult):
            final_result = event

    assert final_result is not None
    assert final_result.ok
    assert final_result.step_outputs["clarify_intent"].lower().startswith("route: normal-skill")
    assert final_result.step_outputs["assemble"] == ""
    assert final_result.step_outputs["persist"] == ""
    assert "normal standalone skill request" in final_result.final_text


async def test_orchestrator_p2_fan_out_merge_proposal(tmp_path, monkeypatch) -> None:
    """P2 fan-out-merge topology: two parallel branches + merge step."""
    home = tmp_path / ".opensquilla"
    log_dir = home / "logs"
    synth_decision_log(log_dir, INTENT_TRIP_PLANNER["co_occurrence_seed"])
    monkeypatch.setenv("OPENSQUILLA_LOG_DIR", str(log_dir))

    loader = SkillLoader(bundled_dir=BUNDLED, snapshot_path=tmp_path / "snap.json")
    loader.invalidate_cache()
    creator_spec = loader.get_by_name("meta-skill-creator")
    plan = parse_meta_plan(creator_spec)

    async def stub_agent_runner(system_prompt: str, user_prompt: str):
        if "Clarify whether the user wants a meta-skill" in user_prompt:
            yield TextDeltaEvent(text=(
                "ROUTE: meta-skill\n"
                "WORKFLOW_GOAL: compose trip planning workflow\n"
                "OUTPUT_SHAPE: SKILL.md proposal\n"
                "TRIGGERS: synth p2 trigger\n"
                "HUMAN_PREFERENCE_BRANCH: no\n"
                "NEEDS_CLARIFICATION: no\n"
                "MISSING_FIELDS:\n"
                "  - none\n"
                "CLARIFY_REASON: none"
            ))
            return
        yield TextDeltaEvent(text="<stub:agent>")

    async def stub_llm_chat(system_prompt: str, user_prompt: str) -> str:
        if "Clarify whether the user wants a meta-skill" in user_prompt:
            return (
                "ROUTE: meta-skill\n"
                "WORKFLOW_GOAL: compose trip planning workflow\n"
                "OUTPUT_SHAPE: SKILL.md proposal\n"
                "TRIGGERS: synth p2 trigger\n"
                "HUMAN_PREFERENCE_BRANCH: no\n"
                "NEEDS_CLARIFICATION: no\n"
                "MISSING_FIELDS:\n"
                "  - none\n"
                "CLARIFY_REASON: none"
            )
        return "p2_fan_out_merge"

    async def stub_tool_invoker(tool_name: str, args: dict) -> str:
        if tool_name == "emit_text":
            return str(args.get("text", ""))
        if tool_name == "meta_skill_fill_slots":
            return json.dumps({
                "name": "synth-p2-trip", "description": "x" * 50,
                "meta_priority": 50, "triggers": ["synth p2 trigger"],
                "branches": [
                    {"id": "weather", "skill": "weather", "task": "w", "with_keys": {}},
                    {"id": "poi", "skill": "multi-search-engine", "task": "p", "with_keys": {}},
                ],
                "merge": {"id": "itin", "skill": "docx", "task": "m", "with_keys": {}},
                "tail": None,
            })
        if tool_name == "meta_skill_assemble":
            from opensquilla.skills.creator.proposer import meta_skill_assemble
            return meta_skill_assemble(args["pattern_id"], args["slots_json"])
        return f"<stub:{tool_name}>"

    orchestrator = MetaOrchestrator(
        agent_runner=stub_agent_runner,
        skill_loader=loader,
        llm_chat=stub_llm_chat,
        tool_invoker=stub_tool_invoker,
    )
    match = MetaMatch(
        plan=plan,
        inputs={"user_message": "compose a trip-planner meta-skill"},
    )

    final_result = None
    async for event in orchestrator.iter_events(match):
        if isinstance(event, MetaResult):
            final_result = event

    assert final_result is not None and final_result.ok
    assemble_output = final_result.step_outputs["assemble"]
    assert "depends_on: [weather, poi]" in assemble_output
