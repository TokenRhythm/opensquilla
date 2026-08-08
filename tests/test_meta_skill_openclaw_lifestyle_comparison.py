import asyncio
import json
from pathlib import Path

from opensquilla.skills.loader import SkillLoader
from opensquilla.skills.meta.parser import parse_meta_plan
from opensquilla.skills.meta.templating import evaluate_when
from scripts.compare_meta_skill_openclaw import EndpointResult, JudgeResult, OpenSquillaRunner
from scripts.compare_meta_skill_openclaw_lifestyle import (
    ARCHIVED_COMPATIBILITY_BENCHMARK,
    LIFESTYLE_COMPARISON_CASES,
    OPENCLAW_T3_MODEL,
    _apply_lifestyle_judge_result,
    _compare_results,
    _judge_lifestyle_with_retries,
    _lifestyle_judge_result_is_complete,
    build_lifestyle_rows,
    judge_existing,
    load_openclaw_baseline,
    render_lifestyle_markdown,
    render_lifestyle_prompts_markdown,
    score_response,
)

SELECTED_SKILLS = [
    "meta-kid-project-planner",
]


def test_lifestyle_catalog_covers_selected_meta_skills_without_exclusions(
    tmp_path: Path,
) -> None:
    assert ARCHIVED_COMPATIBILITY_BENCHMARK is True
    assert [case.skill_name for case in LIFESTYLE_COMPARISON_CASES] == SELECTED_SKILLS
    assert {case.case_id for case in LIFESTYLE_COMPARISON_CASES} == {
        "kid_project_balcony_plants",
    }
    assert all(case.scenario == "lifestyle_primary" for case in LIFESTYLE_COMPARISON_CASES)
    assert "meta-paper-write" not in {case.skill_name for case in LIFESTYLE_COMPARISON_CASES}
    assert "meta-skill-creator" not in {case.skill_name for case in LIFESTYLE_COMPARISON_CASES}

    loader = SkillLoader(
        bundled_dir=Path("src/opensquilla/skills/bundled"),
        snapshot_path=tmp_path / "snapshot.json",
    )
    retired = loader.get_by_name("meta-kid-project-planner")
    assert retired is not None
    assert retired.disable_model_invocation is True
    assert retired.name not in {spec.name for spec in loader.list_meta_specs()}


def test_selected_meta_skills_are_grounded_in_clawhub_top100_components() -> None:
    expectations = {
        "meta-kid-project-planner": ["Multi Search Engine", "Weather", "PowerPoint / PPTX"],
    }

    for skill_name in SELECTED_SKILLS:
        raw = Path(f"src/opensquilla/skills/bundled/{skill_name}/SKILL.md").read_text(
            encoding="utf-8"
        )
        assert "clawhub_top100_composition:" in raw
        assert "Top ClawHub Skills" in raw
        for component in expectations[skill_name]:
            assert component in raw


def test_lifestyle_prompts_are_conversational_and_realistic() -> None:
    prompts = [case.prompt for case in LIFESTYLE_COMPARISON_CASES]

    assert all("benchmark:" not in prompt.lower() for prompt in prompts)
    assert all("OpenSquilla" not in prompt and "OpenClaw" not in prompt for prompt in prompts)
    assert any("科学课" in prompt for prompt in prompts)
    assert any("阳台种豆芽" in prompt for prompt in prompts)
    assert all("example.invalid" not in prompt for prompt in prompts)
    assert all("manifest" not in prompt.lower() for prompt in prompts)


def _bundled_meta_plan(skill_name: str, tmp_path: Path):
    loader = SkillLoader(
        bundled_dir=Path("src/opensquilla/skills/bundled"),
        snapshot_path=tmp_path / "snapshot.json",
    )
    spec = loader.get_by_name(skill_name)
    assert spec is not None
    plan = parse_meta_plan(spec)
    assert plan is not None
    return plan


def test_kid_project_planner_does_not_clarify_when_no_fields_missing(
    tmp_path: Path,
) -> None:
    plan = _bundled_meta_plan("meta-kid-project-planner", tmp_path)
    clarify = next(step for step in plan.steps if step.id == "project_clarify")

    preferences = """
TOPIC: balcony mung bean observation
AGE_BAND: EARLY_GRADE
DEADLINE_DAYS: 14
BUDGET_BAND: SHOESTRING
PARENT_SUPERVISION: LIGHT
LANGUAGE: zh
PROJECT_SAFE: yes
UNSAFE_REASON: none
NEEDS_CLARIFICATION: yes
MISSING_FIELDS:
  - none
ASSUMPTIONS:
  - balcony gets half-day sun
"""

    assert evaluate_when(
        clarify.when,
        inputs={},
        outputs={"preferences": preferences},
    ) is False


def test_new_lifestyle_meta_skills_hide_runtime_failures_and_reply_inline(
    tmp_path: Path,
) -> None:
    expectations = {
        "meta-kid-project-planner": {
            "final": "deliver_project_pack",
            "fallbacks": {
                "recall_past_projects": "recall_past_projects_fallback",
                "web_research": "web_research_fallback",
                "weather_check": "weather_check_fallback",
            },
            "required": [
                "printable record sheet",
                "poster-board layout",
                "Weather / light adjustment",
            ],
        },
    }

    for skill_name, expected in expectations.items():
        plan = _bundled_meta_plan(skill_name, tmp_path)
        step_by_id = {step.id: step for step in plan.steps}

        for step_id, fallback_id in expected["fallbacks"].items():
            assert step_by_id[step_id].on_failure == fallback_id
            assert step_by_id[fallback_id].kind == "llm_chat"

        final_step = step_by_id[expected["final"]]
        final_text = json.dumps(final_step.with_args, ensure_ascii=False)
        assert "Return the complete" in final_text
        assert "inline in chat" in final_text
        assert "Do not create, save, export, attach" in final_text
        assert "Never mention workflow, meta-skill, tool names" in final_text
        assert "connector failures, workspace paths, or runtime details" in final_text
        for required in expected["required"]:
            assert required in final_text


def test_kid_project_preferences_do_not_block_on_optional_context() -> None:
    raw = Path(
        "src/opensquilla/skills/bundled/meta-kid-project-planner/SKILL.md"
    ).read_text(encoding="utf-8")

    assert "If the request already includes a project topic, child age or age" in raw
    assert "NEEDS_CLARIFICATION: no and MISSING_FIELDS: none" in raw
    assert "Budget, exact presentation format, exact weather, and exact" in raw
    assert "proceed with explicit assumptions" in raw


def test_kid_project_planner_only_generates_vocab_when_explicitly_requested(
    tmp_path: Path,
) -> None:
    plan = _bundled_meta_plan("meta-kid-project-planner", tmp_path)
    step_by_id = {step.id: step for step in plan.steps}

    vocab_when = step_by_id["vocab_cards"].when or ""
    assert "vocab" in vocab_when
    assert "bilingual" in vocab_when
    assert "英语" in vocab_when
    assert "双语" in vocab_when
    assert "单词" in vocab_when

    final_text = json.dumps(step_by_id["deliver_project_pack"].with_args, ensure_ascii=False)
    assert "Do not include vocabulary cards unless the user explicitly asked" in final_text
    assert "For Chinese requests, do not use English section headings" in final_text
    assert "do not copy intermediate outputs verbatim" in final_text
    assert "1800-3200 Chinese characters" in final_text


def test_kid_project_planner_final_audits_original_user_constraints(
    tmp_path: Path,
) -> None:
    plan = _bundled_meta_plan("meta-kid-project-planner", tmp_path)
    step_by_id = {step.id: step for step in plan.steps}
    assert plan.final_text_mode == "step:project_pack_audit"
    final_text = json.dumps(step_by_id["deliver_project_pack"].with_args, ensure_ascii=False)
    audit_text = json.dumps(step_by_id["project_pack_audit"].with_args, ensure_ascii=False)

    assert "project_fact_ledger" in step_by_id
    assert "recall_past_projects" in step_by_id["project_fact_ledger"].depends_on
    assert "project_fact_ledger" in step_by_id["deliver_project_pack"].depends_on
    assert "deliver_project_pack" in step_by_id["project_pack_audit"].depends_on
    assert "redirect_unsafe" in step_by_id["project_pack_audit"].depends_on
    assert "project_fact_ledger" in step_by_id["project_pack_audit"].depends_on
    assert "recall_past_projects" in step_by_id["project_pack_audit"].depends_on
    assert "outline_steps" in step_by_id["project_pack_audit"].depends_on
    assert "material_list" in step_by_id["project_pack_audit"].depends_on
    assert "safety_notes" in step_by_id["project_pack_audit"].depends_on
    assert "learning_objectives" in step_by_id["project_pack_audit"].depends_on
    assert "{{ inputs.user_message" in final_text
    assert "Project fact ledger" in final_text
    assert "Durable memory / past-project recall" in final_text
    assert "Project fact ledger" in audit_text
    assert "PROVIDED_MEMORY_CONTEXT" in audit_text
    assert "Do not rewrite" in audit_text
    assert "fields as UNKNOWN" in audit_text
    assert "intermediate" in audit_text
    assert "source sections" in audit_text
    assert "artifact_ref" in audit_text
    assert "download_url" in audit_text
    assert "discard those metadata fields" in audit_text
    assert "printable record sheet" in audit_text


def test_kid_project_planner_audit_preserves_unsafe_redirect(tmp_path: Path) -> None:
    plan = _bundled_meta_plan("meta-kid-project-planner", tmp_path)
    step_by_id = {step.id: step for step in plan.steps}
    final_text = json.dumps(step_by_id["deliver_project_pack"].with_args, ensure_ascii=False)
    audit_text = json.dumps(step_by_id["project_pack_audit"].with_args, ensure_ascii=False)
    raw = Path(
        "src/opensquilla/skills/bundled/meta-kid-project-planner/SKILL.md"
    ).read_text(encoding="utf-8")

    assert "Unsafe redirect source:" in raw
    assert "return the unsafe redirect source as" in raw
    assert "Preserve its refusal and all safe alternative" in raw
    assert "PACK_DELIVERED: no_safety_redirect" in raw
    assert "poster board layout" in audit_text
    assert "inline" in audit_text
    assert "deliverables" in audit_text
    assert "Preserve every explicit user constraint" in final_text
    assert "age, deadline, location, available materials, budget" in final_text
    assert "parent time, light/weather constraints" in final_text
    assert "Do not invent calendar dates" in final_text
    assert "Do not replace user-provided materials" in final_text
    assert "Design a comparison experiment when it fits the project" in final_text


def test_kid_project_planner_final_avoids_fake_dates_weather_and_data(
    tmp_path: Path,
) -> None:
    plan = _bundled_meta_plan("meta-kid-project-planner", tmp_path)
    step_by_id = {step.id: step for step in plan.steps}
    final_text = json.dumps(step_by_id["deliver_project_pack"].with_args, ensure_ascii=False)
    audit_text = json.dumps(step_by_id["project_pack_audit"].with_args, ensure_ascii=False)

    assert "If the user gives only a relative deadline" in final_text
    assert "do not convert it into a calendar date" in final_text
    assert "Do not invent balcony direction, temperature ranges" in final_text
    assert "Do not prefill observation tables with fake measurements" in final_text
    assert "leave measurement cells blank or as placeholders" in final_text
    assert "Do not suggest tasting or eating the experiment materials" in final_text
    assert "Prefer a clear comparison design" in final_text
    assert "same seed, cup, water, and paper-towel conditions" in final_text
    assert "If the fact ledger marks a detail UNKNOWN" in final_text
    assert "Remove exact calendar dates, weekdays, months, or current-year references" in audit_text
    assert "Remove fake sample measurements" in audit_text
    assert "Remove invented balcony direction, temperature ranges, rain forecasts" in audit_text
    assert "2500-3600 Chinese characters" in audit_text
    assert "Remove leading process commentary" in audit_text
    assert "first non-empty" in audit_text
    assert "user-facing project title" in audit_text
    assert "English-only prose and English headings" in audit_text
    assert "Return markdown only" in audit_text
    assert "Never return JSON, artifact metadata" in audit_text


def test_kid_project_planner_printable_defaults_to_inline_markdown() -> None:
    raw = Path(
        "src/opensquilla/skills/bundled/meta-kid-project-planner/SKILL.md"
    ).read_text(encoding="utf-8")

    assert "Treat requests for a printable worksheet" in raw
    assert "print-ready markdown included inline" in raw
    assert "Printable\" means a clean markdown table" in raw
    assert "Do not\n            create or refer to PDFs, HTML files, downloads" in raw
    assert "unless the\n            user explicitly asked for a file/PDF/export/download" in raw
    assert "memorable title" in raw
    assert "visual theme" in raw
    assert "drawing-heavy record sheet" in raw
    assert "parent-ready poster layout" in raw


def test_lifestyle_prompts_have_english_equivalents_without_benchmark_jargon() -> None:
    rows = build_lifestyle_rows("en")
    prompts = [row["case"]["prompt"] for row in rows]

    assert all(row["case"]["case_id"].endswith("_en") for row in rows)
    assert all("benchmark:" not in prompt.lower() for prompt in prompts)
    assert all("meta-skill" not in prompt.lower() for prompt in prompts)
    assert all("OpenSquilla" not in prompt and "OpenClaw" not in prompt for prompt in prompts)
    assert any("balcony sprout" in prompt for prompt in prompts)
    assert all("example.invalid" not in prompt for prompt in prompts)


def test_lifestyle_rubrics_reward_meta_specific_artifacts() -> None:
    for case in LIFESTYLE_COMPARISON_CASES:
        assert len(case.rubric) >= 5
        assert case.failure_modes
        assert "Squilla Router" in case.expected_advantage
        assert "Opus 4.8" in case.expected_advantage
        assert "If OpenSquilla does not beat OpenClaw" in case.optimization_if_not_better


def test_lifestyle_score_rewards_strong_answers_over_t3_generic_answers() -> None:
    weak = "可以，建议你按优先级处理。我会列一个简短计划。"
    strong_by_case = {
        "kid_project_balcony_plants": """
        Age fit for an 8-year-old. Daily step plan and timeline.
        Materials, budget, substitutes. Safety and adult supervision.
        Learning objectives, data recording, charts, presentation plan.
        Weather/light assumptions and deadline constraints.
        """,
    }

    for case in LIFESTYLE_COMPARISON_CASES:
        assert score_response(
            strong_by_case[case.case_id], case
        ).total > score_response(weak, case).total


def test_lifestyle_report_labels_openclaw_t3_opus_baseline() -> None:
    rows = build_lifestyle_rows()
    markdown = render_lifestyle_markdown(rows)
    prompts = render_lifestyle_prompts_markdown(rows)

    assert "# OpenSquilla Meta-Skills vs OpenClaw t3 Matched-Skills Lifestyle Benchmark" in markdown
    assert "OpenSquilla + Squilla Router" in markdown
    assert "OpenClaw + t3 + capability-equivalent normal skills baseline" in markdown
    assert "multi-search-engine" in markdown
    assert "pdf-toolkit" in markdown
    assert "docx -> OpenClaw word-docx" in markdown
    assert "deep-research -> OpenClaw deep-research-pro" in markdown
    assert OPENCLAW_T3_MODEL in markdown
    assert "# Lifestyle Test Prompts" in prompts
    assert "OpenSquilla" not in prompts
    assert "OpenClaw" not in prompts
    assert "Benchmark constraints" not in prompts
    assert "Meta-skill:" not in prompts
    assert "Expected advantage:" not in prompts
    assert all(row["openclaw"]["model"] == OPENCLAW_T3_MODEL for row in rows)


def test_lifestyle_report_surfaces_models_and_judge_scores() -> None:
    rows = build_lifestyle_rows()
    row = rows[0]
    row["opensquilla"]["model"] = "deepseek/deepseek-v4-flash-20260423"
    row["openclaw"]["model"] = OPENCLAW_T3_MODEL
    row["score_basis"] = "llm_judge"
    row["winner"] = "opensquilla"
    row["judge"] = {
        "scores": {"opensquilla": 91, "openclaw": 87},
        "confidence": 0.81,
        "rationale": "OpenSquilla has the better final artifact.",
        "raw": {
            "subscores": {
                "opensquilla": {"final_artifact_quality": 38},
                "openclaw": {"final_artifact_quality": 34},
            }
        },
    }

    markdown = render_lifestyle_markdown(rows)

    assert "OpenSquilla model" in markdown
    assert "Judge 0-100" in markdown
    assert "Final artifact" in markdown
    assert "deepseek/deepseek-v4-flash-20260423" in markdown
    assert "91-87" in markdown
    assert "38-34" in markdown


def test_lifestyle_judge_result_requires_subscores_and_rationale() -> None:
    incomplete = JudgeResult(
        winner="openclaw",
        scores={"opensquilla": 78, "openclaw": 83},
        confidence=0.0,
        rationale="",
        risks=[],
        raw={"opensquilla": 78, "openclaw": 83},
        model="judge-model",
    )
    complete = JudgeResult(
        winner="opensquilla",
        scores={"opensquilla": 91, "openclaw": 87},
        confidence=0.8,
        rationale="OpenSquilla has the better final artifact.",
        risks=[],
        raw={
            "subscores": {
                "opensquilla": {
                    "final_artifact_quality": 38,
                    "task_completion": 19,
                    "evidence_traceability": 14,
                    "actionability": 9,
                    "risk_boundary_safety": 8,
                    "meta_skill_fit": 3,
                },
                "openclaw": {
                    "final_artifact_quality": 34,
                    "task_completion": 18,
                    "evidence_traceability": 13,
                    "actionability": 8,
                    "risk_boundary_safety": 9,
                    "meta_skill_fit": 5,
                },
            }
        },
        model="judge-model",
    )

    assert _lifestyle_judge_result_is_complete(incomplete) is False
    assert _lifestyle_judge_result_is_complete(complete) is True


def test_lifestyle_judge_scores_are_recomputed_from_weighted_subscores() -> None:
    case = LIFESTYLE_COMPARISON_CASES[0]
    opensquilla = EndpointResult(
        endpoint="opensquilla",
        case_id=case.case_id,
        ok=True,
        elapsed_s=1.0,
        response_text="strong answer",
        score={"total": 1},
    )
    openclaw = EndpointResult(
        endpoint="openclaw",
        case_id=case.case_id,
        ok=True,
        elapsed_s=1.0,
        response_text="baseline answer",
        score={"total": 1},
        model=OPENCLAW_T3_MODEL,
    )
    row = _compare_results(case, opensquilla, openclaw)
    judge = JudgeResult(
        winner="openclaw",
        scores={"opensquilla": 0, "openclaw": 100},
        confidence=0.9,
        rationale="OpenSquilla has the better weighted final artifact.",
        risks=[],
        raw={
            "subscores": {
                "opensquilla": {
                    "final_artifact_quality": 40,
                    "task_completion": 20,
                    "evidence_traceability": 15,
                    "actionability": 10,
                    "risk_boundary_safety": 10,
                    "meta_skill_fit": 5,
                },
                "openclaw": {
                    "final_artifact_quality": 30,
                    "task_completion": 20,
                    "evidence_traceability": 15,
                    "actionability": 10,
                    "risk_boundary_safety": 10,
                    "meta_skill_fit": 5,
                },
            }
        },
        model="judge-model",
    )

    updated = _apply_lifestyle_judge_result(row, judge, case)

    assert updated["judge"]["scores"] == {"opensquilla": 100, "openclaw": 90}
    assert updated["winner"] == "opensquilla"
    assert updated["judge"]["raw"]["score_source"] == "weighted_subscores"


def test_lifestyle_judge_retries_until_weighted_payload_is_complete() -> None:
    case = LIFESTYLE_COMPARISON_CASES[0]
    opensquilla = EndpointResult(
        endpoint="opensquilla",
        case_id=case.case_id,
        ok=True,
        elapsed_s=1.0,
        response_text="strong answer",
        score={"total": 1},
    )
    openclaw = EndpointResult(
        endpoint="openclaw",
        case_id=case.case_id,
        ok=True,
        elapsed_s=1.0,
        response_text="baseline answer",
        score={"total": 1},
        model=OPENCLAW_T3_MODEL,
    )

    class FakeJudge:
        def __init__(self) -> None:
            self.calls = 0

        async def judge(self, *_args):
            self.calls += 1
            if self.calls == 1:
                return JudgeResult(
                    winner="openclaw",
                    scores={"opensquilla": 10, "openclaw": 20},
                    confidence=0.1,
                    rationale="missing subscores",
                    risks=[],
                    raw={},
                    model="judge-model",
                )
            return JudgeResult(
                winner="openclaw",
                scores={"opensquilla": 10, "openclaw": 20},
                confidence=0.9,
                rationale="complete weighted payload",
                risks=[],
                raw={
                    "subscores": {
                        "opensquilla": {
                            "final_artifact_quality": 40,
                            "task_completion": 20,
                            "evidence_traceability": 15,
                            "actionability": 10,
                            "risk_boundary_safety": 10,
                            "meta_skill_fit": 5,
                        },
                        "openclaw": {
                            "final_artifact_quality": 30,
                            "task_completion": 20,
                            "evidence_traceability": 15,
                            "actionability": 10,
                            "risk_boundary_safety": 10,
                            "meta_skill_fit": 5,
                        },
                    }
                },
                model="judge-model",
            )

    fake = FakeJudge()
    result = asyncio.run(
        _judge_lifestyle_with_retries(fake, case, opensquilla, openclaw)  # type: ignore[arg-type]
    )

    assert fake.calls == 2
    assert result.scores == {"opensquilla": 100, "openclaw": 90}


def test_lifestyle_judge_existing_rejudges_jsonl_with_weighted_scores(
    tmp_path: Path,
    monkeypatch,
) -> None:
    case = LIFESTYLE_COMPARISON_CASES[0]
    report = tmp_path / "existing.jsonl"
    row = _compare_results(
        case,
        EndpointResult(
            endpoint="opensquilla",
            case_id=case.case_id,
            ok=True,
            elapsed_s=1.0,
            response_text="strong answer",
            score={"total": 1},
        ),
        EndpointResult(
            endpoint="openclaw",
            case_id=case.case_id,
            ok=True,
            elapsed_s=1.0,
            response_text="baseline answer",
            score={"total": 1},
            model=OPENCLAW_T3_MODEL,
        ),
    )
    report.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
    captured: dict[str, list[dict]] = {}

    class FakeJudge:
        def __init__(self, **_kwargs) -> None:
            pass

        async def judge(self, *_args):
            return JudgeResult(
                winner="openclaw",
                scores={"opensquilla": 0, "openclaw": 100},
                confidence=0.9,
                rationale="complete weighted payload",
                risks=[],
                raw={
                    "subscores": {
                        "opensquilla": {
                            "final_artifact_quality": 40,
                            "task_completion": 20,
                            "evidence_traceability": 15,
                            "actionability": 10,
                            "risk_boundary_safety": 10,
                            "meta_skill_fit": 5,
                        },
                        "openclaw": {
                            "final_artifact_quality": 30,
                            "task_completion": 20,
                            "evidence_traceability": 15,
                            "actionability": 10,
                            "risk_boundary_safety": 10,
                            "meta_skill_fit": 5,
                        },
                    }
                },
                model="judge-model",
            )

    def fake_write(rows, stamp=None):
        captured["rows"] = rows
        return tmp_path / "out.jsonl", tmp_path / "out.md"

    monkeypatch.setattr(
        "scripts.compare_meta_skill_openclaw_lifestyle.LLMJudge",
        FakeJudge,
    )
    monkeypatch.setattr(
        "scripts.compare_meta_skill_openclaw_lifestyle.write_lifestyle_reports",
        fake_write,
    )
    args = type(
        "Args",
        (),
        {
            "judge_jsonl": str(report),
            "judge_model": "judge-model",
            "judge_api_key": "x",
            "judge_base_url": "http://judge",
            "judge_timeout": 1.0,
        },
    )()

    asyncio.run(judge_existing(args))

    judged = captured["rows"][0]
    assert judged["winner"] == "opensquilla"
    assert judged["judge"]["scores"] == {"opensquilla": 100, "openclaw": 90}


def test_lifestyle_comparison_marks_endpoint_failure_invalid_not_win() -> None:
    case = LIFESTYLE_COMPARISON_CASES[0]
    opensquilla = EndpointResult(
        endpoint="opensquilla",
        case_id=case.case_id,
        ok=True,
        elapsed_s=1.0,
        response_text="rich answer",
        score={"total": 6},
    )
    openclaw = EndpointResult(
        endpoint="openclaw",
        case_id=case.case_id,
        ok=False,
        elapsed_s=1.0,
        response_text="",
        score={"total": 0},
        error="401 Missing Authentication header",
        model=OPENCLAW_T3_MODEL,
    )

    row = _compare_results(case, opensquilla, openclaw)

    assert row["winner"] == "invalid"
    assert row["score_basis"] == "invalid_endpoint"
    assert row["opensquilla_better"] is False
    assert row["recommended_optimization"] is None


def test_lifestyle_comparison_marks_bootstrap_response_invalid_not_win() -> None:
    case = LIFESTYLE_COMPARISON_CASES[0]
    opensquilla = EndpointResult(
        endpoint="opensquilla",
        case_id=case.case_id,
        ok=True,
        elapsed_s=1.0,
        response_text="rich answer",
        score={"total": 6},
    )
    openclaw = EndpointResult(
        endpoint="openclaw",
        case_id=case.case_id,
        ok=True,
        elapsed_s=1.0,
        response_text="Bootstrap removed. Ready for the task — what would you like me to do?",
        score={"total": 0},
        model=OPENCLAW_T3_MODEL,
    )

    row = _compare_results(case, opensquilla, openclaw)

    assert row["winner"] == "invalid"
    assert row["score_basis"] == "invalid_endpoint"
    assert row["opensquilla_better"] is False
    assert "openclaw: unrelated bootstrap response" in row["invalid_reasons"]


def test_lifestyle_comparison_only_scores_when_both_endpoints_are_valid() -> None:
    case = LIFESTYLE_COMPARISON_CASES[0]
    opensquilla = EndpointResult(
        endpoint="opensquilla",
        case_id=case.case_id,
        ok=True,
        elapsed_s=1.0,
        response_text="rich answer",
        score={"total": 6},
    )
    openclaw = EndpointResult(
        endpoint="openclaw",
        case_id=case.case_id,
        ok=True,
        elapsed_s=1.0,
        response_text="baseline answer",
        score={"total": 4},
        model=OPENCLAW_T3_MODEL,
    )

    row = _compare_results(case, opensquilla, openclaw)

    assert row["winner"] == "opensquilla"
    assert row["score_basis"] == "deterministic"
    assert row["opensquilla_better"] is True


def test_opensquilla_runner_can_isolate_agent_per_case() -> None:
    runner = OpenSquillaRunner(
        "ws://example/ws",
        token=None,
        agent_id="main",
        isolated_agent_per_case=True,
        run_id="testrun",
    )
    first = runner._agent_id_for_case(LIFESTYLE_COMPARISON_CASES[0])

    assert first == "meta-compare-testrun-kid-project-balcony-plants"
    assert first != "main"


def test_load_openclaw_baseline_refreshes_final_text_from_state(
    tmp_path: Path,
) -> None:
    case = LIFESTYLE_COMPARISON_CASES[0]
    state_dir = tmp_path / "openclaw"
    sessions_dir = state_dir / "agents" / "main" / "sessions"
    sessions_dir.mkdir(parents=True)
    session_key = "agent:main:dashboard:test"
    session_file = sessions_dir / "abc.jsonl"
    session_file.write_text(
        "\n".join(
            [
                '{"type":"message","message":{"role":"user","content":[{"type":"text","text":"'
                + case.prompt
                + '"}]}}',
                (
                    '{"type":"message","message":{"role":"assistant","content":'
                    '[{"type":"text","text":"final openclaw baseline answer with '
                    '风险 证据表 24 小时"}]}}'
                ),
            ]
        ),
        encoding="utf-8",
    )
    (sessions_dir / "abc.trajectory.jsonl").write_text(
        f'{{"sessionKey":"{session_key}"}}\n',
        encoding="utf-8",
    )
    report = tmp_path / "baseline.jsonl"
    row = {
        "case": {"case_id": case.case_id, "prompt": case.prompt},
        "openclaw": {
            "endpoint": "openclaw",
            "case_id": case.case_id,
            "ok": True,
            "elapsed_s": 1.0,
            "response_text": "checking sources",
            "score": {"total": 0},
            "session_key": session_key,
            "model": OPENCLAW_T3_MODEL,
        },
    }
    report.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")

    baseline = load_openclaw_baseline(report, [case], state_dir=state_dir)

    assert baseline[case.case_id].response_text.startswith("final openclaw baseline")
    assert baseline[case.case_id].ok is True


def test_load_openclaw_baseline_rejects_prompt_mismatch(tmp_path: Path) -> None:
    case = LIFESTYLE_COMPARISON_CASES[0]
    report = tmp_path / "baseline.jsonl"
    row = {
        "case": {"case_id": case.case_id, "prompt": "changed prompt"},
        "openclaw": {
            "endpoint": "openclaw",
            "case_id": case.case_id,
            "ok": True,
            "elapsed_s": 1.0,
            "response_text": "baseline answer",
            "score": {"total": 1},
            "model": OPENCLAW_T3_MODEL,
        },
    }
    report.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")

    try:
        load_openclaw_baseline(report, [case])
    except SystemExit as exc:
        assert "baseline prompt mismatch" in str(exc)
    else:
        raise AssertionError("expected prompt mismatch to fail")
