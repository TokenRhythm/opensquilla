"""End-to-end MVP coverage for the Meta-Skill subsystem."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.engine.steps.meta_resolution import _build_hint, meta_resolution
from opensquilla.engine.types import (
    AgentConfig,
    AgentEvent,
    DoneEvent,
    TextDeltaEvent,
)
from opensquilla.persistence.meta_run_writer import open_meta_run_writer
from opensquilla.persistence.migrator import apply_pending
from opensquilla.skills.loader import SkillLoader
from opensquilla.skills.meta.inputs import make_meta_inputs
from opensquilla.skills.meta.orchestrator import (
    MetaOrchestrator,
    _append_output_contract_block,
    _audit_output_contract,
    _verify_declared_artifacts,
    format_step_prompt,
    make_llm_chat_from_provider,
    render_with_args,
    resolve_route,
)
from opensquilla.skills.meta.parser import MetaPlanError, parse_meta_plan
from opensquilla.skills.meta.scheduler import _localized_request_template, _localized_step_label
from opensquilla.skills.meta.types import MetaMatch, MetaPlan, MetaResult, MetaStep, RouteCase
from opensquilla.skills.types import SkillLayer, SkillSpec

# ---------------------------------------------------------------------------
# Parser tests
# ---------------------------------------------------------------------------


def _make_meta_spec(
    *,
    name: str = "meta-x",
    triggers: list[str] | None = None,
    composition: dict[str, Any] | None = None,
    kind: str = "meta",
    priority: int = 0,
    content: str = "fallback body text",
    final_text_mode: str = "raw",
    output_contract: dict[str, Any] | None = None,
) -> SkillSpec:
    # Default to "raw" in the test fixture so legacy unit tests that
    # count llm_chat calls don't get an extra invocation from the auto
    # final-text summariser. Tests that exercise the auto path opt in
    # explicitly with ``final_text_mode="auto"``.
    return SkillSpec(
        name=name,
        description="test meta skill",
        layer=SkillLayer.BUNDLED,
        always=False,
        triggers=triggers or ["test trigger"],
        content=content,
        kind=kind,
        meta_priority=priority,
        composition_raw=composition,
        final_text_mode=final_text_mode,
        output_contract=output_contract or {},
    )


def test_parser_returns_none_for_regular_skill() -> None:
    spec = _make_meta_spec(kind="skill", composition=None)
    assert parse_meta_plan(spec) is None


@pytest.mark.asyncio
async def test_preflight_pause_finalizes_persisted_run_as_cancelled(
    tmp_path: Path,
) -> None:
    async def unused_runner(_prompt: str, _config: AgentConfig) -> str:
        raise AssertionError("preflight pause should not dispatch steps")

    db_path = str(tmp_path / "meta-runs.db")
    migrations_dir = Path(__file__).resolve().parents[2] / "migrations"
    apply_pending(db_path, migrations_dir)
    writer = open_meta_run_writer(db_path)
    plan = MetaPlan(
        name="meta-preflight-required",
        triggers=("fake",),
        priority=0,
        steps=(
            MetaStep(
                id="draft",
                skill="draft",
                kind="llm_chat",
                with_args={"text": "Draft for {{ inputs.audience }}"},
            ),
        ),
        request_template={
            "mode": "confirm",
            "fields": [{"name": "audience", "required": True}],
        },
        final_text_mode="raw",
    )
    orch = MetaOrchestrator(
        agent_runner=unused_runner,
        skill_loader=SimpleNamespace(),
        run_writer=writer,  # type: ignore[arg-type]
    )

    try:
        result = await orch.run(
            MetaMatch(plan=plan, inputs={"user_message": "write a brief"}),
        )
        rows = writer.list_runs(limit=5)
    finally:
        writer.close()

    assert result.paused is True
    assert len(rows) == 1
    assert rows[0].status == "cancelled"
    assert rows[0].error == "preflight_required"


@pytest.mark.asyncio
async def test_confirmed_preflight_reuses_paused_run_id(tmp_path: Path) -> None:
    async def unused_runner(_prompt: str, _config: AgentConfig) -> str:
        raise AssertionError("llm_chat step should not use the agent runner")

    async def fake_llm_chat(_system: str, _user: str) -> str:
        return "approved"

    db_path = str(tmp_path / "meta-runs.db")
    migrations_dir = Path(__file__).resolve().parents[2] / "migrations"
    apply_pending(db_path, migrations_dir)
    writer = open_meta_run_writer(db_path)
    plan = MetaPlan(
        name="meta-preflight-required",
        triggers=("fake",),
        priority=0,
        steps=(
            MetaStep(
                id="draft",
                skill="draft",
                kind="llm_chat",
                with_args={"text": "Draft for {{ inputs.audience }}"},
            ),
        ),
        request_template={
            "mode": "confirm",
            "fields": [{"name": "audience", "required": True}],
        },
        final_text_mode="raw",
    )
    orch = MetaOrchestrator(
        agent_runner=unused_runner,
        skill_loader=SimpleNamespace(),
        llm_chat=fake_llm_chat,
        run_writer=writer,  # type: ignore[arg-type]
        session_key="S1",
    )

    try:
        paused = await orch.run(
            MetaMatch(plan=plan, inputs={"user_message": "write a brief"}),
        )
        paused_run_id = writer.list_runs(limit=5)[0].run_id
        result = await orch.run(
            MetaMatch(
                plan=plan,
                inputs={
                    "user_message": "write a brief",
                    "audience": "decision owner",
                    "meta_preflight_confirmed": True,
                    "meta_preflight_run_id": paused_run_id,
                },
            ),
        )
        rows = writer.list_runs(limit=5)
    finally:
        writer.close()

    assert paused.paused is True
    assert result.ok is True
    assert len(rows) == 1
    assert rows[0].run_id == paused_run_id
    assert rows[0].status == "ok"


@pytest.mark.asyncio
async def test_confirmed_preflight_rejects_cross_session_run_id(tmp_path: Path) -> None:
    async def unused_runner(_prompt: str, _config: AgentConfig) -> str:
        raise AssertionError("cross-session confirmation must pause before steps")

    db_path = str(tmp_path / "meta-runs.db")
    migrations_dir = Path(__file__).resolve().parents[2] / "migrations"
    apply_pending(db_path, migrations_dir)
    writer = open_meta_run_writer(db_path)
    plan = MetaPlan(
        name="meta-preflight-required",
        triggers=("fake",),
        priority=0,
        steps=(
            MetaStep(
                id="draft",
                skill="draft",
                kind="llm_chat",
                with_args={"text": "Draft for {{ inputs.audience }}"},
            ),
        ),
        request_template={
            "mode": "confirm",
            "fields": [{"name": "audience", "required": True}],
        },
        final_text_mode="raw",
    )
    first = MetaOrchestrator(
        agent_runner=unused_runner,
        skill_loader=SimpleNamespace(),
        run_writer=writer,  # type: ignore[arg-type]
        session_key="S1",
    )
    second = MetaOrchestrator(
        agent_runner=unused_runner,
        skill_loader=SimpleNamespace(),
        run_writer=writer,  # type: ignore[arg-type]
        session_key="S2",
    )

    try:
        paused = await first.run(
            MetaMatch(plan=plan, inputs={"user_message": "write a brief"}),
        )
        paused_run_id = writer.list_runs(limit=5)[0].run_id
        result = await second.run(
            MetaMatch(
                plan=plan,
                inputs={
                    "user_message": "write a brief",
                    "audience": "decision owner",
                    "meta_preflight_confirmed": True,
                    "meta_preflight_run_id": paused_run_id,
                },
            ),
        )
        rows = writer.list_runs(limit=5)
    finally:
        writer.close()

    assert paused.paused is True
    assert result.paused is True
    assert len(rows) == 2
    assert {row.session_key for row in rows} == {"S1", "S2"}
    assert all(row.status == "cancelled" for row in rows)


@pytest.mark.asyncio
async def test_confirmed_preflight_rejects_completed_run_id(tmp_path: Path) -> None:
    async def unused_runner(_prompt: str, _config: AgentConfig) -> str:
        raise AssertionError("llm_chat step should not use the agent runner")

    calls = 0

    async def fake_llm_chat(_system: str, _user: str) -> str:
        nonlocal calls
        calls += 1
        return "approved"

    db_path = str(tmp_path / "meta-runs.db")
    migrations_dir = Path(__file__).resolve().parents[2] / "migrations"
    apply_pending(db_path, migrations_dir)
    writer = open_meta_run_writer(db_path)
    plan = MetaPlan(
        name="meta-preflight-required",
        triggers=("fake",),
        priority=0,
        steps=(
            MetaStep(
                id="draft",
                skill="draft",
                kind="llm_chat",
                with_args={"text": "Draft for {{ inputs.audience }}"},
            ),
        ),
        request_template={
            "mode": "confirm",
            "fields": [{"name": "audience", "required": True}],
        },
        final_text_mode="raw",
    )
    orch = MetaOrchestrator(
        agent_runner=unused_runner,
        skill_loader=SimpleNamespace(),
        llm_chat=fake_llm_chat,
        run_writer=writer,  # type: ignore[arg-type]
        session_key="S1",
    )

    try:
        paused = await orch.run(
            MetaMatch(plan=plan, inputs={"user_message": "write a brief"}),
        )
        paused_run_id = writer.list_runs(limit=5)[0].run_id
        first = await orch.run(
            MetaMatch(
                plan=plan,
                inputs={
                    "user_message": "write a brief",
                    "audience": "decision owner",
                    "meta_preflight_confirmed": True,
                    "meta_preflight_run_id": paused_run_id,
                },
            ),
        )
        second = await orch.run(
            MetaMatch(
                plan=plan,
                inputs={
                    "user_message": "write a brief",
                    "audience": "decision owner",
                    "meta_preflight_confirmed": True,
                    "meta_preflight_run_id": paused_run_id,
                },
            ),
        )
        rows = writer.list_runs(limit=5)
    finally:
        writer.close()

    assert paused.paused is True
    assert first.ok is True
    assert second.paused is True
    assert calls == 1
    assert len(rows) == 2
    by_id = {row.run_id: row for row in rows}
    assert by_id[paused_run_id].status == "ok"
    assert any(row.run_id != paused_run_id and row.status == "cancelled" for row in rows)


def test_parser_happy_path() -> None:
    spec = _make_meta_spec(
        composition={
            "steps": [
                {"id": "a", "skill": "summarize", "with": {"text": "x"}},
                {"id": "b", "skill": "docx", "depends_on": ["a"], "with": {}},
            ],
        },
        triggers=["x report"],
        priority=42,
    )
    plan = parse_meta_plan(spec)
    assert plan is not None
    assert plan.name == "meta-x"
    assert plan.priority == 42
    assert [s.id for s in plan.steps] == ["a", "b"]
    assert plan.steps[1].depends_on == ("a",)


def test_parser_rejects_cycle() -> None:
    spec = _make_meta_spec(
        composition={
            "steps": [
                {"id": "a", "skill": "x", "depends_on": ["b"]},
                {"id": "b", "skill": "y", "depends_on": ["a"]},
            ],
        },
    )
    with pytest.raises(MetaPlanError, match="cycle"):
        parse_meta_plan(spec)


def test_parser_rejects_duplicate_id() -> None:
    spec = _make_meta_spec(
        composition={
            "steps": [
                {"id": "a", "skill": "x"},
                {"id": "a", "skill": "y"},
            ],
        },
    )
    with pytest.raises(MetaPlanError, match="duplicate"):
        parse_meta_plan(spec)


def test_parser_rejects_undefined_depends_on() -> None:
    spec = _make_meta_spec(
        composition={
            "steps": [
                {"id": "a", "skill": "x", "depends_on": ["nonexistent"]},
            ],
        },
    )
    with pytest.raises(MetaPlanError, match="undefined step"):
        parse_meta_plan(spec)


# ---------------------------------------------------------------------------
# Template renderer
# ---------------------------------------------------------------------------


def test_render_with_args_xml_escape_and_truncate() -> None:
    rendered = render_with_args(
        {"q": "{{ inputs.topic | xml_escape | truncate(15) }}"},
        inputs={"topic": "Hello <world> & you"},
        outputs={},
    )
    assert rendered["q"] == "Hello &lt;world"


def test_render_with_args_extracts_user_mentioned_spreadsheet_path() -> None:
    rendered = render_with_args(
        {"path": "{{ inputs.user_message | extract_path('xlsx') }}"},
        inputs={
            "user_message": (
                "材料在这里："
                "/tmp/opensquilla-meta-skill-pr/tests/fixtures/watchlist.xlsx，"
                "请做观察清单。"
            ),
        },
        outputs={},
    )

    assert (
        rendered["path"]
        == "/tmp/opensquilla-meta-skill-pr/tests/fixtures/watchlist.xlsx"
    )


def test_meta_activation_hint_uses_meta_skill_wording_without_preamble() -> None:
    hint = _build_hint("meta-x", "travel plan", activation_mode="recommend")

    assert 'call `meta_invoke(name="meta-x")`' in hint
    assert "Do not emit explanatory text before" in hint
    assert "meta-skill" in hint
    assert "workflow" not in hint.lower()


def test_render_with_args_unknown_variable_raises() -> None:
    with pytest.raises(ValueError, match="undefined template variable"):
        render_with_args(
            {"q": "{{ outputs.missing }}"},
            inputs={},
            outputs={},
        )


def test_render_with_args_blocks_class_introspection_escape() -> None:
    """A1: the Jinja environment must block ``__class__`` attribute access so
    a SKILL.md author cannot escape via Python's MRO chain
    (``{{ inputs.__class__.__mro__[1].__subclasses__() }}``)."""
    with pytest.raises(ValueError, match="security violation"):
        render_with_args(
            {"hack": "{{ inputs.__class__ }}"},
            inputs={"x": 1},
            outputs={},
        )


def test_render_with_args_blocks_subclasses_walk() -> None:
    """A1: full attribute chain that previously let a template enumerate
    every loaded Python subclass must raise SecurityError (wrapped as
    ValueError for the orchestrator's step-failure path)."""
    with pytest.raises(ValueError, match="security violation"):
        render_with_args(
            {"hack": "{{ inputs.__class__.__mro__[1].__subclasses__() }}"},
            inputs={"x": 1},
            outputs={},
        )


def test_render_with_args_preserves_nested_get_chain() -> None:
    """A1 regression guard: the sandbox upgrade must preserve the
    ``inputs.get('collected', {}).get('field', default)`` pattern that
    bundled creator skills (meta-skill-creator, meta-paper-write, etc.)
    rely on. Without this guard a sandbox-strictness change could break
    every bundled meta skill silently."""
    rendered = render_with_args(
        {"out": "{{ inputs.get('collected', {}).get('field', 'fallback') }}"},
        inputs={"collected": {"field": "got it"}},
        outputs={},
    )
    assert rendered == {"out": "got it"}


def test_render_with_args_preserves_subscript_and_filter_pipeline() -> None:
    """A1 regression guard: subscript access, the ``tojson`` filter, and
    the ``length`` filter must all survive the sandbox upgrade. These are
    the load-bearing primitives for the bundled meta-skill-creator DAG.

    NOTE: the third pipeline uses ``outputs.numbers`` rather than ``items``
    on purpose — ``obj.items`` resolves to the bound dict method via
    Jinja's getattr-first attribute protocol, which would fail under any
    Jinja environment (sandboxed or not). The pipeline shape we care
    about is "dotted access into a stored list, then ``| length``"."""
    rendered = render_with_args(
        {
            "subscript": "{{ inputs['user_message'] | truncate(8) }}",
            "tojson": "{{ outputs.payload | tojson }}",
            "length": "{{ outputs.numbers | length }}",
        },
        inputs={"user_message": "hello-world-meta-skill"},
        outputs={"payload": {"k": "v"}, "numbers": [1, 2, 3]},
    )
    assert rendered["subscript"] == "hello-wo"
    assert rendered["tojson"] == '{"k": "v"}'
    assert rendered["length"] == "3"


def test_resolve_route_blocks_class_introspection_escape() -> None:
    """A1: ``route.when`` expressions go through ``compile_expression`` on
    the same sandboxed env; introspection escapes must surface as
    ValueError so the orchestrator treats them as a step failure rather
    than a silent allow."""
    from opensquilla.skills.meta.templating import resolve_route
    from opensquilla.skills.meta.types import RouteCase

    with pytest.raises(ValueError, match="security violation"):
        resolve_route(
            (RouteCase(when="inputs.__class__.__name__ == 'dict'", to="x"),),
            inputs={"x": 1},
            outputs={},
        )


def test_evaluate_when_blocks_class_introspection_escape() -> None:
    """A1: step-level ``when`` expressions follow the same contract as
    ``route.when`` — sandbox violations must raise ValueError."""
    from opensquilla.skills.meta.templating import evaluate_when

    with pytest.raises(ValueError, match="security violation"):
        evaluate_when(
            "inputs.__class__.__name__ == 'dict'",
            inputs={"x": 1},
            outputs={},
        )


def test_when_expression_supports_lower_filter() -> None:
    from opensquilla.skills.meta.templating import evaluate_when

    assert evaluate_when(
        "'current repo' in (inputs.user_message | lower)",
        inputs={"user_message": "Please use the CURRENT REPO"},
        outputs={},
    )


def test_format_step_prompt_includes_all_args() -> None:
    out = format_step_prompt("summarize", {"text": "hello", "max_words": 100})
    assert "summarize" in out
    assert "text: hello" in out
    assert "max_words: 100" in out


def test_make_meta_inputs_marks_plain_english_requests_as_english_only() -> None:
    inputs = make_meta_inputs(user_message="Please build a launch brief.")

    assert inputs["user_language"] == "en"
    assert "English only" in inputs["language_instruction"]
    assert "Do not copy Chinese or bilingual headings" in inputs["language_instruction"]


def test_make_meta_inputs_marks_chinese_requests_as_simplified_chinese() -> None:
    inputs = make_meta_inputs(user_message="帮我做一个发布简报")

    assert inputs["user_language"] == "zh"
    assert "Simplified Chinese" in inputs["language_instruction"]


def test_make_meta_inputs_extracts_preflight_confirmation_marker() -> None:
    inputs = make_meta_inputs(
        user_message=(
            "Please build a launch brief.\n\n"
            "<!-- opensquilla:meta_preflight_confirmed=1 -->\n"
            "<!-- opensquilla:meta_preflight_run_id=01ABC -->"
        ),
    )

    assert inputs["meta_preflight_confirmed"] is True
    assert inputs["meta_preflight_run_id"] == "01ABC"
    assert inputs["user_message"] == "Please build a launch brief."
    assert "meta_preflight_confirmed" not in inputs["language_instruction"]


def test_make_meta_inputs_removes_preflight_confirmed_fields_block() -> None:
    inputs = make_meta_inputs(
        user_message=(
            "Please review the renewal.\n\n"
            "Confirmed request fields:\n"
            "- audience: decision owner\n"
            "- language: English\n\n"
            "<!-- opensquilla:meta_preflight_confirmed=1 -->"
        ),
    )

    assert inputs["meta_preflight_confirmed"] is True
    assert inputs["user_message"] == "Please review the renewal."
    assert "Confirmed request fields" not in inputs["language_instruction"]


def test_make_meta_inputs_language_preference_overrides_detected_user_language() -> None:
    english = make_meta_inputs(
        user_message="请帮我判断这份合同",
        language="English",
    )
    chinese = make_meta_inputs(
        user_message="Please review this contract",
        preferences={"language": "中文"},
    )

    assert english["user_language"] == "en"
    assert "English only" in english["language_instruction"]
    assert chinese["user_language"] == "zh"
    assert "Simplified Chinese" in chinese["language_instruction"]


def test_preflight_request_template_localizes_for_user_language() -> None:
    template = {
        "outcome": "Decision memo",
        "outcome_zh": "决策简报",
        "outcome_en": "Decision brief",
        "fields": [
            {
                "name": "decision_question",
                "required": True,
                "label_zh": "决策问题",
                "label_en": "Decision question",
                "description_zh": "你要判断什么",
                "description_en": "What you need to decide",
            }
        ],
        "assumptions": ["Use practical defaults."],
        "assumptions_zh": ["如果缺少标准，默认按风险、成本和可逆性判断。"],
        "assumptions_en": ["If criteria are missing, use risk, cost, and reversibility."],
    }

    zh = _localized_request_template(template, "zh")
    en = _localized_request_template(template, "en")

    assert zh["outcome"] == "决策简报"
    assert zh["fields"][0]["label"] == "决策问题"
    assert zh["fields"][0]["description"] == "你要判断什么"
    assert zh["assumptions"] == ["如果缺少标准，默认按风险、成本和可逆性判断。"]
    assert en["outcome"] == "Decision brief"
    assert en["fields"][0]["label"] == "Decision question"
    assert en["fields"][0]["description"] == "What you need to decide"
    assert en["assumptions"] == ["If criteria are missing, use risk, cost, and reversibility."]


def test_meta_step_label_localizes_for_user_language() -> None:
    step = MetaStep(
        id="risk_review",
        skill="llm",
        label="风险审查",
        label_by_language={"zh": "风险审查", "en": "Risk review"},
    )

    assert _localized_step_label(step, "zh") == "风险审查"
    assert _localized_step_label(step, "en") == "Risk review"


def test_meta_step_label_without_language_metadata_uses_declared_label() -> None:
    step = MetaStep(id="pdf_extract", skill="tool", label="PDF 抽取")

    assert _localized_step_label(step, "en") == "PDF 抽取"


def test_all_bundled_meta_skills_have_language_safe_user_visible_copy(
    tmp_path: Path,
) -> None:
    def has_cjk(text: object) -> bool:
        return any(
            "\u3400" <= ch <= "\u9fff" or "\uf900" <= ch <= "\ufaff"
            for ch in str(text)
        )

    def visible(value: object) -> str:
        if value is None:
            return ""
        if isinstance(value, list):
            return " ".join(str(item) for item in value)
        return str(value)

    bundled = Path("src/opensquilla/skills/bundled").resolve()
    loader = SkillLoader(
        bundled_dir=bundled,
        snapshot_path=tmp_path / "skills-snapshot.json",
    )
    specs = [s for s in loader.load_all() if getattr(s, "kind", "") == "meta"]

    assert specs
    issues: list[str] = []
    for spec in specs:
        plan = parse_meta_plan(spec)
        assert plan is not None
        for language in ("zh", "en"):
            template = _localized_request_template(plan.request_template, language)
            outcome = visible(template.get("outcome"))
            if language == "zh" and not has_cjk(outcome):
                issues.append(f"{spec.name}: zh outcome is not Chinese: {outcome}")
            if language == "en" and has_cjk(outcome):
                issues.append(f"{spec.name}: en outcome contains Chinese: {outcome}")
            for field in template.get("fields", []) or []:
                if not isinstance(field, dict):
                    continue
                label = visible(field.get("label") or field.get("name"))
                default = visible(field.get("default"))
                if language == "zh" and not has_cjk(label):
                    issues.append(
                        f"{spec.name}: zh label for {field.get('name')} is not Chinese: "
                        f"{label}"
                    )
                if language == "en" and has_cjk(label):
                    issues.append(
                        f"{spec.name}: en label for {field.get('name')} contains "
                        f"Chinese: {label}"
                    )
                if language == "en" and has_cjk(default):
                    issues.append(
                        f"{spec.name}: en default for {field.get('name')} contains "
                        f"Chinese: {default}"
                    )
            assumptions = visible(template.get("assumptions"))
            if language == "zh" and assumptions and not has_cjk(assumptions):
                issues.append(f"{spec.name}: zh assumptions are not Chinese")
            if language == "en" and has_cjk(assumptions):
                issues.append(f"{spec.name}: en assumptions contain Chinese")
        for step in plan.steps:
            en_label = _localized_step_label(step, "en")
            if has_cjk(en_label):
                issues.append(
                    f"{spec.name}: en label for step {step.id} contains Chinese: "
                    f"{en_label}"
                )
            cfg = step.clarify_config
            if cfg is None:
                continue
            if cfg.intro and not cfg.intro_by_language.get("zh"):
                issues.append(f"{spec.name}: {step.id} missing intro_zh")
            if cfg.intro and not cfg.intro_by_language.get("en"):
                issues.append(f"{spec.name}: {step.id} missing intro_en")
            for field in cfg.fields:
                if field.prompt and not field.prompt_by_language.get("zh"):
                    issues.append(f"{spec.name}: {step.id}.{field.name} missing prompt_zh")
                if field.prompt and not field.prompt_by_language.get("en"):
                    issues.append(f"{spec.name}: {step.id}.{field.name} missing prompt_en")

    assert not issues, "\n".join(issues)


def test_all_bundled_meta_skills_keep_output_contract_audits_internal(
    tmp_path: Path,
) -> None:
    bundled = Path("src/opensquilla/skills/bundled").resolve()
    loader = SkillLoader(
        bundled_dir=bundled,
        snapshot_path=tmp_path / "skills-snapshot.json",
    )
    specs = [s for s in loader.load_all() if getattr(s, "kind", "") == "meta"]

    assert specs
    issues: list[str] = []
    for spec in specs:
        plan = parse_meta_plan(spec)
        assert plan is not None
        if plan.output_contract and plan.output_contract.get("append_to_final_text") is not False:
            issues.append(f"{spec.name}: output_contract must stay out of final text")

    assert not issues, "\n".join(issues)


# ---------------------------------------------------------------------------
# Resolver (engine.steps.meta_resolution)
# ---------------------------------------------------------------------------


class _FakeLoader:
    def __init__(self, specs: list[SkillSpec]) -> None:
        self._specs = specs

    def load_all(self) -> list[SkillSpec]:
        return list(self._specs)

    def get_by_name(self, name: str) -> SkillSpec | None:
        for s in self._specs:
            if s.name == name:
                return s
        return None


@pytest.mark.asyncio
async def test_meta_resolution_matches_trigger() -> None:
    spec = _make_meta_spec(
        composition={"steps": [{"id": "a", "skill": "summarize"}]},
        triggers=["pdf briefing"],
        priority=10,
    )
    loader = _FakeLoader([spec])
    ctx = SimpleNamespace(
        message="please make me a PDF briefing on rust",
        semantic_message="please make me a PDF briefing on rust",
        metadata={"skill_loader": loader},
        config=SimpleNamespace(meta_skill=SimpleNamespace(enabled=True, auto_trigger=True)),
    )
    out = await meta_resolution(ctx)  # type: ignore[arg-type]
    match = out.metadata["meta_match"]
    assert match.plan.name == "meta-x"
    assert match.inputs["user_message"] == "please make me a PDF briefing on rust"
    assert out.metadata["meta_match_tool_choice"] == {
        "type": "function",
        "function": {"name": "meta_invoke"},
    }


@pytest.mark.asyncio
async def test_meta_resolution_threads_preference_metadata_into_match_inputs() -> None:
    spec = _make_meta_spec(
        composition={"steps": [{"id": "a", "skill": "summarize"}]},
        triggers=["pdf briefing"],
        priority=10,
    )
    loader = _FakeLoader([spec])
    ctx = SimpleNamespace(
        message="please make me a PDF briefing on rust",
        semantic_message="please make me a PDF briefing on rust",
        metadata={
            "skill_loader": loader,
            "meta_preferences": {"briefing_depth": "compact"},
            "meta_audience": "executives",
            "meta_language": "zh-CN",
        },
        config=SimpleNamespace(meta_skill=SimpleNamespace(enabled=True, auto_trigger=True)),
    )

    out = await meta_resolution(ctx)  # type: ignore[arg-type]

    match = out.metadata["meta_match"]
    assert match.inputs["audience"] == "executives"
    assert match.inputs["language"] == "zh-CN"
    assert match.inputs["preferences"]["briefing_depth"] == "compact"


@pytest.mark.asyncio
async def test_meta_resolution_ignores_trigger_inside_raw_page_dump() -> None:
    spec = _make_meta_spec(
        composition={"steps": [{"id": "a", "skill": "summarize"}]},
        triggers=["家庭日程"],
        priority=10,
    )
    loader = _FakeLoader([spec])
    semantic_text = "Please process the attached WebChat page dump."
    ctx = SimpleNamespace(
        message=(
            "WebChat page dump: navigation, ads, contacts, and a copied section "
            "mentioning 家庭日程 inside the attached material."
        ),
        raw_message=semantic_text,
        semantic_message=semantic_text,
        metadata={"skill_loader": loader},
    )

    out = await meta_resolution(ctx)  # type: ignore[arg-type]

    assert "meta_match" not in out.metadata
    assert "meta_skill_match" not in out.metadata


@pytest.mark.asyncio
async def test_meta_resolution_semantic_fallback_matches_without_trigger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib
    meta_resolution_module = importlib.import_module(
        "opensquilla.engine.steps.meta_resolution",
    )

    spec = _make_meta_spec(
        name="meta-pdf-intelligence",
        composition={"steps": [{"id": "a", "skill": "summarize"}]},
        triggers=["PDF analysis"],
        priority=55,
    )
    loader = _FakeLoader([spec])

    class FakeRetriever:
        def __init__(self, **kwargs: Any) -> None:
            assert kwargs["strategy"] == "hybrid"

        def retrieve(self, skills: list[SkillSpec], query: str, top_k: int = 1) -> list[SkillSpec]:
            assert query == "帮我看一下这个文档，重点讲结论和风险"
            assert top_k == 1
            return [skills[0]]

    monkeypatch.setattr(meta_resolution_module, "HybridRetriever", FakeRetriever)

    ctx = SimpleNamespace(
        message="帮我看一下这个文档，重点讲结论和风险",
        semantic_message="帮我看一下这个文档，重点讲结论和风险",
        session_key="semantic-session",
        metadata={"skill_loader": loader},
        system_prompt=("base prompt", ""),
        config=SimpleNamespace(
            skills=SimpleNamespace(filter_strategy="lexical"),
            meta_skill=SimpleNamespace(enabled=True, auto_trigger=True),
        ),
    )

    out = await meta_resolution(ctx)  # type: ignore[arg-type]

    assert out.metadata["meta_match"].plan.name == "meta-pdf-intelligence"
    assert out.metadata["meta_match_source"] == "semantic"
    assert out.metadata["meta_match_trigger"] == "semantic"
    assert out.metadata["meta_activation_mode"] == "hint"
    assert "meta_match_tool_choice" not in out.metadata
    hint = str(out.system_prompt)
    assert "Activation mode: hint" in hint
    assert 'meta_invoke(name="meta-pdf-intelligence")' in hint
    assert "Do not answer directly" in hint
    assert "Do not call ordinary tools before `meta_invoke`" in hint


@pytest.mark.asyncio
async def test_meta_resolution_soft_hint_directs_meta_invoke_not_skill_view() -> None:
    spec = _make_meta_spec(
        composition={"steps": [{"id": "a", "skill": "summarize"}]},
        triggers=["travel plan"],
        priority=10,
    )
    loader = _FakeLoader([spec])
    ctx = SimpleNamespace(
        message="please make a travel plan for Dalian",
        semantic_message="please make a travel plan for Dalian",
        metadata={"skill_loader": loader},
        system_prompt=("base prompt", ""),
        config=SimpleNamespace(meta_skill=SimpleNamespace(enabled=True, auto_trigger=True)),
    )

    out = await meta_resolution(ctx)  # type: ignore[arg-type]

    hint = out.system_prompt[1]
    assert out.metadata["meta_activation_mode"] == "recommend"
    assert out.metadata["meta_match_tool_choice"] == {
        "type": "function",
        "function": {"name": "meta_invoke"},
    }
    assert "Activation mode: recommend" in hint
    assert 'call `meta_invoke(name="meta-x")`' in hint
    assert "Do not call `skill_view` for this meta-skill" in hint


@pytest.mark.asyncio
async def test_meta_resolution_noops_when_meta_skill_config_disabled() -> None:
    spec = _make_meta_spec(
        composition={"steps": [{"id": "a", "skill": "summarize"}]},
        triggers=["pdf briefing"],
        priority=10,
    )
    loader = _FakeLoader([spec])
    ctx = SimpleNamespace(
        message="please make me a PDF briefing on rust",
        semantic_message="please make me a PDF briefing on rust",
        config=SimpleNamespace(meta_skill=SimpleNamespace(enabled=False)),
        metadata={"skill_loader": loader},
        system_prompt="base",
    )

    out = await meta_resolution(ctx)  # type: ignore[arg-type]

    assert "meta_match" not in out.metadata
    assert out.system_prompt == "base"


@pytest.mark.asyncio
async def test_meta_resolution_highest_priority_wins() -> None:
    lo = _make_meta_spec(
        name="meta-lo",
        composition={"steps": [{"id": "a", "skill": "summarize"}]},
        triggers=["report"],
        priority=10,
    )
    hi = _make_meta_spec(
        name="meta-hi",
        composition={"steps": [{"id": "a", "skill": "summarize"}]},
        triggers=["report"],
        priority=99,
    )
    loader = _FakeLoader([lo, hi])
    ctx = SimpleNamespace(
        message="produce a report",
        semantic_message="produce a report",
        metadata={"skill_loader": loader},
        config=SimpleNamespace(meta_skill=SimpleNamespace(enabled=True, auto_trigger=True)),
    )
    out = await meta_resolution(ctx)  # type: ignore[arg-type]
    assert out.metadata["meta_match"].plan.name == "meta-hi"


@pytest.mark.asyncio
async def test_meta_resolution_ignores_triggers_inside_pasted_webchat_dump() -> None:
    spec = _make_meta_spec(
        name="meta-household-calendar-test",
        composition={"steps": [{"id": "a", "skill": "summarize"}]},
        triggers=["家庭日程"],
        priority=56,
    )
    loader = _FakeLoader([spec])
    dump_body = "\n".join(
        [
            "WebChat dump",
            "assistant: 这里是旧页面里的 skill 列表",
            "meta-household-calendar-test 家庭日程协调",
            "meta-skill-creator",
        ]
        + [f"history line {i}" for i in range(20)]
    )
    ctx = SimpleNamespace(
        message=f"请分析下面历史页面是否有误触发，不要运行任何技能。\n{dump_body}",
        semantic_message=f"请分析下面历史页面是否有误触发，不要运行任何技能。\n{dump_body}",
        system_prompt="base",
        metadata={"skill_loader": loader},
    )

    out = await meta_resolution(ctx)  # type: ignore[arg-type]

    assert "meta_match" not in out.metadata
    assert out.system_prompt == "base"


@pytest.mark.asyncio
async def test_meta_resolution_invokes_explicit_named_meta_skill_request() -> None:
    spec = _make_meta_spec(
        name="AwesomeWebpageMetaSkill",
        composition={"steps": [{"id": "a", "skill": "summarize"}]},
        triggers=["AwesomeWebpageMetaSkill"],
        priority=50,
    )
    loader = _FakeLoader([spec])
    ctx = SimpleNamespace(
        message="invoke AwesomeWebpageMetaSkill to create a webpage",
        semantic_message="invoke AwesomeWebpageMetaSkill to create a webpage",
        system_prompt=("base prompt", ""),
        metadata={"skill_loader": loader},
        config=SimpleNamespace(meta_skill=SimpleNamespace(enabled=True, auto_trigger=True)),
    )

    out = await meta_resolution(ctx)  # type: ignore[arg-type]

    assert out.metadata["meta_match"].plan.name == "AwesomeWebpageMetaSkill"
    assert out.metadata["meta_match_tool_choice"] == {
        "type": "function",
        "function": {"name": "meta_invoke"},
    }


@pytest.mark.asyncio
async def test_meta_resolution_still_matches_current_cjk_intent() -> None:
    spec = _make_meta_spec(
        name="meta-household-calendar-test",
        composition={"steps": [{"id": "a", "skill": "summarize"}]},
        triggers=["家庭日程"],
        priority=56,
    )
    loader = _FakeLoader([spec])
    ctx = SimpleNamespace(
        message="帮我安排明天的家庭日程",
        semantic_message="帮我安排明天的家庭日程",
        system_prompt="base",
        metadata={"skill_loader": loader},
        config=SimpleNamespace(meta_skill=SimpleNamespace(enabled=True, auto_trigger=True)),
    )

    out = await meta_resolution(ctx)  # type: ignore[arg-type]

    assert out.metadata["meta_match"].plan.name == "meta-household-calendar-test"


@pytest.mark.asyncio
async def test_meta_resolution_promotes_meta_skill_creator_to_highest_text_tier() -> None:
    spec = _make_meta_spec(
        name="meta-skill-creator",
        composition={"steps": [{"id": "a", "skill": "summarize"}]},
        triggers=["create a meta-skill"],
        priority=90,
    )
    loader = _FakeLoader([spec])
    ctx = SimpleNamespace(
        message="please create a meta-skill for analyst briefs",
        semantic_message="please create a meta-skill for analyst briefs",
        model="router-default-model",
        system_prompt=("base system prompt", "dynamic system prompt"),
        metadata={"skill_loader": loader},
        config=SimpleNamespace(
            squilla_router=SimpleNamespace(
                tiers={
                    "c0": {"model": "cheap-model"},
                    "c1": {"model": "balanced-model"},
                    "c2": {"model": "strong-model"},
                    "c3": {"model": "frontier-model"},
                    "image": {"model": "vision-model", "image_only": True},
                },
            ),
            meta_skill=SimpleNamespace(enabled=True, auto_trigger=True),
        ),
    )

    out = await meta_resolution(ctx)  # type: ignore[arg-type]

    assert out.metadata["meta_match"].plan.name == "meta-skill-creator"
    assert out.metadata["meta_match"].inputs["system_prompt"] == (
        "base system prompt\n\n"
        "dynamic system prompt"
    )
    assert out.model == "frontier-model"
    assert out.metadata["meta_required_tier"] == "c3"
    assert out.metadata["meta_required_model"] == "frontier-model"
    assert out.metadata["meta_required_source"] == "meta-skill-creator"
    assert out.metadata["routed_tier"] == "c3"
    assert out.metadata["routed_model"] == "frontier-model"
    assert out.metadata["routing_source"] == "meta_skill_required_tier"
    assert out.metadata["routing_confidence"] == 1.0


@pytest.mark.asyncio
async def test_meta_resolution_does_not_promote_non_creator_meta_to_highest_text_tier() -> None:
    spec = _make_meta_spec(
        name="meta-pdf-intelligence",
        composition={"steps": [{"id": "a", "skill": "summarize"}]},
        triggers=["PDF intelligence"],
        priority=55,
    )
    loader = _FakeLoader([spec])
    ctx = SimpleNamespace(
        message="Run this as a PDF intelligence task",
        semantic_message="Run this as a PDF intelligence task",
        model="router-default-model",
        system_prompt=("base system prompt", ""),
        metadata={"skill_loader": loader},
        config=SimpleNamespace(
            squilla_router=SimpleNamespace(
                tiers={
                    "c1": {"model": "cheap-model"},
                    "c3": {"model": "frontier-model"},
                    "vision": {"model": "vision-model", "image_only": True},
                },
            ),
            meta_skill=SimpleNamespace(enabled=True, auto_trigger=True),
        ),
    )

    out = await meta_resolution(ctx)  # type: ignore[arg-type]

    assert out.metadata["meta_match"].plan.name == "meta-pdf-intelligence"
    assert out.model == "router-default-model"
    assert "meta_required_tier" not in out.metadata
    assert "meta_required_model" not in out.metadata
    assert "meta_required_source" not in out.metadata
    assert "routing_source" not in out.metadata
    assert "routing_confidence" not in out.metadata


@pytest.mark.asyncio
async def test_meta_resolution_no_match_keeps_metadata_clean() -> None:
    spec = _make_meta_spec(
        composition={"steps": [{"id": "a", "skill": "summarize"}]},
        triggers=["nope"],
    )
    loader = _FakeLoader([spec])
    ctx = SimpleNamespace(
        message="hello world",
        semantic_message="hello world",
        metadata={"skill_loader": loader},
    )
    out = await meta_resolution(ctx)  # type: ignore[arg-type]
    assert "meta_match" not in out.metadata


# ---------------------------------------------------------------------------
# Orchestrator with stub Agent runner
# ---------------------------------------------------------------------------


def _make_skill_spec(name: str, content: str = "") -> SkillSpec:
    return SkillSpec(
        name=name,
        description=f"{name} description",
        layer=SkillLayer.BUNDLED,
        always=False,
        triggers=[],
        content=content,
        kind="skill",
    )


@pytest.mark.asyncio
async def test_orchestrator_runs_steps_in_topological_order() -> None:
    # Plan: a -> b -> c, sub-Agent echoes the system prompt back as final text
    spec = _make_meta_spec(
        composition={
            "steps": [
                {"id": "a", "skill": "skill_a", "with": {"in": "alpha"}},
                {
                    "id": "b",
                    "skill": "skill_b",
                    "depends_on": ["a"],
                    "with": {"upstream": "{{ outputs.a }}"},
                },
                {
                    "id": "c",
                    "skill": "skill_c",
                    "depends_on": ["b"],
                    "with": {"upstream": "{{ outputs.b }}"},
                },
            ],
        },
    )
    plan = parse_meta_plan(spec)
    assert plan is not None
    loader = _FakeLoader(
        [
            _make_skill_spec("skill_a", content="A-skill"),
            _make_skill_spec("skill_b", content="B-skill"),
            _make_skill_spec("skill_c", content="C-skill"),
        ],
    )

    call_log: list[tuple[str, str]] = []

    async def stub_runner(system_prompt: str, user_message: str) -> AsyncIterator[AgentEvent]:
        call_log.append((system_prompt, user_message))
        # Each step returns a deterministic payload that the next can quote.
        if "A-skill" in system_prompt:
            yield TextDeltaEvent(text="OUT_A")
        elif "B-skill" in system_prompt:
            yield TextDeltaEvent(text="OUT_B(" + user_message.count("OUT_A").__str__() + ")")
        elif "C-skill" in system_prompt:
            yield TextDeltaEvent(text="OUT_C[" + user_message.count("OUT_B").__str__() + "]")
        yield DoneEvent(text="")

    orch = MetaOrchestrator(agent_runner=stub_runner, skill_loader=loader)
    match = MetaMatch(plan=plan, inputs={"user_message": "trigger"})
    result = await orch.run(match)

    assert result.ok, result.error
    assert result.final_text == "OUT_C[1]"
    assert result.step_outputs == {
        "a": "OUT_A",
        "b": "OUT_B(1)",
        "c": "OUT_C[1]",
    }
    # 3 sub-Agent invocations, in dependency order
    assert len(call_log) == 3
    assert "A-skill" in call_log[0][0]
    assert "B-skill" in call_log[1][0]
    assert "C-skill" in call_log[2][0]


@pytest.mark.asyncio
async def test_orchestrator_skips_step_when_condition_is_false() -> None:
    from opensquilla.engine.types import ToolResultEvent

    spec = _make_meta_spec(
        composition={
            "steps": [
                {"id": "a", "skill": "skill_a"},
                {
                    "id": "b",
                    "skill": "skill_b",
                    "depends_on": ["a"],
                    "when": "outputs.a == 'RUN_B'",
                },
                {
                    "id": "c",
                    "skill": "skill_c",
                    "depends_on": ["b"],
                    "with": {"upstream": "{{ outputs.b }}"},
                },
            ],
        },
    )
    plan = parse_meta_plan(spec)
    assert plan is not None
    loader = _FakeLoader(
        [
            _make_skill_spec("skill_a", content="A-skill"),
            _make_skill_spec("skill_b", content="B-skill"),
            _make_skill_spec("skill_c", content="C-skill"),
        ],
    )

    call_log: list[str] = []

    async def stub_runner(system_prompt: str, user_message: str) -> AsyncIterator[AgentEvent]:
        call_log.append(system_prompt)
        if "A-skill" in system_prompt:
            yield TextDeltaEvent(text="SKIP_B")
        elif "B-skill" in system_prompt:
            yield TextDeltaEvent(text="must-not-run")
        elif "C-skill" in system_prompt:
            yield TextDeltaEvent(text=f"C saw {user_message!r}")
        yield DoneEvent(text="")

    orch = MetaOrchestrator(agent_runner=stub_runner, skill_loader=loader)
    skipped_results: list[ToolResultEvent] = []
    final: MetaResult | None = None
    async for ev in orch.iter_events(MetaMatch(plan=plan, inputs={"user_message": "x"})):
        if isinstance(ev, ToolResultEvent) and ev.tool_name == "meta-step:b":
            skipped_results.append(ev)
        if isinstance(ev, MetaResult):
            final = ev

    assert final is not None
    assert final.ok, final.error
    assert final.step_outputs["b"] == ""
    assert "B-skill" not in "\n".join(call_log)
    assert skipped_results
    assert skipped_results[-1].arguments is not None
    assert skipped_results[-1].arguments["skipped"] is True


@pytest.mark.asyncio
async def test_orchestrator_returns_failure_when_step_skill_missing() -> None:
    spec = _make_meta_spec(
        composition={"steps": [{"id": "a", "skill": "nonexistent_skill"}]},
    )
    plan = parse_meta_plan(spec)
    assert plan is not None
    loader = _FakeLoader([])  # No skills registered

    async def stub_runner(_sys: str, _user: str) -> AsyncIterator[AgentEvent]:
        yield TextDeltaEvent(text="never")

    orch = MetaOrchestrator(agent_runner=stub_runner, skill_loader=loader)
    match = MetaMatch(plan=plan, inputs={"user_message": "x"})
    result = await orch.run(match)

    assert not result.ok
    assert result.failed_step_id == "a"
    assert "not found" in (result.error or "")


@pytest.mark.asyncio
async def test_orchestrator_refuses_meta_inside_meta() -> None:
    spec = _make_meta_spec(
        composition={"steps": [{"id": "a", "skill": "inner-meta"}]},
    )
    plan = parse_meta_plan(spec)
    assert plan is not None
    inner_meta = _make_meta_spec(
        name="inner-meta",
        composition={"steps": [{"id": "z", "skill": "summarize"}]},
    )
    loader = _FakeLoader([inner_meta])

    async def stub_runner(_sys: str, _user: str) -> AsyncIterator[AgentEvent]:
        yield TextDeltaEvent(text="never")

    orch = MetaOrchestrator(agent_runner=stub_runner, skill_loader=loader)
    match = MetaMatch(plan=plan, inputs={"user_message": "x"})
    result = await orch.run(match)

    assert not result.ok
    assert "cannot compose another meta-skill" in (result.error or "")


# ---------------------------------------------------------------------------
# Loader integration — make sure the bundled sample is picked up
# ---------------------------------------------------------------------------


def test_bundled_sample_loads(tmp_path: Path) -> None:
    bundled = Path(__file__).resolve().parents[2] / "src" / "opensquilla" / "skills" / "bundled"
    snapshot = tmp_path / "snap.json"
    loader = SkillLoader(bundled_dir=bundled, snapshot_path=snapshot)
    loader.invalidate_cache()
    specs = {s.name: s for s in loader.load_all()}
    meta = specs.get("meta-kid-project-planner")
    assert meta is not None
    assert meta.kind == "meta"
    plan = parse_meta_plan(meta)
    assert plan is not None
    step_ids = {s.id for s in plan.steps}
    assert {
        "preferences",
        "project_clarify",
        "outline_steps",
        "deliver_project_pack",
        "project_pack_audit",
    } <= step_ids


# ---------------------------------------------------------------------------
# Routing primitive
# ---------------------------------------------------------------------------


def test_parser_accepts_route() -> None:
    spec = _make_meta_spec(
        composition={
            "steps": [
                {"id": "classify", "skill": "sub-agent"},
                {
                    "id": "ingest",
                    "skill": "deep-research",
                    "depends_on": ["classify"],
                    "route": [
                        {"when": "'URL' in outputs.classify", "to": "multi-search-engine"},
                        {"when": "'PDF' in outputs.classify", "to": "pdf-toolkit"},
                    ],
                },
            ],
        },
    )
    plan = parse_meta_plan(spec)
    assert plan is not None
    ingest = plan.steps[1]
    assert len(ingest.route) == 2
    assert ingest.route[0].to == "multi-search-engine"
    assert ingest.route[1].when.startswith("'PDF'")


def test_parser_rejects_malformed_route() -> None:
    spec = _make_meta_spec(
        composition={
            "steps": [
                {"id": "a", "skill": "x", "route": [{"when": "x"}]},  # missing 'to'
            ],
        },
    )
    with pytest.raises(MetaPlanError, match="missing non-empty 'to'"):
        parse_meta_plan(spec)


def test_parser_rejects_route_not_a_list() -> None:
    spec = _make_meta_spec(
        composition={
            "steps": [
                {"id": "a", "skill": "x", "route": "not-a-list"},
            ],
        },
    )
    with pytest.raises(MetaPlanError, match="route must be a list"):
        parse_meta_plan(spec)


def test_parser_accepts_step_when() -> None:
    spec = _make_meta_spec(
        composition={
            "steps": [
                {"id": "a", "skill": "x"},
                {
                    "id": "b",
                    "skill": "y",
                    "depends_on": ["a"],
                    "when": "outputs.a == 'RUN'",
                },
            ],
        },
    )
    plan = parse_meta_plan(spec)
    assert plan is not None
    assert plan.steps[1].when == "outputs.a == 'RUN'"


def test_parser_rejects_malformed_step_when() -> None:
    spec = _make_meta_spec(
        composition={
            "steps": [
                {"id": "a", "skill": "x", "when": []},
            ],
        },
    )
    with pytest.raises(MetaPlanError, match="when must be"):
        parse_meta_plan(spec)


def test_resolve_route_first_match_wins() -> None:
    cases = (
        RouteCase(when="'PDF' in outputs.classify", to="pdf-toolkit"),
        RouteCase(when="'URL' in outputs.classify", to="multi-search-engine"),
    )
    routed = resolve_route(cases, inputs={}, outputs={"classify": "PDF"})
    assert routed == "pdf-toolkit"


def test_resolve_route_no_match_returns_none() -> None:
    cases = (
        RouteCase(when="'PDF' in outputs.classify", to="pdf-toolkit"),
    )
    routed = resolve_route(cases, inputs={}, outputs={"classify": "TEXT"})
    assert routed is None


def test_resolve_route_empty_returns_none() -> None:
    assert resolve_route((), inputs={}, outputs={}) is None


def test_resolve_route_undefined_var_raises_value_error() -> None:
    cases = (RouteCase(when="outputs.does_not_exist == 'x'", to="anything"),)
    with pytest.raises(ValueError, match="undefined variable"):
        resolve_route(cases, inputs={}, outputs={})


@pytest.mark.asyncio
async def test_orchestrator_route_overrides_skill() -> None:
    spec = _make_meta_spec(
        composition={
            "steps": [
                {"id": "classify", "skill": "tagger", "with": {}},
                {
                    "id": "ingest",
                    "skill": "default-ingest",
                    "depends_on": ["classify"],
                    "route": [
                        {"when": "'URL' in outputs.classify", "to": "url-ingest"},
                        {"when": "'PDF' in outputs.classify", "to": "pdf-ingest"},
                    ],
                    "with": {},
                },
            ],
        },
    )
    plan = parse_meta_plan(spec)
    assert plan is not None
    loader = _FakeLoader(
        [
            _make_skill_spec("tagger", content="TAGGER"),
            _make_skill_spec("default-ingest", content="DEFAULT-INGEST"),
            _make_skill_spec("url-ingest", content="URL-INGEST"),
            _make_skill_spec("pdf-ingest", content="PDF-INGEST"),
        ],
    )

    call_log: list[str] = []

    async def stub_runner(system_prompt: str, _user: str) -> AsyncIterator[AgentEvent]:
        call_log.append(system_prompt)
        if "TAGGER" in system_prompt:
            yield TextDeltaEvent(text="URL")
        elif "URL-INGEST" in system_prompt:
            yield TextDeltaEvent(text="url-ingested")
        else:
            yield TextDeltaEvent(text="other")
        yield DoneEvent(text="")

    orch = MetaOrchestrator(agent_runner=stub_runner, skill_loader=loader)
    result = await orch.run(MetaMatch(plan=plan, inputs={"user_message": "go"}))

    assert result.ok, result.error
    assert result.step_outputs["classify"] == "URL"
    assert result.step_outputs["ingest"] == "url-ingested"
    # second invocation must be the routed-to skill, NOT default-ingest
    assert "URL-INGEST" in call_log[1]
    assert "DEFAULT-INGEST" not in call_log[1]


@pytest.mark.asyncio
async def test_orchestrator_route_fallthrough_uses_default_skill() -> None:
    spec = _make_meta_spec(
        composition={
            "steps": [
                {"id": "classify", "skill": "tagger", "with": {}},
                {
                    "id": "ingest",
                    "skill": "default-ingest",
                    "depends_on": ["classify"],
                    "route": [
                        {"when": "'URL' in outputs.classify", "to": "url-ingest"},
                    ],
                    "with": {},
                },
            ],
        },
    )
    plan = parse_meta_plan(spec)
    assert plan is not None
    loader = _FakeLoader(
        [
            _make_skill_spec("tagger", content="TAGGER"),
            _make_skill_spec("default-ingest", content="DEFAULT-INGEST"),
            _make_skill_spec("url-ingest", content="URL-INGEST"),
        ],
    )

    call_log: list[str] = []

    async def stub_runner(system_prompt: str, _user: str) -> AsyncIterator[AgentEvent]:
        call_log.append(system_prompt)
        if "TAGGER" in system_prompt:
            yield TextDeltaEvent(text="TEXT")  # no route case matches
        elif "DEFAULT-INGEST" in system_prompt:
            yield TextDeltaEvent(text="default-ingested")
        yield DoneEvent(text="")

    orch = MetaOrchestrator(agent_runner=stub_runner, skill_loader=loader)
    result = await orch.run(MetaMatch(plan=plan, inputs={"user_message": "go"}))

    assert result.ok, result.error
    assert result.step_outputs["ingest"] == "default-ingested"
    assert "DEFAULT-INGEST" in call_log[1]


# ---------------------------------------------------------------------------
# Step kind dispatch (llm_classify / tool_call)
# ---------------------------------------------------------------------------


def test_parser_llm_classify_requires_choices() -> None:
    spec = _make_meta_spec(
        composition={
            "steps": [
                {"id": "classify", "kind": "llm_classify", "with": {"text": "x"}},
            ],
        },
    )
    with pytest.raises(MetaPlanError, match="output_choices"):
        parse_meta_plan(spec)


def test_parser_llm_classify_accepts_with_choices() -> None:
    spec = _make_meta_spec(
        composition={
            "steps": [
                {
                    "id": "classify",
                    "kind": "llm_classify",
                    "output_choices": ["A", "B"],
                    "with": {"text": "x"},
                },
            ],
        },
    )
    plan = parse_meta_plan(spec)
    assert plan is not None
    assert plan.steps[0].kind == "llm_classify"
    assert plan.steps[0].output_choices == ("A", "B")
    # skill defaults to step id when not specified for non-agent kinds
    assert plan.steps[0].skill == "classify"


def test_parser_llm_chat_accepts_prompt_only_step() -> None:
    spec = _make_meta_spec(
        composition={
            "steps": [
                {
                    "id": "synthesize",
                    "kind": "llm_chat",
                    "with": {"task": "Summarize {{ inputs.user_message }}"},
                },
            ],
        },
    )
    plan = parse_meta_plan(spec)
    assert plan is not None
    assert plan.steps[0].kind == "llm_chat"
    assert plan.steps[0].skill == "synthesize"


def test_parser_llm_classify_rejects_duplicate_choices() -> None:
    spec = _make_meta_spec(
        composition={
            "steps": [
                {
                    "id": "c",
                    "kind": "llm_classify",
                    "output_choices": ["A", "A"],
                    "with": {"text": "x"},
                },
            ],
        },
    )
    with pytest.raises(MetaPlanError, match="unique"):
        parse_meta_plan(spec)


def test_parser_tool_call_requires_tool_name() -> None:
    spec = _make_meta_spec(
        composition={
            "steps": [
                {"id": "save", "kind": "tool_call", "tool_args": {"k": "v"}},
            ],
        },
    )
    with pytest.raises(MetaPlanError, match="tool"):
        parse_meta_plan(spec)


def test_parser_tool_call_accepts_full_spec() -> None:
    spec = _make_meta_spec(
        composition={
            "steps": [
                {
                    "id": "save",
                    "kind": "tool_call",
                    "tool": "memory_save",
                    "tool_args": {"content": "{{ inputs.user_message }}"},
                },
            ],
        },
    )
    plan = parse_meta_plan(spec)
    assert plan is not None
    assert plan.steps[0].kind == "tool_call"
    assert plan.steps[0].tool == "memory_save"
    assert plan.steps[0].tool_args == {"content": "{{ inputs.user_message }}"}


def test_parser_tool_allowlist_must_contain_tool() -> None:
    spec = _make_meta_spec(
        composition={
            "steps": [
                {
                    "id": "save",
                    "kind": "tool_call",
                    "tool": "exec_command",
                    "tool_allowlist": ["memory_save", "memory_search"],
                    "tool_args": {"command": "rm -rf /"},
                },
            ],
        },
    )
    with pytest.raises(MetaPlanError, match="not in tool_allowlist"):
        parse_meta_plan(spec)


def test_parser_tool_allowlist_accepts_matching_tool() -> None:
    spec = _make_meta_spec(
        composition={
            "steps": [
                {
                    "id": "save",
                    "kind": "tool_call",
                    "tool": "memory_save",
                    "tool_allowlist": ["memory_save"],
                    "tool_args": {"content": "x"},
                },
            ],
        },
    )
    plan = parse_meta_plan(spec)
    assert plan is not None
    assert plan.steps[0].tool_allowlist == ("memory_save",)


def test_parser_tool_allowlist_only_valid_for_tool_call() -> None:
    spec = _make_meta_spec(
        composition={
            "steps": [
                {
                    "id": "a",
                    "skill": "summarize",
                    "tool_allowlist": ["foo"],
                    "with": {},
                },
            ],
        },
    )
    with pytest.raises(MetaPlanError, match="tool_allowlist.*only valid"):
        parse_meta_plan(spec)


def test_parser_rejects_choices_on_non_classify_kind() -> None:
    spec = _make_meta_spec(
        composition={
            "steps": [
                {
                    "id": "a",
                    "skill": "summarize",
                    "output_choices": ["X"],
                    "with": {},
                },
            ],
        },
    )
    with pytest.raises(MetaPlanError, match="only valid for kind=llm_classify"):
        parse_meta_plan(spec)


def test_parser_rejects_tool_on_non_tool_call_kind() -> None:
    spec = _make_meta_spec(
        composition={
            "steps": [
                {
                    "id": "a",
                    "skill": "summarize",
                    "tool": "memory_save",
                    "with": {},
                },
            ],
        },
    )
    with pytest.raises(MetaPlanError, match="only valid for kind=tool_call"):
        parse_meta_plan(spec)


def test_parser_skill_exec_requires_skill() -> None:
    spec = _make_meta_spec(
        composition={
            "steps": [
                {"id": "ingest", "kind": "skill_exec", "with": {}},
            ],
        },
    )
    with pytest.raises(MetaPlanError, match="missing skill"):
        parse_meta_plan(spec)


def test_parser_skill_exec_accepts_full_spec() -> None:
    spec = _make_meta_spec(
        composition={
            "steps": [
                {
                    "id": "ingest",
                    "kind": "skill_exec",
                    "skill": "multi-search-engine",
                },
            ],
        },
    )
    plan = parse_meta_plan(spec)
    assert plan is not None
    assert plan.steps[0].kind == "skill_exec"
    assert plan.steps[0].skill == "multi-search-engine"


@pytest.mark.asyncio
async def test_orchestrator_skill_exec_invokes_subprocess(tmp_path: Path) -> None:
    """skill_exec must run the entrypoint as a real subprocess, no LLM."""

    # Synthesize a fake skill with a real entrypoint script that echoes its args.
    skill_dir = tmp_path / "fake_skill"
    skill_dir.mkdir()
    script = skill_dir / "echo.py"
    script.write_text(
        "import json\n"
        "import sys\n"
        "print(json.dumps({'argv': sys.argv[1:], 'ok': True}))\n",
        encoding="utf-8",
    )

    fake_spec = _make_skill_spec("fake-echo", content="echo me")
    fake_spec.base_dir = str(skill_dir)
    fake_spec.entrypoint = {
        "command": "python",
        "args": [
            "{baseDir}/echo.py",
            "--query",
            "{{ inputs.user_message }}",
            "--n",
            "{{ with.n }}",
        ],
        "parse": "json",
        "timeout": 15,
    }

    plan_spec = _make_meta_spec(
        composition={
            "steps": [
                {
                    "id": "run",
                    "kind": "skill_exec",
                    "skill": "fake-echo",
                    "with": {"n": "3"},
                },
            ],
        },
    )
    plan = parse_meta_plan(plan_spec)
    assert plan is not None

    async def explode_runner(_s: str, _u: str) -> AsyncIterator[AgentEvent]:
        raise AssertionError("skill_exec must not spawn a sub-Agent")
        yield  # pragma: no cover

    orch = MetaOrchestrator(
        agent_runner=explode_runner,
        skill_loader=_FakeLoader([fake_spec]),
    )
    result = await orch.run(MetaMatch(plan=plan, inputs={"user_message": "hello"}))

    assert result.ok, result.error
    import json as _json

    parsed = _json.loads(result.step_outputs["run"])
    assert parsed["ok"] is True
    assert parsed["argv"] == ["--query", "hello", "--n", "3"]


@pytest.mark.asyncio
async def test_orchestrator_skill_exec_propagates_nonzero_exit(tmp_path: Path) -> None:
    skill_dir = tmp_path / "fail_skill"
    skill_dir.mkdir()
    script = skill_dir / "fail.py"
    script.write_text(
        "import sys\n"
        "sys.stderr.write('boom\\n')\n"
        "raise SystemExit(7)\n",
        encoding="utf-8",
    )

    fake_spec = _make_skill_spec("fail-skill", content="x")
    fake_spec.base_dir = str(skill_dir)
    fake_spec.entrypoint = {"command": "python", "args": ["{baseDir}/fail.py"]}

    plan_spec = _make_meta_spec(
        composition={
            "steps": [
                {"id": "x", "kind": "skill_exec", "skill": "fail-skill"},
            ],
        },
    )
    plan = parse_meta_plan(plan_spec)
    assert plan is not None

    async def runner(_s: str, _u: str) -> AsyncIterator[AgentEvent]:
        raise AssertionError("no sub-Agent")
        yield  # pragma: no cover

    orch = MetaOrchestrator(
        agent_runner=runner,
        skill_loader=_FakeLoader([fake_spec]),
    )
    result = await orch.run(MetaMatch(plan=plan, inputs={}))

    assert result.ok is False
    assert result.failed_step_id == "x"
    assert result.error and "exited 7" in result.error
    assert "boom" in result.error


@pytest.mark.asyncio
async def test_orchestrator_skill_exec_rejects_cwd_outside_workspace(
    tmp_path: Path,
) -> None:
    skill_dir = tmp_path / "cwd_skill"
    skill_dir.mkdir()
    script = skill_dir / "echo_cwd.py"
    script.write_text("from pathlib import Path\nprint(Path.cwd())\n")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    fake_spec = _make_skill_spec("cwd-skill", content="x")
    fake_spec.base_dir = str(skill_dir)
    fake_spec.entrypoint = {
        "command": "python {baseDir}/echo_cwd.py",
        "cwd": str(outside),
    }

    plan_spec = _make_meta_spec(
        composition={
            "steps": [
                {"id": "x", "kind": "skill_exec", "skill": "cwd-skill"},
            ],
        },
    )
    plan = parse_meta_plan(plan_spec)
    assert plan is not None

    async def runner(_s: str, _u: str) -> AsyncIterator[AgentEvent]:
        raise AssertionError("no sub-Agent")
        yield  # pragma: no cover

    orch = MetaOrchestrator(
        agent_runner=runner,
        skill_loader=_FakeLoader([fake_spec]),
        workspace_dir=str(workspace),
    )
    result = await orch.run(MetaMatch(plan=plan, inputs={}))

    assert result.ok is False
    assert result.failed_step_id == "x"
    assert result.error and "cwd" in result.error
    assert result.error and "escapes allowed root" in result.error


@pytest.mark.asyncio
async def test_orchestrator_skill_exec_creates_missing_workspace_dir(
    tmp_path: Path,
) -> None:
    skill_dir = tmp_path / "cwd_skill"
    skill_dir.mkdir()
    script = skill_dir / "echo_cwd.py"
    script.write_text(
        "from pathlib import Path\n"
        "print(Path.cwd())\n",
        encoding="utf-8",
    )

    workspace = tmp_path / "missing" / "workspace"
    assert not workspace.exists()

    fake_spec = _make_skill_spec("cwd-skill", content="x")
    fake_spec.base_dir = str(skill_dir)
    fake_spec.entrypoint = {"command": "python {baseDir}/echo_cwd.py"}

    plan_spec = _make_meta_spec(
        composition={
            "steps": [
                {"id": "x", "kind": "skill_exec", "skill": "cwd-skill"},
            ],
        },
    )
    plan = parse_meta_plan(plan_spec)
    assert plan is not None

    async def runner(_s: str, _u: str) -> AsyncIterator[AgentEvent]:
        raise AssertionError("no sub-Agent")
        yield  # pragma: no cover

    orch = MetaOrchestrator(
        agent_runner=runner,
        skill_loader=_FakeLoader([fake_spec]),
        workspace_dir=str(workspace),
    )
    result = await orch.run(MetaMatch(plan=plan, inputs={}))

    assert result.ok, result.error
    assert workspace.exists()
    assert Path(result.step_outputs["x"]) == workspace


@pytest.mark.asyncio
async def test_orchestrator_skill_exec_requires_entrypoint() -> None:
    """A skill with no entrypoint manifest cannot run as skill_exec."""

    bare = _make_skill_spec("bare", content="no entrypoint here")
    # No bare.entrypoint set — defaults to None.

    plan_spec = _make_meta_spec(
        composition={
            "steps": [
                {"id": "x", "kind": "skill_exec", "skill": "bare"},
            ],
        },
    )
    plan = parse_meta_plan(plan_spec)
    assert plan is not None

    async def runner(_s: str, _u: str) -> AsyncIterator[AgentEvent]:
        raise AssertionError("no sub-Agent")
        yield  # pragma: no cover

    orch = MetaOrchestrator(agent_runner=runner, skill_loader=_FakeLoader([bare]))
    result = await orch.run(MetaMatch(plan=plan, inputs={}))

    assert result.ok is False
    assert result.failed_step_id == "x"
    assert result.error and "entrypoint manifest" in result.error


def test_parser_rejects_unknown_kind() -> None:
    spec = _make_meta_spec(
        composition={
            "steps": [
                {"id": "a", "kind": "python", "skill": "x"},
            ],
        },
    )
    with pytest.raises(MetaPlanError, match="kind="):
        parse_meta_plan(spec)


@pytest.mark.asyncio
async def test_orchestrator_llm_classify_uses_llm_chat_when_wired() -> None:
    spec = _make_meta_spec(
        composition={
            "steps": [
                {
                    "id": "classify",
                    "kind": "llm_classify",
                    "output_choices": ["URL", "PDF", "TEXT"],
                    "with": {"text": "Check: {{ inputs.user_message }}"},
                },
            ],
        },
    )
    plan = parse_meta_plan(spec)
    assert plan is not None
    loader = _FakeLoader([])

    chat_calls: list[tuple[str, str]] = []

    async def fake_chat(system_prompt: str, user_message: str) -> str:
        chat_calls.append((system_prompt, user_message))
        return "URL"

    async def explode_runner(_s: str, _u: str) -> AsyncIterator[AgentEvent]:
        # Must NOT be invoked when llm_chat is wired.
        raise AssertionError("agent runner must not be called for llm_classify")
        yield  # pragma: no cover — make this an async generator

    orch = MetaOrchestrator(
        agent_runner=explode_runner,
        skill_loader=loader,
        llm_chat=fake_chat,
    )
    result = await orch.run(MetaMatch(plan=plan, inputs={"user_message": "https://x"}))

    assert result.ok, result.error
    assert result.step_outputs["classify"] == "URL"
    assert len(chat_calls) == 1
    sys_prompt, user_msg = chat_calls[0]
    assert "URL | PDF | TEXT" in sys_prompt
    assert "https://x" in user_msg


@pytest.mark.asyncio
async def test_orchestrator_llm_chat_uses_single_llm_call_when_wired() -> None:
    spec = _make_meta_spec(
        composition={
            "steps": [
                {
                    "id": "synthesize",
                    "kind": "llm_chat",
                    "with": {
                        "system": "Write a compact report.",
                        "task": "Input: {{ inputs.user_message }}",
                    },
                },
            ],
        },
        final_text_mode="step:synthesize",
    )
    plan = parse_meta_plan(spec)
    assert plan is not None
    loader = _FakeLoader([])

    chat_calls: list[tuple[str, str]] = []

    async def fake_chat(system_prompt: str, user_message: str) -> str:
        chat_calls.append((system_prompt, user_message))
        return "compact report"

    async def explode_runner(_s: str, _u: str) -> AsyncIterator[AgentEvent]:
        raise AssertionError("agent runner must not be called for llm_chat")
        yield  # pragma: no cover

    orch = MetaOrchestrator(
        agent_runner=explode_runner,
        skill_loader=loader,
        llm_chat=fake_chat,
    )
    result = await orch.run(MetaMatch(plan=plan, inputs={"user_message": "x"}))

    assert result.ok, result.error
    assert result.final_text == "compact report"
    assert len(chat_calls) == 1
    assert chat_calls[0][0].startswith("Write a compact report.")
    assert "write final user-facing prose" in chat_calls[0][0]
    assert "English only" in chat_calls[0][0]
    assert chat_calls[0][1] == "Input: x"


@pytest.mark.asyncio
async def test_orchestrator_llm_chat_includes_rendered_context_args() -> None:
    spec = _make_meta_spec(
        composition={
            "steps": [
                {
                    "id": "diff",
                    "kind": "llm_chat",
                    "with": {"task": "Return a diff."},
                },
                {
                    "id": "summarize",
                    "kind": "llm_chat",
                    "depends_on": ["diff"],
                    "with": {
                        "task": "Summarize the upstream evidence.",
                        "upstream": "{{ outputs.diff | truncate(2000) }}",
                        "prior_outputs": {"diff": "{{ outputs.diff }}"},
                    },
                },
            ],
        },
        final_text_mode="step:summarize",
    )
    plan = parse_meta_plan(spec)
    assert plan is not None

    chat_calls: list[tuple[str, str]] = []

    async def fake_chat(system_prompt: str, user_message: str) -> str:
        chat_calls.append((system_prompt, user_message))
        if len(chat_calls) == 1:
            return "diff --git a/README.md"
        return "summary"

    async def explode_runner(_s: str, _u: str) -> AsyncIterator[AgentEvent]:
        raise AssertionError("agent runner must not be called for llm_chat")
        yield  # pragma: no cover

    orch = MetaOrchestrator(
        agent_runner=explode_runner,
        skill_loader=_FakeLoader([]),
        llm_chat=fake_chat,
    )
    result = await orch.run(MetaMatch(plan=plan, inputs={}))

    assert result.ok, result.error
    assert "Context:" in chat_calls[1][1]
    assert "upstream:\ndiff --git a/README.md" in chat_calls[1][1]
    assert '"diff": "diff --git a/README.md"' in chat_calls[1][1]


@pytest.mark.asyncio
async def test_meta_llm_chat_injects_english_language_guard_for_bilingual_templates() -> None:
    spec = _make_meta_spec(
        composition={
            "steps": [
                {
                    "id": "final",
                    "kind": "llm_chat",
                    "with": {
                        "system": "Write the final plan.",
                        "task": (
                            "Use these template headings if applicable: "
                            "\"Top 3 / 前三优先级\", \"Data limits / 数据限制\".\n"
                            "Request: {{ inputs.user_message }}"
                        ),
                    },
                },
            ],
        },
        final_text_mode="step:final",
    )
    plan = parse_meta_plan(spec)
    assert plan is not None
    chat_calls: list[tuple[str, str]] = []

    async def fake_chat(system_prompt: str, user_message: str) -> str:
        chat_calls.append((system_prompt, user_message))
        return "## Top 3\n\nData limits: only pasted context was used."

    async def explode_runner(_s: str, _u: str) -> AsyncIterator[AgentEvent]:
        raise AssertionError("agent runner must not be called for llm_chat")
        yield  # pragma: no cover

    orch = MetaOrchestrator(
        agent_runner=explode_runner,
        skill_loader=_FakeLoader([]),
        llm_chat=fake_chat,
    )
    result = await orch.run(
        MetaMatch(
            plan=plan,
            inputs=make_meta_inputs(
                user_message="Please make a daily brief for tomorrow.",
            ),
        ),
    )

    assert result.ok, result.error
    assert "前" not in result.final_text
    assert len(chat_calls) == 1
    assert "English only" in chat_calls[0][0]
    assert "Do not copy Chinese or bilingual headings" in chat_calls[0][0]
    assert "Top 3 / 前三优先级" in chat_calls[0][1]


@pytest.mark.asyncio
async def test_meta_agent_step_injects_language_guard_into_subagent_prompt() -> None:
    spec = _make_meta_spec(
        composition={
            "steps": [
                {
                    "id": "agent_step",
                    "skill": "skill_a",
                    "with": {
                        "text": "Use heading \"风险 / Risk\" if applicable.",
                    },
                },
            ],
        },
        final_text_mode="step:agent_step",
    )
    plan = parse_meta_plan(spec)
    assert plan is not None
    calls: list[tuple[str, str]] = []

    async def stub_runner(system_prompt: str, user_message: str) -> AsyncIterator[AgentEvent]:
        calls.append((system_prompt, user_message))
        yield TextDeltaEvent(text="Risk\n- Review the calendar.")
        yield DoneEvent(text="")

    orch = MetaOrchestrator(
        agent_runner=stub_runner,
        skill_loader=_FakeLoader([_make_skill_spec("skill_a", content="Skill body")]),
    )
    result = await orch.run(
        MetaMatch(
            plan=plan,
            inputs=make_meta_inputs(
                user_message="Please coordinate a household plan for Friday.",
            ),
        ),
    )

    assert result.ok, result.error
    assert result.final_text == "Risk\n- Review the calendar."
    assert len(calls) == 1
    assert "English only" in calls[0][0]
    assert "English only" in calls[0][1]


@pytest.mark.asyncio
async def test_meta_orchestrator_repairs_chinese_leakage_in_english_final_text() -> None:
    spec = _make_meta_spec(
        composition={
            "steps": [
                {
                    "id": "final",
                    "kind": "llm_chat",
                    "with": {
                        "system": "Write a short answer.",
                        "task": "Request: {{ inputs.user_message }}",
                    },
                },
            ],
        },
        final_text_mode="step:final",
    )
    plan = parse_meta_plan(spec)
    assert plan is not None
    chat_calls: list[tuple[str, str]] = []

    async def fake_chat(system_prompt: str, user_message: str) -> str:
        chat_calls.append((system_prompt, user_message))
        if "localization pass" in system_prompt:
            return "# Proposal Preview\n\nBasic information only."
        return "# 提案预览\n\n基本信息。"

    async def explode_runner(_s: str, _u: str) -> AsyncIterator[AgentEvent]:
        raise AssertionError("agent runner must not be called for llm_chat")
        yield  # pragma: no cover

    orch = MetaOrchestrator(
        agent_runner=explode_runner,
        skill_loader=_FakeLoader([]),
        llm_chat=fake_chat,
    )
    result = await orch.run(
        MetaMatch(
            plan=plan,
            inputs=make_meta_inputs(
                user_message="Please create a release readiness skill.",
            ),
        ),
    )

    assert result.ok, result.error
    assert result.final_text == "# Proposal Preview\n\nBasic information only."
    assert len(chat_calls) == 2
    assert "English only" in chat_calls[1][0]


@pytest.mark.asyncio
async def test_make_llm_chat_from_provider_uses_deliverable_sized_token_budget() -> None:
    from opensquilla.provider.types import (
        ProviderRequestCorrelation,
    )
    from opensquilla.provider.types import (
        TextDeltaEvent as ProviderTextDelta,
    )

    captured: dict[str, object] = {}

    class FakeProvider:
        async def chat(self, _messages, *, tools, config):
            assert tools is None
            captured["max_tokens"] = config.max_tokens
            captured["correlation"] = config.provider_request_correlation
            yield ProviderTextDelta(text="ok")

    root = ProviderRequestCorrelation(
        session_id="session-1",
        turn_id="turn-1",
        execution_id="meta-execution",
        call_kind="auxiliary.meta",
    )
    llm_chat = make_llm_chat_from_provider(
        provider=FakeProvider(),
        base_config=AgentConfig(model_id="fake"),
        provider_request_correlation=root,
    )

    assert await llm_chat("system", "user") == "ok"
    assert captured["max_tokens"] == 16384
    assert captured["correlation"] == root


@pytest.mark.asyncio
async def test_make_llm_chat_from_provider_derives_context_correlation() -> None:
    from opensquilla.provider.correlation_context import bind_provider_request_correlation
    from opensquilla.provider.types import (
        ProviderRequestCorrelation,
    )
    from opensquilla.provider.types import (
        TextDeltaEvent as ProviderTextDelta,
    )

    captured: dict[str, object] = {}

    class FakeProvider:
        async def chat(self, _messages, *, tools, config):
            captured["correlation"] = config.provider_request_correlation
            yield ProviderTextDelta(text="ok")

    root = ProviderRequestCorrelation(
        session_id="session-1",
        turn_id="turn-1",
        execution_id="root-execution",
        call_kind="agent.chat",
    )
    with bind_provider_request_correlation(root):
        llm_chat = make_llm_chat_from_provider(
            provider=FakeProvider(),
            base_config=AgentConfig(model_id="fake"),
        )
        assert await llm_chat("system", "user") == "ok"

    correlation = captured["correlation"]
    assert isinstance(correlation, ProviderRequestCorrelation)
    assert correlation.session_id == root.session_id
    assert correlation.turn_id == root.turn_id
    assert correlation.execution_id != root.execution_id
    assert correlation.call_kind == "auxiliary.meta"


@pytest.mark.asyncio
async def test_make_llm_chat_from_provider_forwards_billed_cost_to_usage_tracker() -> None:
    from opensquilla.engine.usage import UsageTracker, usage_scope
    from opensquilla.provider.types import DoneEvent as ProviderDoneEvent
    from opensquilla.provider.types import TextDeltaEvent as ProviderTextDelta

    class FakeProvider:
        async def chat(self, _messages, *, tools, config):
            assert tools is None
            assert config.temperature == 0.0
            yield ProviderTextDelta(text="ok")
            yield ProviderDoneEvent(
                input_tokens=10,
                output_tokens=2,
                model="deepseek/deepseek-v4-pro-20260423",
                billed_cost=0.123,
            )

    tracker = UsageTracker()
    llm_chat = make_llm_chat_from_provider(
        provider=FakeProvider(),
        base_config=AgentConfig(model_id="fallback-model"),
        usage_tracker=tracker,
        session_key="session-a",
    )

    with usage_scope("meta-run:step-a"):
        assert await llm_chat("system", "user") == "ok"

    usage = tracker.get("session-a")
    scoped = tracker.get_scope("session-a", "meta-run:step-a")
    assert usage is not None
    assert scoped is not None
    assert usage.input_tokens == 10
    assert usage.output_tokens == 2
    assert usage.billed_cost == pytest.approx(0.123)
    assert usage.total_cost == pytest.approx(0.123)
    assert scoped.billed_cost == pytest.approx(0.123)


@pytest.mark.asyncio
async def test_make_llm_chat_from_provider_stamps_provider_for_pricing() -> None:
    from opensquilla.engine.usage import UsageTracker
    from opensquilla.provider.types import DoneEvent as ProviderDoneEvent
    from opensquilla.provider.types import TextDeltaEvent as ProviderTextDelta

    class FakeProvider:
        async def chat(self, _messages, *, tools, config):
            yield ProviderTextDelta(text="label_a")
            yield ProviderDoneEvent(
                input_tokens=5000,
                output_tokens=800,
                model="qwen3:4b",
                billed_cost=0.0,
            )

    tracker = UsageTracker()
    llm_chat = make_llm_chat_from_provider(
        provider=FakeProvider(),
        base_config=AgentConfig(model_id="qwen3:4b", provider_id="ollama"),
        usage_tracker=tracker,
        session_key="session-a",
    )

    assert await llm_chat("system", "user") == "label_a"

    usage = tracker.get("session-a")
    assert usage is not None
    per_model = (usage._per_model or {}).get("qwen3:4b")
    assert per_model is not None
    # provider_id must reach pricing so a local model stays free instead of
    # falling through to the cloud default estimate.
    assert per_model.provider == "ollama"
    assert usage.cost == 0.0
    assert usage.total_cost == 0.0
    assert tracker.check_warning("session-a", threshold=0.02) is None


@pytest.mark.asyncio
async def test_orchestrator_final_text_auto_prepends_llm_summary_to_raw() -> None:
    """``final_text_mode='auto'`` (default) renders ``final_text`` as
    ``<LLM Markdown summary>\n\n---\n\n**Output details:**\n\n<raw last
    step output>``. The summary gives a scannable human cover sheet; the
    raw block underneath preserves IDs/paths/verdicts verbatim so the
    WebUI never loses the deliverable's concrete details."""
    spec = _make_meta_spec(
        composition={
            "steps": [
                {"id": "render", "skill": "summarize", "with": {"text": "x"}},
            ],
        },
        final_text_mode="auto",
    )
    plan = parse_meta_plan(spec)
    assert plan is not None
    skill_spec = _make_skill_spec("summarize", "Summarise input briefly.")
    loader = _FakeLoader([skill_spec])

    chat_calls: list[tuple[str, str]] = []

    async def fake_chat(system_prompt: str, user_message: str) -> str:
        chat_calls.append((system_prompt, user_message))
        return "✅ Meta-skill `meta-x` finished. See `out.txt`."

    async def stub_runner(_s: str, _u: str) -> AsyncIterator[AgentEvent]:
        yield TextDeltaEvent(text="raw-last-step-output-with-id-42abc")

    orch = MetaOrchestrator(
        agent_runner=stub_runner,
        skill_loader=loader,
        llm_chat=fake_chat,
    )
    result = await orch.run(MetaMatch(plan=plan, inputs={"user_message": "u"}))
    assert result.ok, result.error
    # final_text contains BOTH the summary (cover sheet) and the raw
    # deliverable (verbatim ID).
    assert result.final_text.startswith("✅")
    assert "**Output details:**" in result.final_text
    assert "raw-last-step-output-with-id-42abc" in result.final_text, (
        "raw last-step output must be preserved verbatim in final_text"
    )
    # exactly one llm_chat call (no llm_classify in this spec → only summary)
    assert len(chat_calls) == 1
    summary_system, summary_user = chat_calls[0]
    assert "Markdown summary" in summary_system
    assert "meta-x" in summary_user
    assert "raw-last-step-output-with-id-42abc" in summary_user


def test_output_contract_block_appends_stable_summary() -> None:
    text = _append_output_contract_block(
        "Decision: negotiate.",
        {
            "required_sections": ["Recommendation", "Evidence"],
            "assumptions": ["Criteria inferred from request"],
            "unverified": ["Live pricing was not checked"],
            "artifacts": [{"name": "decision_memo.md", "required": False}],
        },
    )

    assert text.startswith("Decision: negotiate.")
    for heading in ("声明的输出契约", "必需部分", "假设", "未验证", "生成的 artifact"):
        assert heading in text
    assert "已覆盖" not in text
    assert "Recommendation" in text
    assert "Criteria inferred from request" in text
    assert "Live pricing was not checked" in text
    assert "decision_memo.md" in text


def test_output_contract_block_reports_required_section_presence() -> None:
    text = _append_output_contract_block(
        "## Recommendation\nShip the narrow version.",
        {
            "required_sections": ["Recommendation", "Evidence"],
        },
    )

    assert "### 确定性检查" in text
    assert "- 已出现: Recommendation" in text
    assert "- 缺失: Evidence" in text


def test_output_contract_block_can_render_english_headings() -> None:
    text = _append_output_contract_block(
        "## Recommendation\nShip the narrow version.",
        {"required_sections": ["Recommendation"]},
        language="en",
    )

    assert "## Declared Output Contract" in text
    assert "### Required Sections" in text
    assert "### Deterministic Check" in text
    assert "- Present: Recommendation" in text
    assert "声明的输出契约" not in text


def test_output_contract_block_can_be_kept_out_of_user_visible_final_text() -> None:
    text = _append_output_contract_block(
        "## Recommendation\nNegotiate.",
        {
            "required_sections": ["Recommendation", "Evidence"],
            "append_to_final_text": False,
        },
    )

    assert text == "## Recommendation\nNegotiate."
    assert "确定性检查" not in text
    assert "Declared Output Contract" not in text


def test_output_contract_audit_reports_pass_warn_and_fail() -> None:
    passed = _audit_output_contract(
        "## Recommendation\nShip.\n\n## Evidence\nFact.",
        {"required_sections": ["Recommendation", "Evidence"]},
    )
    missing = _audit_output_contract(
        "## Recommendation\nShip.",
        {"required_sections": ["Recommendation", "Evidence"]},
    )
    forbidden = _audit_output_contract(
        "## Recommendation\nShip.\n\nNo risk exists.",
        {
            "required_sections": ["Recommendation"],
            "do_not": ["No risk exists"],
        },
    )

    assert passed["status"] == "pass"
    assert missing["status"] == "fail"
    assert missing["missing_required_sections"] == ["Evidence"]
    assert forbidden["status"] == "fail"
    assert forbidden["forbidden_terms_found"] == ["No risk exists"]


@pytest.mark.asyncio
async def test_orchestrator_result_metadata_includes_output_contract_audit() -> None:
    spec = _make_meta_spec(
        composition={
            "steps": [
                {"id": "final", "skill": "summarize"},
            ],
        },
        final_text_mode="raw",
        output_contract={"required_sections": ["Recommendation", "Evidence"]},
    )
    plan = parse_meta_plan(spec)
    assert plan is not None

    async def stub_runner(system_prompt: str, user_message: str) -> AsyncIterator[AgentEvent]:
        yield TextDeltaEvent(text="## Recommendation\nShip.")

    orch = MetaOrchestrator(
        skill_loader=_FakeLoader([_make_skill_spec("summarize", "")]),
        agent_runner=stub_runner,
    )
    result = await orch.run(MetaMatch(plan=plan, inputs={"user_message": "u"}))

    assert result.metadata["output_contract_audit"]["status"] == "fail"
    assert result.metadata["output_contract_audit"]["missing_required_sections"] == ["Evidence"]


def test_verify_declared_artifacts_reports_ok_empty_and_missing(tmp_path: Path) -> None:
    ok = tmp_path / "ok.md"
    empty = tmp_path / "empty.md"
    missing = tmp_path / "missing.md"
    ok.write_text("artifact body")
    empty.write_text("")

    report = _verify_declared_artifacts(
        {
            "artifacts": [
                {"name": "ok", "path": str(ok), "required": True},
                {"name": "empty", "path": str(empty), "required": True},
                {"name": "missing", "path": str(missing), "required": True},
            ],
        },
        workspace_dir=str(tmp_path),
    )

    by_name = {item["name"]: item for item in report["artifacts"]}
    assert report["status"] == "fail"
    assert by_name["ok"]["status"] == "ok"
    assert by_name["ok"]["size"] == len("artifact body")
    assert len(by_name["ok"]["sha256"]) == 64
    assert by_name["empty"]["status"] == "empty"
    assert by_name["missing"]["status"] == "missing"


@pytest.mark.asyncio
async def test_orchestrator_appends_artifact_verification_failure_notice(tmp_path: Path) -> None:
    missing = tmp_path / "missing.md"
    spec = _make_meta_spec(
        composition={
            "steps": [
                {"id": "final", "skill": "summarize"},
            ],
        },
        final_text_mode="raw",
        output_contract={
            "artifacts": [
                {"name": "decision memo", "path": str(missing), "required": True},
            ],
        },
    )
    plan = parse_meta_plan(spec)
    assert plan is not None

    async def stub_runner(system_prompt: str, user_message: str) -> AsyncIterator[AgentEvent]:
        yield TextDeltaEvent(text="## Recommendation\nShip.")

    orch = MetaOrchestrator(
        skill_loader=_FakeLoader([_make_skill_spec("summarize", "")]),
        agent_runner=stub_runner,
        workspace_dir=str(tmp_path),
    )
    result = await orch.run(MetaMatch(plan=plan, inputs={"user_message": "u"}))

    assert result.metadata["artifact_verification"]["status"] == "fail"
    assert "Artifact verification failed" in result.final_text
    assert "decision memo" in result.final_text


@pytest.mark.asyncio
async def test_orchestrator_final_text_raw_preserves_last_output() -> None:
    """``final_text_mode='raw'`` skips the summariser."""
    spec = _make_meta_spec(
        composition={
            "steps": [
                {"id": "render", "skill": "summarize", "with": {"text": "x"}},
            ],
        },
        final_text_mode="raw",
    )
    plan = parse_meta_plan(spec)
    assert plan is not None
    loader = _FakeLoader([_make_skill_spec("summarize", "")])

    chat_calls: list[tuple[str, str]] = []

    async def fake_chat(s: str, u: str) -> str:
        chat_calls.append((s, u))
        return "should-not-be-used"

    async def stub_runner(_s: str, _u: str) -> AsyncIterator[AgentEvent]:
        yield TextDeltaEvent(text="raw-deliverable")

    orch = MetaOrchestrator(
        agent_runner=stub_runner,
        skill_loader=loader,
        llm_chat=fake_chat,
    )
    result = await orch.run(MetaMatch(plan=plan, inputs={"user_message": "u"}))
    assert result.ok
    assert result.final_text == "raw-deliverable"
    assert chat_calls == []  # no summariser invocation


@pytest.mark.asyncio
async def test_orchestrator_final_text_step_picks_named_output() -> None:
    """``final_text_mode='step:<id>'`` picks a specific step output."""
    spec = _make_meta_spec(
        composition={
            "steps": [
                {"id": "first", "skill": "summarize", "with": {"text": "x"}},
                {"id": "second", "skill": "summarize", "depends_on": ["first"],
                 "with": {"text": "y"}},
            ],
        },
        final_text_mode="step:first",
    )
    plan = parse_meta_plan(spec)
    assert plan is not None
    loader = _FakeLoader([_make_skill_spec("summarize", "")])

    call_count = {"n": 0}

    async def numbered_runner(_s: str, _u: str) -> AsyncIterator[AgentEvent]:
        call_count["n"] += 1
        yield TextDeltaEvent(text=f"output-from-call-{call_count['n']}")

    orch = MetaOrchestrator(
        agent_runner=numbered_runner,
        skill_loader=loader,
        llm_chat=None,  # not needed; "step:" mode never calls llm_chat
    )
    result = await orch.run(MetaMatch(plan=plan, inputs={"user_message": "u"}))
    assert result.ok
    # first runs first → output-from-call-1; second runs second → call-2;
    # final_text should pick `first`, not the last step.
    assert result.final_text == "output-from-call-1"


@pytest.mark.asyncio
async def test_orchestrator_final_text_step_falls_back_when_named_output_empty() -> None:
    spec = _make_meta_spec(
        composition={
            "steps": [
                {
                    "id": "final_report",
                    "kind": "llm_chat",
                    "with": {"task": "write final"},
                },
                {
                    "id": "handoff",
                    "kind": "llm_chat",
                    "depends_on": ["final_report"],
                    "with": {"task": "write fallback"},
                },
            ],
        },
        final_text_mode="step:final_report",
    )
    plan = parse_meta_plan(spec)
    assert plan is not None
    loader = _FakeLoader([])

    calls = {"n": 0}

    async def fake_chat(_s: str, _u: str) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            return ""
        return "fallback handoff"

    async def explode_runner(_s: str, _u: str) -> AsyncIterator[AgentEvent]:
        raise AssertionError("agent runner must not be called for llm_chat")
        yield  # pragma: no cover

    orch = MetaOrchestrator(
        agent_runner=explode_runner,
        skill_loader=loader,
        llm_chat=fake_chat,
    )
    result = await orch.run(MetaMatch(plan=plan, inputs={"user_message": "u"}))

    assert result.ok, result.error
    assert result.final_text == "fallback handoff"


@pytest.mark.asyncio
async def test_orchestrator_final_text_step_falls_back_to_last_non_empty_output() -> None:
    spec = _make_meta_spec(
        composition={
            "steps": [
                {
                    "id": "preview",
                    "kind": "llm_chat",
                    "with": {"task": "write preview"},
                },
                {
                    "id": "final",
                    "kind": "llm_chat",
                    "depends_on": ["preview"],
                    "with": {"task": "write final"},
                },
                {
                    "id": "audit",
                    "kind": "llm_chat",
                    "depends_on": ["final"],
                    "with": {"task": "write audit"},
                },
            ],
        },
        final_text_mode="step:final",
    )
    plan = parse_meta_plan(spec)
    assert plan is not None
    loader = _FakeLoader([])

    replies = iter(["usable preview", "", ""])

    async def fake_chat(_s: str, _u: str) -> str:
        return next(replies)

    async def explode_runner(_s: str, _u: str) -> AsyncIterator[AgentEvent]:
        raise AssertionError("agent runner must not be called for llm_chat")
        yield  # pragma: no cover

    orch = MetaOrchestrator(
        agent_runner=explode_runner,
        skill_loader=loader,
        llm_chat=fake_chat,
    )
    result = await orch.run(MetaMatch(plan=plan, inputs={"user_message": "u"}))

    assert result.ok, result.error
    assert result.final_text == "usable preview"


@pytest.mark.asyncio
async def test_orchestrator_final_text_step_uses_visible_fallback_when_all_outputs_empty() -> None:
    spec = _make_meta_spec(
        name="meta-empty-final",
        composition={
            "steps": [
                {
                    "id": "final",
                    "kind": "llm_chat",
                    "with": {"task": "write final"},
                },
            ],
        },
        final_text_mode="step:final",
    )
    plan = parse_meta_plan(spec)
    assert plan is not None
    loader = _FakeLoader([])

    async def empty_chat(_s: str, _u: str) -> str:
        return ""

    async def explode_runner(_s: str, _u: str) -> AsyncIterator[AgentEvent]:
        raise AssertionError("agent runner must not be called for llm_chat")
        yield  # pragma: no cover

    orch = MetaOrchestrator(
        agent_runner=explode_runner,
        skill_loader=loader,
        llm_chat=empty_chat,
    )
    result = await orch.run(
        MetaMatch(
            plan=plan,
            inputs={
                "user_message": "请运行",
                "user_language": "zh",
            },
        ),
    )

    assert result.ok, result.error
    assert "meta-empty-final" in result.final_text
    assert "没有生成可展示的最终回答" in result.final_text


@pytest.mark.asyncio
async def test_orchestrator_skips_memory_step_when_persist_disabled() -> None:
    """``memory_persist_enabled=False`` short-circuits any ``skill: memory``
    step so exploratory turns don't pollute the long-term store. Downstream
    steps still see a placeholder so depends_on links survive."""
    spec = _make_meta_spec(
        composition={
            "steps": [
                {"id": "work", "skill": "summarize", "with": {"text": "x"}},
                {"id": "persist", "skill": "memory", "depends_on": ["work"],
                 "with": {"action": "save", "topic": "t", "content": "..."}},
            ],
        },
        final_text_mode="raw",  # avoid LLM summary noise
    )
    plan = parse_meta_plan(spec)
    assert plan is not None
    loader = _FakeLoader([
        _make_skill_spec("summarize"),
        _make_skill_spec("memory"),
    ])

    invoked_steps: list[str] = []

    async def stub_runner(_s: str, _u: str) -> AsyncIterator[AgentEvent]:
        invoked_steps.append("sub-agent")
        yield TextDeltaEvent(text="work-output")

    orch = MetaOrchestrator(
        agent_runner=stub_runner,
        skill_loader=loader,
        memory_persist_enabled=False,
    )
    result = await orch.run(MetaMatch(plan=plan, inputs={"user_message": "u"}))
    assert result.ok
    # Only the non-memory step should have spawned a sub-Agent.
    assert len(invoked_steps) == 1, invoked_steps
    # Memory step produces a placeholder so depends_on links remain valid.
    assert "skipped by config" in result.step_outputs["persist"]
    # Non-memory step output is preserved.
    assert result.step_outputs["work"] == "work-output"


@pytest.mark.asyncio
async def test_orchestrator_skips_tool_call_memory_save_when_persist_disabled() -> None:
    """The opt-out also covers the new ``kind: tool_call`` + ``tool: memory_save``
    form so the config switch is uniform across both wiring styles."""
    spec = _make_meta_spec(
        composition={
            "steps": [
                {"id": "work", "skill": "summarize", "with": {"text": "x"}},
                {"id": "persist",
                 "kind": "tool_call",
                 "tool": "memory_save",
                 "depends_on": ["work"],
                 "tool_args": {"path": "memory/t.md", "content": "..."}},
            ],
        },
        final_text_mode="raw",
    )
    plan = parse_meta_plan(spec)
    assert plan is not None
    loader = _FakeLoader([_make_skill_spec("summarize")])

    tool_calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_tool_invoker(tool_name: str, args: dict[str, Any]) -> str:
        tool_calls.append((tool_name, args))
        return "saved"

    async def stub_runner(_s: str, _u: str) -> AsyncIterator[AgentEvent]:
        yield TextDeltaEvent(text="work-output")

    orch = MetaOrchestrator(
        agent_runner=stub_runner,
        skill_loader=loader,
        tool_invoker=fake_tool_invoker,
        memory_persist_enabled=False,
    )
    result = await orch.run(MetaMatch(plan=plan, inputs={"user_message": "u"}))
    assert result.ok
    # memory_save tool should NOT have been invoked despite being a tool_call step.
    # (the sub-Agent step before it may call skill_view, but that's unrelated.)
    invoked_names = [name for name, _ in tool_calls]
    assert "memory_save" not in invoked_names, invoked_names
    assert "skipped by config" in result.step_outputs["persist"]


@pytest.mark.asyncio
async def test_orchestrator_runs_memory_step_when_persist_enabled() -> None:
    """Default ``memory_persist_enabled=True`` preserves legacy behaviour —
    memory steps execute normally."""
    spec = _make_meta_spec(
        composition={
            "steps": [
                {"id": "work", "skill": "summarize", "with": {"text": "x"}},
                {"id": "persist", "skill": "memory", "depends_on": ["work"],
                 "with": {"action": "save", "topic": "t", "content": "..."}},
            ],
        },
        final_text_mode="raw",
    )
    plan = parse_meta_plan(spec)
    assert plan is not None
    loader = _FakeLoader([
        _make_skill_spec("summarize"),
        _make_skill_spec("memory"),
    ])

    invoked: list[str] = []

    async def stub_runner(_s: str, _u: str) -> AsyncIterator[AgentEvent]:
        invoked.append("sub-agent")
        yield TextDeltaEvent(text=f"out-{len(invoked)}")

    orch = MetaOrchestrator(
        agent_runner=stub_runner,
        skill_loader=loader,
        # memory_persist_enabled defaults to True
    )
    result = await orch.run(MetaMatch(plan=plan, inputs={"user_message": "u"}))
    assert result.ok
    # Both steps spawn the sub-Agent (no skipping).
    assert len(invoked) == 2, invoked


@pytest.mark.asyncio
async def test_orchestrator_final_text_auto_falls_back_when_llm_missing() -> None:
    """``auto`` mode without an ``llm_chat`` instance preserves the
    scheduler-seeded text (degraded mode used by older tests / CLI)."""
    spec = _make_meta_spec(
        composition={
            "steps": [
                {"id": "render", "skill": "summarize", "with": {"text": "x"}},
            ],
        },
        final_text_mode="auto",
    )
    plan = parse_meta_plan(spec)
    assert plan is not None
    loader = _FakeLoader([_make_skill_spec("summarize", "")])

    async def stub_runner(_s: str, _u: str) -> AsyncIterator[AgentEvent]:
        yield TextDeltaEvent(text="fallback-deliverable")

    orch = MetaOrchestrator(
        agent_runner=stub_runner,
        skill_loader=loader,
        llm_chat=None,  # not wired
    )
    result = await orch.run(MetaMatch(plan=plan, inputs={"user_message": "u"}))
    assert result.ok
    assert result.final_text == "fallback-deliverable"


@pytest.mark.asyncio
async def test_orchestrator_llm_classify_coerces_noisy_reply() -> None:
    spec = _make_meta_spec(
        composition={
            "steps": [
                {
                    "id": "classify",
                    "kind": "llm_classify",
                    "output_choices": ["URL", "PDF", "GIT", "TEXT"],
                    "with": {"text": "{{ inputs.user_message }}"},
                },
            ],
        },
    )
    plan = parse_meta_plan(spec)
    assert plan is not None

    async def noisy_chat(_s: str, _u: str) -> str:
        return 'Answer: "URL".'

    async def explode_runner(_s: str, _u: str) -> AsyncIterator[AgentEvent]:
        raise AssertionError("should not run")
        yield  # pragma: no cover

    orch = MetaOrchestrator(
        agent_runner=explode_runner,
        skill_loader=_FakeLoader([]),
        llm_chat=noisy_chat,
    )
    result = await orch.run(MetaMatch(plan=plan, inputs={"user_message": "x"}))

    assert result.ok
    assert result.step_outputs["classify"] == "URL"


@pytest.mark.asyncio
async def test_orchestrator_llm_classify_repairs_ambiguous_reply_with_llm() -> None:
    spec = _make_meta_spec(
        composition={
            "steps": [
                {
                    "id": "classify",
                    "kind": "llm_classify",
                    "output_choices": ["A", "B"],
                    "with": {"text": "{{ inputs.user_message }}"},
                },
            ],
        },
    )
    plan = parse_meta_plan(spec)
    assert plan is not None

    chat_calls: list[tuple[str, str]] = []

    async def repairing_chat(system_prompt: str, user_message: str) -> str:
        chat_calls.append((system_prompt, user_message))
        if len(chat_calls) == 1:
            return "I cannot tell from the prompt"
        return "B"

    async def explode_runner(_s: str, _u: str) -> AsyncIterator[AgentEvent]:
        raise AssertionError("should not run")
        yield  # pragma: no cover

    orch = MetaOrchestrator(
        agent_runner=explode_runner,
        skill_loader=_FakeLoader([]),
        llm_chat=repairing_chat,
    )
    result = await orch.run(MetaMatch(plan=plan, inputs={"user_message": "x"}))

    assert result.ok
    assert result.step_outputs["classify"] == "B"
    assert len(chat_calls) == 2
    assert "repair classifier outputs" in chat_calls[1][0].lower()
    assert "I cannot tell" in chat_calls[1][1]


@pytest.mark.asyncio
async def test_orchestrator_llm_classify_falls_back_to_agent_runner() -> None:
    spec = _make_meta_spec(
        composition={
            "steps": [
                {
                    "id": "classify",
                    "kind": "llm_classify",
                    "output_choices": ["A", "B"],
                    "with": {"text": "x"},
                },
            ],
        },
    )
    plan = parse_meta_plan(spec)
    assert plan is not None
    runner_calls: list[tuple[str, str]] = []

    async def fallback_runner(system_prompt: str, user_message: str) -> AsyncIterator[AgentEvent]:
        runner_calls.append((system_prompt, user_message))
        yield TextDeltaEvent(text="B")
        yield DoneEvent(text="")

    orch = MetaOrchestrator(
        agent_runner=fallback_runner,
        skill_loader=_FakeLoader([]),
        llm_chat=None,  # no fast path → degraded mode
    )
    result = await orch.run(MetaMatch(plan=plan, inputs={"user_message": "x"}))

    assert result.ok
    assert result.step_outputs["classify"] == "B"
    assert len(runner_calls) == 1
    assert "EXACTLY ONE of: A | B" in runner_calls[0][0]


@pytest.mark.asyncio
async def test_orchestrator_tool_call_invokes_tool_directly() -> None:
    spec = _make_meta_spec(
        composition={
            "steps": [
                {
                    "id": "save",
                    "kind": "tool_call",
                    "tool": "memory_save",
                    "tool_args": {
                        "content": "Topic: {{ inputs.topic }}",
                        "mode": "append",
                    },
                },
            ],
        },
    )
    plan = parse_meta_plan(spec)
    assert plan is not None

    tool_calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_invoker(tool_name: str, args: dict[str, Any]) -> str:
        tool_calls.append((tool_name, args))
        return "saved to memory/2026-05-18.md"

    async def explode_runner(_s: str, _u: str) -> AsyncIterator[AgentEvent]:
        raise AssertionError("agent runner must not be called for tool_call")
        yield  # pragma: no cover

    orch = MetaOrchestrator(
        agent_runner=explode_runner,
        skill_loader=_FakeLoader([]),
        tool_invoker=fake_invoker,
    )
    result = await orch.run(MetaMatch(plan=plan, inputs={"topic": "kb"}))

    assert result.ok, result.error
    assert result.step_outputs["save"] == "saved to memory/2026-05-18.md"
    assert tool_calls == [
        ("memory_save", {"content": "Topic: kb", "mode": "append"}),
    ]


@pytest.mark.asyncio
async def test_orchestrator_tool_call_falls_back_to_agent_runner() -> None:
    spec = _make_meta_spec(
        composition={
            "steps": [
                {
                    "id": "save",
                    "kind": "tool_call",
                    "tool": "memory_save",
                    "tool_args": {"content": "hello"},
                },
            ],
        },
    )
    plan = parse_meta_plan(spec)
    assert plan is not None

    runner_calls: list[tuple[str, str]] = []

    async def fallback_runner(system_prompt: str, user_message: str) -> AsyncIterator[AgentEvent]:
        runner_calls.append((system_prompt, user_message))
        yield TextDeltaEvent(text="ok")
        yield DoneEvent(text="")

    orch = MetaOrchestrator(
        agent_runner=fallback_runner,
        skill_loader=_FakeLoader([]),
        tool_invoker=None,
    )
    result = await orch.run(MetaMatch(plan=plan, inputs={}))

    assert result.ok
    assert result.step_outputs["save"] == "ok"
    assert "memory_save" in runner_calls[0][0]
    assert '"content": "hello"' in runner_calls[0][1]


@pytest.mark.asyncio
async def test_orchestrator_mixed_kinds_pipeline() -> None:
    """End-to-end: llm_classify → agent → tool_call, with routing."""
    spec = _make_meta_spec(
        composition={
            "steps": [
                {
                    "id": "classify",
                    "kind": "llm_classify",
                    "output_choices": ["URL", "TEXT"],
                    "with": {"text": "{{ inputs.user_message }}"},
                },
                {
                    "id": "ingest",
                    "skill": "deep-research",
                    "depends_on": ["classify"],
                    "route": [
                        {"when": "'URL' in outputs.classify", "to": "fetch-url"},
                    ],
                    "with": {"q": "{{ inputs.user_message }}"},
                },
                {
                    "id": "save",
                    "kind": "tool_call",
                    "tool": "memory_save",
                    "depends_on": ["ingest"],
                    "tool_args": {"content": "{{ outputs.ingest }}"},
                },
            ],
        },
    )
    plan = parse_meta_plan(spec)
    assert plan is not None

    async def fake_chat(_s: str, _u: str) -> str:
        return "URL"

    saved: list[dict[str, Any]] = []

    async def fake_invoker(tool: str, args: dict[str, Any]) -> str:
        # skill_view is now called by the orchestrator as the real-tool
        # preface for every skill-loading step — handle it explicitly so the
        # mixed-pipeline assertion only inspects the actual save tool below.
        if tool == "skill_view":
            return f"REAL skill_view: {args['name']}"
        saved.append(args)
        return "saved-ok"

    async def runner(system_prompt: str, _u: str) -> AsyncIterator[AgentEvent]:
        if "FETCH-URL" in system_prompt:
            yield TextDeltaEvent(text="fetched-content")
        else:
            yield TextDeltaEvent(text="other")
        yield DoneEvent(text="")

    orch = MetaOrchestrator(
        agent_runner=runner,
        skill_loader=_FakeLoader(
            [
                _make_skill_spec("deep-research", content="DEEP-RESEARCH"),
                _make_skill_spec("fetch-url", content="FETCH-URL"),
            ],
        ),
        llm_chat=fake_chat,
        tool_invoker=fake_invoker,
    )
    result = await orch.run(MetaMatch(plan=plan, inputs={"user_message": "https://x"}))

    assert result.ok, result.error
    assert result.step_outputs["classify"] == "URL"
    assert result.step_outputs["ingest"] == "fetched-content"
    assert result.step_outputs["save"] == "saved-ok"
    assert saved == [{"content": "fetched-content"}]


def test_coerce_to_choice_helper() -> None:
    from opensquilla.skills.meta.orchestrator import _coerce_to_choice

    choices = ["URL", "PDF", "GIT", "TEXT"]
    assert _coerce_to_choice("URL", choices) == "URL"
    assert _coerce_to_choice('"URL"', choices) == "URL"
    assert _coerce_to_choice("Answer: URL.", choices) == "URL"
    assert _coerce_to_choice("url", choices) == "URL"  # case-insensitive
    assert _coerce_to_choice("the answer is GIT here", choices) == "GIT"
    # No match → return stripped raw
    assert _coerce_to_choice("definitely something else", choices) == "definitely something else"
    # Empty choices → identity (stripped)
    assert _coerce_to_choice("  hello  ", []) == "hello"


@pytest.mark.asyncio
async def test_iter_events_invokes_real_skill_view_for_skill_steps(
    tmp_path: Path,
) -> None:
    """Each skill_exec / agent step routes through the registered skill_view tool.

    The orchestrator must call ``self._tool_invoker("skill_view", {name: ...})``
    so the request goes through the parent's tool boundary (audit log, sandbox,
    usage tracking). The emitted ``ToolResultEvent`` carries whatever the tool
    actually returned — NOT a pre-computed SKILL.md preview.

    llm_classify and tool_call kinds do not load a SKILL.md, so they MUST NOT
    trigger skill_view.
    """

    from opensquilla.engine.types import ToolResultEvent, ToolUseStartEvent
    from opensquilla.skills.meta.types import MetaResult

    script = tmp_path / "echo.py"
    script.write_text(
        "import json\n"
        "print(json.dumps({'ok': True}))\n",
        encoding="utf-8",
    )
    exec_spec = _make_skill_spec("scripty", content="Run the wrapped CLI.")
    exec_spec.base_dir = str(tmp_path)
    exec_spec.entrypoint = {
        "command": "python",
        "args": ["{baseDir}/echo.py"],
        "parse": "json",
    }
    agent_spec = _make_skill_spec("brainy", content="Sub-agent skill body.")

    spec = _make_meta_spec(
        composition={
            "steps": [
                {
                    "id": "classify",
                    "kind": "llm_classify",
                    "output_choices": ["A"],
                    "with": {"text": "x"},
                },
                {
                    "id": "ingest",
                    "kind": "skill_exec",
                    "skill": "scripty",
                    "depends_on": ["classify"],
                },
                {
                    "id": "summarise",
                    "skill": "brainy",
                    "depends_on": ["ingest"],
                },
            ],
        },
    )
    plan = parse_meta_plan(spec)
    assert plan is not None

    invoker_calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_invoker(tool_name: str, args: dict[str, Any]) -> str:
        invoker_calls.append((tool_name, args))
        if tool_name == "skill_view":
            return f"REAL SKILL_VIEW OUTPUT for {args['name']}"
        return "unhandled-tool"

    async def chat(_s: str, _u: str) -> str:
        return "A"

    async def runner(_s: str, _u: str) -> AsyncIterator[AgentEvent]:
        yield TextDeltaEvent(text="summary")
        yield DoneEvent(text="")

    orch = MetaOrchestrator(
        agent_runner=runner,
        skill_loader=_FakeLoader([exec_spec, agent_spec]),
        llm_chat=chat,
        tool_invoker=fake_invoker,
    )

    skill_view_starts: list[ToolUseStartEvent] = []
    skill_view_results: list[ToolResultEvent] = []
    final: MetaResult | None = None
    async for ev in orch.iter_events(MetaMatch(plan=plan, inputs={"user_message": "x"})):
        if isinstance(ev, MetaResult):
            final = ev
        elif isinstance(ev, ToolUseStartEvent) and ev.tool_name == "skill_view":
            skill_view_starts.append(ev)
        elif isinstance(ev, ToolResultEvent) and ev.tool_name == "skill_view":
            skill_view_results.append(ev)

    assert final is not None and final.ok, final.error if final else "no result"
    # The orchestrator must have actually invoked skill_view via the tool
    # boundary, not synthesised the result locally.
    skill_view_invocations = [c for c in invoker_calls if c[0] == "skill_view"]
    assert skill_view_invocations == [
        ("skill_view", {"name": "scripty"}),
        ("skill_view", {"name": "brainy"}),
    ]
    assert len(skill_view_starts) == 2
    assert len(skill_view_results) == 2
    # Result is whatever the tool returned — not a SKILL.md preview.
    assert skill_view_results[0].result == "REAL SKILL_VIEW OUTPUT for scripty"
    assert skill_view_results[1].result == "REAL SKILL_VIEW OUTPUT for brainy"


@pytest.mark.asyncio
async def test_iter_events_skill_view_skipped_when_tool_invoker_absent() -> None:
    """Without a tool_invoker, the orchestrator skips the preface entirely
    rather than fabricating an event. Step execution still proceeds."""

    from opensquilla.engine.types import ToolResultEvent, ToolUseStartEvent
    from opensquilla.skills.meta.types import MetaResult

    spec = _make_meta_spec(
        composition={
            "steps": [
                {"id": "x", "skill": "brainy", "with": {}},
            ],
        },
    )
    plan = parse_meta_plan(spec)
    assert plan is not None

    async def runner(_s: str, _u: str) -> AsyncIterator[AgentEvent]:
        yield TextDeltaEvent(text="done")
        yield DoneEvent(text="")

    orch = MetaOrchestrator(
        agent_runner=runner,
        skill_loader=_FakeLoader([_make_skill_spec("brainy", content="B")]),
        tool_invoker=None,
    )

    saw_skill_view = False
    final: MetaResult | None = None
    async for ev in orch.iter_events(MetaMatch(plan=plan, inputs={})):
        if isinstance(ev, MetaResult):
            final = ev
        elif isinstance(ev, (ToolUseStartEvent, ToolResultEvent)):
            if ev.tool_name == "skill_view":
                saw_skill_view = True

    assert final is not None and final.ok
    assert saw_skill_view is False


@pytest.mark.asyncio
async def test_iter_events_skill_view_surfaces_tool_invoker_errors() -> None:
    """If skill_view raises, the orchestrator emits an error card and continues
    to the real step executor (which then surfaces its own canonical error)."""

    from opensquilla.engine.types import ToolResultEvent
    from opensquilla.skills.meta.types import MetaResult

    spec = _make_meta_spec(
        composition={
            "steps": [
                {"id": "x", "skill": "nope", "with": {}},
            ],
        },
    )
    plan = parse_meta_plan(spec)
    assert plan is not None

    async def boom_invoker(tool_name: str, args: dict[str, Any]) -> str:
        if tool_name == "skill_view":
            raise RuntimeError(f"skill_view: {args['name']!r} not found")
        return ""

    async def runner(_s: str, _u: str) -> AsyncIterator[AgentEvent]:
        raise AssertionError("loader fails first")
        yield  # pragma: no cover

    orch = MetaOrchestrator(
        agent_runner=runner,
        skill_loader=_FakeLoader([]),
        tool_invoker=boom_invoker,
    )

    skill_view_results: list[ToolResultEvent] = []
    final: MetaResult | None = None
    async for ev in orch.iter_events(MetaMatch(plan=plan, inputs={})):
        if isinstance(ev, MetaResult):
            final = ev
        elif isinstance(ev, ToolResultEvent) and ev.tool_name == "skill_view":
            skill_view_results.append(ev)

    assert final is not None and final.ok is False
    assert len(skill_view_results) == 1
    assert skill_view_results[0].is_error is True
    assert "not found" in skill_view_results[0].result


@pytest.mark.asyncio
async def test_iter_events_emits_step_boundaries() -> None:
    """Each step appears as a ToolUseStart + ToolResult pair so the UI can render it."""

    from opensquilla.engine.types import ToolResultEvent, ToolUseStartEvent
    from opensquilla.skills.meta.types import MetaResult

    spec = _make_meta_spec(
        composition={
            "steps": [
                {
                    "id": "classify",
                    "kind": "llm_classify",
                    "output_choices": ["A", "B"],
                    "with": {"text": "{{ inputs.user_message }}"},
                },
                {
                    "id": "save",
                    "kind": "tool_call",
                    "tool": "memory_save",
                    "depends_on": ["classify"],
                    "tool_args": {"content": "{{ outputs.classify }}"},
                },
            ],
        },
    )
    plan = parse_meta_plan(spec)
    assert plan is not None

    async def fake_chat(_s: str, _u: str) -> str:
        return "A"

    saved: list[dict[str, Any]] = []

    async def fake_invoker(_tool: str, args: dict[str, Any]) -> str:
        saved.append(args)
        return "saved"

    async def explode_runner(_s: str, _u: str) -> AsyncIterator[AgentEvent]:
        raise AssertionError("no sub-Agent should be spawned")
        yield  # pragma: no cover

    orch = MetaOrchestrator(
        agent_runner=explode_runner,
        skill_loader=_FakeLoader([]),
        llm_chat=fake_chat,
        tool_invoker=fake_invoker,
    )

    starts: list[ToolUseStartEvent] = []
    results: list[ToolResultEvent] = []
    final: MetaResult | None = None
    async for ev in orch.iter_events(MetaMatch(plan=plan, inputs={"user_message": "x"})):
        if isinstance(ev, MetaResult):
            final = ev
        elif isinstance(ev, ToolUseStartEvent):
            starts.append(ev)
        elif isinstance(ev, ToolResultEvent):
            results.append(ev)

    assert final is not None and final.ok
    assert [s.tool_name for s in starts] == ["meta-step:classify", "meta-step:save"]
    assert [r.tool_name for r in results] == ["meta-step:classify", "meta-step:save"]
    # Each result includes step metadata so the UI can label the card.
    classify_args = results[0].arguments or {}
    assert classify_args.get("kind") == "llm_classify"
    assert classify_args.get("skill") == "classify"
    save_args = results[1].arguments or {}
    assert save_args.get("kind") == "tool_call"
    # Results carry a preview of the step output.
    assert results[0].result == "A"
    assert results[1].result == "saved"


@pytest.mark.asyncio
async def test_iter_events_forwards_subagent_tool_events_but_folds_text() -> None:
    """For ``agent`` kind steps, sub-Agent's tool events stream through to the
    outer UI (so users see inner tool-call cards), but its TextDeltaEvent is
    folded into the parent meta-step:<id> card and surfaces only through the
    closing ToolResultEvent.result preview. Reduces UI noise for text-heavy
    skills (paper-section-author etc.). Design: docs/proposals/meta-skills/
    MECHANISM.md §17 single user-visible channel."""

    from opensquilla.engine.types import ToolResultEvent, ToolUseStartEvent
    from opensquilla.skills.meta.types import MetaResult

    spec = _make_meta_spec(
        composition={
            "steps": [
                {"id": "x", "skill": "deep-thinker", "with": {}},
            ],
        },
    )
    plan = parse_meta_plan(spec)
    assert plan is not None

    async def inner_runner(_s: str, _u: str) -> AsyncIterator[AgentEvent]:
        # Simulate a sub-Agent that calls skill_view then writes a summary.
        yield ToolUseStartEvent(tool_use_id="inner_1", tool_name="skill_view")
        yield ToolResultEvent(
            tool_use_id="inner_1",
            tool_name="skill_view",
            result="loaded SKILL.md content",
        )
        yield TextDeltaEvent(text="final answer is 42")
        yield DoneEvent(text="")

    orch = MetaOrchestrator(
        agent_runner=inner_runner,
        skill_loader=_FakeLoader([_make_skill_spec("deep-thinker", content="THINK")]),
    )

    forwarded_tool_names: list[str] = []
    text_chunks: list[str] = []
    step_close_previews: list[str] = []
    final: MetaResult | None = None
    async for ev in orch.iter_events(MetaMatch(plan=plan, inputs={})):
        if isinstance(ev, MetaResult):
            final = ev
        elif isinstance(ev, ToolUseStartEvent):
            forwarded_tool_names.append(ev.tool_name)
        elif isinstance(ev, TextDeltaEvent):
            text_chunks.append(ev.text)
        elif isinstance(ev, ToolResultEvent) and ev.tool_name.startswith("meta-step:"):
            step_close_previews.append(ev.result or "")

    assert final is not None and final.ok
    # Outer step boundary + inner skill_view both appear (nested cards visible).
    assert "meta-step:x" in forwarded_tool_names
    assert "skill_view" in forwarded_tool_names
    # Sub-Agent's TextDelta is NOT forwarded to outer stream — folded.
    assert "".join(text_chunks) == "", \
        f"sub-Agent TextDelta should not reach outer stream, got: {text_chunks!r}"
    # Final text shows up only in the meta-step closing card preview + MetaResult.
    assert any("final answer is 42" in p for p in step_close_previews), \
        f"final text should appear in step close preview, got: {step_close_previews!r}"
    assert "final answer is 42" in final.final_text


@pytest.mark.asyncio
async def test_paper_section_author_uses_llm_chat_without_subagent_tools() -> None:
    """paper-section-author is a text-only section writer.

    Running it as a full sub-Agent exposes shell/code tools, which can turn a
    single section into a long write/check/rewrite loop. When the orchestrator
    has an LLM-only dependency, this skill should bypass the sub-Agent runner.
    """

    spec = _make_meta_spec(
        composition={
            "steps": [
                {
                    "id": "abstract",
                    "kind": "agent",
                    "skill": "paper-section-author",
                    "with": {"task": "Write the abstract."},
                },
            ],
        },
    )
    plan = parse_meta_plan(spec)
    assert plan is not None

    async def explode_runner(_s: str, _u: str) -> AsyncIterator[AgentEvent]:
        raise AssertionError("paper-section-author must not use sub-Agent tools")
        if False:
            yield DoneEvent(text="")  # pragma: no cover

    llm_calls: list[tuple[str, str]] = []

    async def llm_chat(system_prompt: str, user_message: str) -> str:
        llm_calls.append((system_prompt, user_message))
        return "\\begin{abstract}\nFast section.\n\\end{abstract}"

    orch = MetaOrchestrator(
        agent_runner=explode_runner,
        skill_loader=_FakeLoader([
            _make_skill_spec("paper-section-author", content="SECTION BODY"),
        ]),
        llm_chat=llm_chat,
    )

    final: MetaResult | None = None
    async for ev in orch.iter_events(MetaMatch(plan=plan, inputs={})):
        if isinstance(ev, MetaResult):
            final = ev

    assert final is not None and final.ok
    assert len(llm_calls) == 1
    assert "SECTION BODY" in llm_calls[0][0]
    assert "\\begin{abstract}" in final.final_text


@pytest.mark.asyncio
async def test_iter_events_emits_error_result_on_step_failure() -> None:
    from opensquilla.engine.types import ToolResultEvent
    from opensquilla.skills.meta.types import MetaResult

    spec = _make_meta_spec(
        composition={
            "steps": [
                {"id": "broken", "skill": "missing-skill", "with": {}},
            ],
        },
    )
    plan = parse_meta_plan(spec)
    assert plan is not None

    async def runner(_s: str, _u: str) -> AsyncIterator[AgentEvent]:
        raise AssertionError("loader should fail before reaching runner")
        yield  # pragma: no cover

    orch = MetaOrchestrator(
        agent_runner=runner,
        skill_loader=_FakeLoader([]),  # missing-skill not registered
    )

    errored: list[ToolResultEvent] = []
    final: MetaResult | None = None
    async for ev in orch.iter_events(MetaMatch(plan=plan, inputs={})):
        if isinstance(ev, MetaResult):
            final = ev
        elif isinstance(ev, ToolResultEvent) and ev.is_error:
            errored.append(ev)

    assert final is not None
    assert final.ok is False
    assert len(errored) == 1
    assert "missing-skill" in errored[0].result


def test_expand_skill_placeholders_substitutes_basedir() -> None:
    from opensquilla.skills.meta.orchestrator import _expand_skill_placeholders

    spec = SkillSpec(
        name="multi-search-engine",
        description="d",
        layer=SkillLayer.BUNDLED,
        always=False,
        triggers=[],
        content="Run `python {baseDir}/scripts/search.py --query X`",
        kind="skill",
        base_dir="/opt/skills/multi-search-engine",
    )
    out = _expand_skill_placeholders(spec)
    assert "{baseDir}" not in out
    assert "/opt/skills/multi-search-engine/scripts/search.py" in out


def test_expand_skill_placeholders_no_base_dir_passes_through() -> None:
    from opensquilla.skills.meta.orchestrator import _expand_skill_placeholders

    spec = SkillSpec(
        name="bare",
        description="d",
        layer=SkillLayer.BUNDLED,
        always=False,
        triggers=[],
        content="Body with {baseDir} unresolved",
        kind="skill",
        base_dir="",
    )
    # Body unchanged when base_dir is empty.
    assert _expand_skill_placeholders(spec) == "Body with {baseDir} unresolved"


@pytest.mark.asyncio
async def test_drain_agent_runner_does_not_swallow_tool_errors() -> None:
    """A trailing error-result must surface as RuntimeError, not poison downstream steps."""

    from opensquilla.engine.types import ToolResultEvent

    spec = _make_meta_spec(
        composition={
            "steps": [
                {"id": "a", "skill": "broken", "with": {}},
            ],
        },
    )
    plan = parse_meta_plan(spec)
    assert plan is not None

    async def error_runner(_s: str, _u: str) -> AsyncIterator[AgentEvent]:
        # Sub-Agent calls a tool that errors; emits NO closing plain text.
        yield ToolResultEvent(
            tool_use_id="t1",
            tool_name="glob_search",
            result="No files matched pattern '**/broken/**'",
            is_error=True,
        )
        yield DoneEvent(text="")

    orch = MetaOrchestrator(
        agent_runner=error_runner,
        skill_loader=_FakeLoader([_make_skill_spec("broken", content="BROKEN")]),
    )
    result = await orch.run(MetaMatch(plan=plan, inputs={}))

    assert result.ok is False
    assert result.failed_step_id == "a"
    assert result.error and "no plain-text output" in result.error


@pytest.mark.asyncio
async def test_drain_agent_runner_fails_when_sub_agent_produces_no_text() -> None:
    """No plain text from sub-Agent ⇒ step fails — even if a tool returned OK.

    Tool output is not a substitute for the sub-Agent's plain-text deliverable;
    the SKILL.md prompt explicitly asks the sub-Agent to summarise. Promoting
    a tool result silently hides the case where the sub-Agent never wrote a
    summary and the printed bytes are unrelated noise.
    """

    from opensquilla.engine.types import ToolResultEvent

    spec = _make_meta_spec(
        composition={
            "steps": [
                {"id": "a", "skill": "ok-skill", "with": {}},
            ],
        },
    )
    plan = parse_meta_plan(spec)
    assert plan is not None

    async def silent_ok_runner(_s: str, _u: str) -> AsyncIterator[AgentEvent]:
        yield ToolResultEvent(
            tool_use_id="t1",
            tool_name="exec_command",
            result="exit_code=0\nsome_unrelated_output",
            is_error=False,
        )
        yield DoneEvent(text="")

    orch = MetaOrchestrator(
        agent_runner=silent_ok_runner,
        skill_loader=_FakeLoader([_make_skill_spec("ok-skill", content="OK-SKILL")]),
    )
    result = await orch.run(MetaMatch(plan=plan, inputs={}))

    assert result.ok is False
    assert result.failed_step_id == "a"


@pytest.mark.asyncio
async def test_drain_agent_runner_uses_done_event_text_when_deltas_absent() -> None:
    """Some providers surface final text only on DoneEvent; keep that output."""

    spec = _make_meta_spec(
        composition={
            "steps": [
                {"id": "a", "skill": "done-only", "with": {}},
            ],
        },
    )
    plan = parse_meta_plan(spec)
    assert plan is not None

    async def done_only_runner(_s: str, _u: str) -> AsyncIterator[AgentEvent]:
        yield DoneEvent(text="final answer from done")

    orch = MetaOrchestrator(
        agent_runner=done_only_runner,
        skill_loader=_FakeLoader([_make_skill_spec("done-only", content="DONE")]),
    )
    result = await orch.run(MetaMatch(plan=plan, inputs={}))

    assert result.ok is True
    assert result.step_outputs["a"] == "final answer from done"
