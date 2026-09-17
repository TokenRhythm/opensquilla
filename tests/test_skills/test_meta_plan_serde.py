"""Round-trip serialization tests for MetaPlan (PR2)."""

from __future__ import annotations

import json

from opensquilla.skills.meta.plan_serde import (
    PLAN_SERDE_VERSION,
    from_jsonable,
    to_jsonable,
)
from opensquilla.skills.meta.types import (
    ClarifyField,
    ClarifyStepConfig,
    MetaPlan,
    MetaStep,
    RouteCase,
)


def _example_plan() -> MetaPlan:
    return MetaPlan(
        name="example",
        triggers=("hello world",),
        priority=5,
        steps=(
            MetaStep(
                id="classify",
                skill="classify",
                kind="llm_classify",
                label="分类",
                label_by_language={"zh": "分类", "en": "Classify"},
                output_choices=("A", "B"),
                with_args={"text": "{{ inputs.user_message }}"},
            ),
            MetaStep(
                id="handle",
                skill="summarize",
                kind="agent",
                label="处理",
                progress_emits=False,
                depends_on=("classify",),
                route=(RouteCase(when="outputs.classify == 'A'", to="writer"),),
                with_args={"request": "{{ inputs.user_message }}"},
            ),
            MetaStep(
                id="paid-render",
                skill="seedance-2-prompt",
                kind="skill_exec",
                side_effect="external_paid_submit",
            ),
            MetaStep(
                id="collect",
                skill="collect",
                kind="user_input",
                label="澄清",
                clarify_config=ClarifyStepConfig(
                    mode="form",
                    intro="补充信息 / Add details",
                    intro_by_language={
                        "zh": "请补充信息。",
                        "en": "Please add details.",
                    },
                    fields=(
                        ClarifyField(
                            name="topic",
                            type="string",
                            required=True,
                            prompt="主题 / Topic",
                            prompt_by_language={"zh": "主题", "en": "Topic"},
                        ),
                    ),
                ),
            ),
        ),
        fallback_body="body",
        final_text_mode="step:handle",
        request_template={
            "outcome": "Decision memo",
            "fields": [{"name": "decision_criteria", "required": True}],
            "assumptions": ["Use public evidence only"],
        },
        output_contract={
            "required_sections": ["Recommendation", "Evidence"],
            "assumptions": ["Criteria inferred from request"],
            "unverified": ["Live prices not verified"],
            "artifacts": [{"name": "decision_memo.md", "required": False}],
        },
        eval_prompts=[{
            "name": "happy-path",
            "prompt": "Write a decision memo for project X",
            "rubric": ["Recommendation", "Evidence"],
        }],
        preference_keys=("preferred_language", "briefing_depth"),
        policy_tags=("public-data-only", "no-pii"),
    )


def test_to_jsonable_produces_versioned_envelope():
    payload = to_jsonable(_example_plan())
    assert payload["v"] == PLAN_SERDE_VERSION
    assert payload["v"] == 2
    assert "plan" in payload
    plan_obj = payload["plan"]
    assert plan_obj["name"] == "example"
    assert plan_obj["priority"] == 5
    assert len(plan_obj["steps"]) == 4
    assert plan_obj["steps"][0]["kind"] == "llm_classify"
    assert plan_obj["steps"][0]["label"] == "分类"
    assert plan_obj["steps"][0]["label_by_language"] == {
        "zh": "分类",
        "en": "Classify",
    }
    assert plan_obj["steps"][1]["progress_emits"] is False
    assert plan_obj["steps"][2]["side_effect"] == "external_paid_submit"
    clarify = plan_obj["steps"][3]["clarify_config"]
    assert clarify["intro_by_language"] == {
        "zh": "请补充信息。",
        "en": "Please add details.",
    }
    assert clarify["fields"][0]["prompt_by_language"] == {
        "zh": "主题",
        "en": "Topic",
    }
    assert plan_obj["request_template"]["outcome"] == "Decision memo"
    assert plan_obj["output_contract"]["required_sections"] == [
        "Recommendation",
        "Evidence",
    ]
    assert plan_obj["eval_prompts"][0]["name"] == "happy-path"
    assert plan_obj["preference_keys"] == ["preferred_language", "briefing_depth"]
    assert plan_obj["policy_tags"] == ["public-data-only", "no-pii"]


def test_to_jsonable_is_json_dumpable():
    payload = to_jsonable(_example_plan())
    json.dumps(payload, sort_keys=True)


def test_from_jsonable_round_trip():
    original = _example_plan()
    payload = to_jsonable(original)
    restored = from_jsonable(payload)
    assert restored.name == original.name
    assert restored.triggers == original.triggers
    assert restored.priority == original.priority
    assert len(restored.steps) == len(original.steps)
    assert restored.fallback_body == original.fallback_body
    assert restored.final_text_mode == original.final_text_mode
    assert restored.request_template == original.request_template
    assert restored.output_contract == original.output_contract
    assert restored.eval_prompts == original.eval_prompts
    assert restored.preference_keys == original.preference_keys
    assert restored.policy_tags == original.policy_tags


def test_from_jsonable_tolerates_legacy_envelope():
    """Deserialize a pre-PR2 snapshot dict (no 'v' key)."""
    original = _example_plan()
    payload = to_jsonable(original)
    # Strip envelope to simulate legacy row
    legacy_dict = payload["plan"]
    restored = from_jsonable(legacy_dict)
    assert restored.name == original.name
    assert restored.priority == original.priority


def test_from_jsonable_legacy_tool_call_progress_default_false():
    restored = from_jsonable({
        "name": "legacy",
        "steps": [
            {"id": "save", "kind": "tool_call", "tool": "memory_save"},
        ],
    })

    assert restored.steps[0].progress_emits is False


def test_future_version_rejected():
    import pytest
    with pytest.raises(ValueError, match="not supported"):
        from_jsonable({"v": 999, "plan": {}})


def test_all_bundled_meta_skills_round_trip():
    """Every bundled `kind: meta` SKILL.md must round-trip without loss.

    Catches schema drift between MetaPlan dataclass fields and the
    serializer / deserializer.
    """
    from pathlib import Path

    from opensquilla.skills.loader import SkillLoader
    from opensquilla.skills.meta.parser import parse_meta_plan

    bundled = Path("src/opensquilla/skills/bundled").resolve()
    loader = SkillLoader(bundled_dir=bundled)
    specs = [s for s in loader.load_all() if getattr(s, "kind", "") == "meta"]

    assert specs, "expected ≥1 bundled meta-skill"
    failures: list[str] = []
    for spec in specs:
        try:
            plan = parse_meta_plan(spec)
        except Exception as exc:
            failures.append(f"{spec.name}: parse failed: {exc}")
            continue
        if plan is None:
            continue
        try:
            restored = from_jsonable(to_jsonable(plan))
        except Exception as exc:
            failures.append(f"{spec.name}: round-trip raised: {exc}")
            continue
        if restored != plan:
            failures.append(f"{spec.name}: round-trip mismatch")
    assert not failures, "\n".join(failures)
