"""Unit + integration tests for the SOP→DAG compiler.

The compiler lives at ``src/opensquilla/skills/meta/sop_compiler.py``.
This file mirrors its four stages (lexer / parser / resolver / emitter)
plus integration and acceptance tests.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from opensquilla.skills.meta.parser import MetaPlanError

# ---------------------------------------------------------------------------
# Stage 0: Foundation types
# ---------------------------------------------------------------------------


def test_source_span_carries_line_and_excerpt() -> None:
    from opensquilla.skills.meta.sop_compiler import SourceSpan

    span = SourceSpan(
        start_line=42,
        start_col=4,
        end_line=42,
        end_col=20,
        excerpt="Run `multi-search-engine`",
    )
    assert span.start_line == 42
    assert "multi-search-engine" in span.excerpt


def test_sop_compile_error_is_meta_plan_error() -> None:
    """SOPCompileError is a MetaPlanError so the loader's existing failure
    path catches it via its current except clause."""
    from opensquilla.skills.meta.sop_compiler import SOPCompileError

    exc = SOPCompileError(
        skill_name="meta-x",
        phase_index=2,
        span=None,
        reason="example",
    )
    assert isinstance(exc, MetaPlanError)
    msg = str(exc)
    assert "meta-x" in msg
    assert "Phase 2" in msg or "phase 2" in msg.lower()
    assert "example" in msg


def test_sop_compile_error_renders_excerpt() -> None:
    from opensquilla.skills.meta.sop_compiler import SOPCompileError, SourceSpan

    span = SourceSpan(
        start_line=10,
        start_col=0,
        end_line=10,
        end_col=30,
        excerpt="- name: introduction",
    )
    exc = SOPCompileError(
        skill_name="meta-y",
        phase_index=5,
        span=span,
        reason="duplicate id",
    )
    msg = str(exc)
    assert "line 10" in msg
    assert "- name: introduction" in msg


# ---------------------------------------------------------------------------
# Stage 1: Lexer
# ---------------------------------------------------------------------------


def test_lex_recognizes_phase_heading() -> None:
    from opensquilla.skills.meta.sop_compiler import TokenType, _lex

    body = "## Phase 1: Search\n\nRun `multi-search-engine`. Save as `s`.\n"
    tokens = list(_lex(body))
    types = [t.type for t in tokens]
    assert TokenType.PHASE_HEADING in types
    heading = next(t for t in tokens if t.type == TokenType.PHASE_HEADING)
    assert heading.span.start_line == 1
    assert "Search" in heading.span.excerpt


def test_lex_recognizes_phase_heading_with_annotations() -> None:
    from opensquilla.skills.meta.sop_compiler import TokenType, _lex

    body = "## Phase 3: Drafting [parallel for_each: section]\n"
    tokens = list(_lex(body))
    heading = next(t for t in tokens if t.type == TokenType.PHASE_HEADING)
    # The lexer captures the full heading text; the parser splits title/annotations.
    assert "parallel for_each" in heading.span.excerpt


def test_lex_recognizes_invocation_and_with_bullets() -> None:
    from opensquilla.skills.meta.sop_compiler import TokenType, _lex

    body = (
        "## Phase 1: Search\n"
        "Invoke `paper-outline-author` with:\n"
        "- topic: `hello`\n"
        "- outline_format: markdown\n"
        "Save as `outline`.\n"
    )
    tokens = list(_lex(body))
    types = [t.type for t in tokens]
    assert TokenType.INVOCATION_LINE in types
    assert types.count(TokenType.WITH_BULLET) == 2
    assert TokenType.SAVE_AS_LINE in types


def test_lex_skips_fenced_code_blocks_except_for_each() -> None:
    from opensquilla.skills.meta.sop_compiler import TokenType, _lex

    body = (
        "## Phase 1: Search\n"
        "```python\n"
        "# This is just docs, ignore me\n"
        "## Phase 99: Not a heading\n"
        "```\n"
        "Run `s`. Save as `x`.\n"
    )
    tokens = list(_lex(body))
    # The fake "Phase 99" inside the python fence must NOT be a token
    phase_count = sum(1 for t in tokens if t.type == TokenType.PHASE_HEADING)
    assert phase_count == 1


def test_lex_captures_fenced_for_each_yaml() -> None:
    from opensquilla.skills.meta.sop_compiler import TokenType, _lex

    body = (
        "## Phase 5: Drafting [parallel for_each: section]\n"
        "```yaml for_each\n"
        "section:\n"
        "  - {id: a, name: A}\n"
        "  - {id: b, name: B}\n"
        "```\n"
        "Invoke `x` with:\n"
        "- y: z\n"
        "Save as `{{ section.id }}`.\n"
    )
    tokens = list(_lex(body))
    types = [t.type for t in tokens]
    assert TokenType.FENCED_YAML_FOR_EACH in types
    fey = next(t for t in tokens if t.type == TokenType.FENCED_YAML_FOR_EACH)
    # The whole inner YAML is preserved in the excerpt for the parser to load
    assert "section:" in fey.span.excerpt
    assert "id: a" in fey.span.excerpt


def test_lex_returns_source_spans_with_correct_line_numbers() -> None:
    from opensquilla.skills.meta.sop_compiler import TokenType, _lex

    body = (
        "Hello\n"               # line 1
        "\n"                     # line 2 (blank)
        "## Phase 1: Search\n"   # line 3
        "Run `s`. Save as `x`.\n"  # line 4
    )
    tokens = list(_lex(body))
    heading = next(t for t in tokens if t.type == TokenType.PHASE_HEADING)
    assert heading.span.start_line == 3
    invocation = next(t for t in tokens if t.type == TokenType.INVOCATION_LINE)
    assert invocation.span.start_line == 4


# ---------------------------------------------------------------------------
# Stage 2: Parser
# ---------------------------------------------------------------------------


def test_parse_single_sequential_phase() -> None:
    from opensquilla.skills.meta.sop_compiler import _lex, _parse

    body = (
        "## Phase 1: Search\n"
        "Run `multi-search-engine`. Save as `s`.\n"
    )
    doc = _parse(list(_lex(body)), skill_name="meta-x")
    assert len(doc.phases) == 1
    phase = doc.phases[0]
    assert phase.index == 1
    assert phase.title == "Search"
    assert phase.annotations == {}
    assert len(phase.invocations) == 1
    inv = phase.invocations[0]
    assert inv.skill_name == "multi-search-engine"
    assert inv.kind_hint is None
    assert inv.step_id_template == "s"
    assert inv.with_args == {}


def test_parse_invoke_with_kind_and_args() -> None:
    from opensquilla.skills.meta.sop_compiler import _lex, _parse

    body = (
        "## Phase 1: Outline\n"
        "Invoke `paper-outline-author` as agent with:\n"
        "- topic: `{{ inputs.user_message }}`\n"
        "- max_words: 200\n"
        "Save as `outline`.\n"
    )
    doc = _parse(list(_lex(body)), skill_name="meta-x")
    inv = doc.phases[0].invocations[0]
    assert inv.skill_name == "paper-outline-author"
    assert inv.kind_hint == "agent"
    assert inv.with_args["topic"] == "`{{ inputs.user_message }}`"
    assert inv.with_args["max_words"] == "200"


def test_parse_parallel_annotation() -> None:
    from opensquilla.skills.meta.sop_compiler import _lex, _parse

    body = (
        "## Phase 1: First\n"
        "Run `a`. Save as `s1`.\n"
        "## Phase 2: Foundation [parallel]\n"
        "Run `b`. Save as `s2`.\n"
        "Run `c`. Save as `s3`.\n"
    )
    doc = _parse(list(_lex(body)), skill_name="meta-x")
    p2 = doc.phases[1]
    assert "parallel" in p2.annotations
    assert len(p2.invocations) == 2


def test_parse_for_each_annotation_with_items() -> None:
    from opensquilla.skills.meta.sop_compiler import _lex, _parse

    body = (
        "## Phase 5: Drafting [parallel for_each: section]\n"
        "```yaml for_each\n"
        "section:\n"
        "  - {id: a, name: A}\n"
        "  - {id: b, name: B, extra: X}\n"
        "```\n"
        "Invoke `paper-section-author` with:\n"
        "- section: `{{ section.name }}`\n"
        "Save as `{{ section.id }}`.\n"
    )
    doc = _parse(list(_lex(body)), skill_name="meta-x")
    p = doc.phases[0]
    assert p.annotations.get("parallel for_each") == "section"
    assert p.for_each_var == "section"
    assert len(p.for_each_items) == 2
    assert p.for_each_items[0] == {"id": "a", "name": "A"}
    assert p.for_each_items[1] == {"id": "b", "name": "B", "extra": "X"}


def test_parse_depends_on_annotation_single() -> None:
    from opensquilla.skills.meta.sop_compiler import _lex, _parse

    body = (
        "## Phase 1: A\n"
        "Run `s`. Save as `a`.\n"
        "## Phase 2: B [depends_on: a]\n"
        "Run `s`. Save as `b`.\n"
    )
    doc = _parse(list(_lex(body)), skill_name="meta-x")
    p2 = doc.phases[1]
    assert p2.annotations.get("depends_on") == "a"


def test_parse_when_annotation_rejected() -> None:
    from opensquilla.skills.meta.sop_compiler import SOPCompileError, _lex, _parse

    body = (
        "## Phase 1: A [when: outputs.x == 'yes']\n"
        "Run `s`. Save as `a`.\n"
    )
    with pytest.raises(SOPCompileError, match="not in MVP scope"):
        _parse(list(_lex(body)), skill_name="meta-x")


def test_parse_missing_save_as_rejected() -> None:
    from opensquilla.skills.meta.sop_compiler import SOPCompileError, _lex, _parse

    body = (
        "## Phase 1: A\n"
        "Run `s`.\n"
        "## Phase 2: B\n"
        "Run `t`. Save as `b`.\n"
    )
    with pytest.raises(SOPCompileError, match="Save as"):
        _parse(list(_lex(body)), skill_name="meta-x")


def test_parse_stdin_prose_rejected() -> None:
    from opensquilla.skills.meta.sop_compiler import SOPCompileError, _lex, _parse

    body = (
        "## Phase 1: Refbib\n"
        "Run `paper-refbib-stub`. Pipe outputs.search to stdin. Save as `r`.\n"
    )
    with pytest.raises(SOPCompileError, match="stdin"):
        _parse(list(_lex(body)), skill_name="meta-x")


# ---------------------------------------------------------------------------
# Stage 3: Resolver
# ---------------------------------------------------------------------------


class _StubSkillLoader:
    """Minimal SkillLoader stub for resolver tests.

    Real SkillLoader is heavy; resolver only needs ``get_by_name``.
    """

    def __init__(self, specs: dict[str, dict[str, Any]]) -> None:
        self._specs = specs

    def get_by_name(self, name: str) -> Any:
        spec = self._specs.get(name)
        if spec is None:
            return None
        from opensquilla.skills.types import SkillLayer, SkillSpec

        return SkillSpec(
            name=name,
            description=f"{name} description",
            layer=SkillLayer.BUNDLED,
            always=False,
            triggers=[],
            content="",
            kind="skill",
            entrypoint=spec.get("entrypoint"),
        )


def test_resolve_kind_skill_exec_when_skill_has_entrypoint() -> None:
    from opensquilla.skills.meta.sop_compiler import (
        SOPInvocation,
        SourceSpan,
        _resolve_kind,
    )

    inv = SOPInvocation(
        skill_name="multi-search-engine",
        kind_hint=None,
        with_args={},
        step_id_template="s",
        span=SourceSpan(1, 0, 1, 10, ""),
    )
    loader = _StubSkillLoader({"multi-search-engine": {"entrypoint": {"command": "x"}}})
    result = _resolve_kind(inv, skill_loader=loader, skill_name="meta-x", phase_index=1)
    assert result == "skill_exec"


def test_resolve_kind_agent_when_no_entrypoint() -> None:
    from opensquilla.skills.meta.sop_compiler import (
        SOPInvocation,
        SourceSpan,
        _resolve_kind,
    )

    inv = SOPInvocation(
        skill_name="paper-outline-author",
        kind_hint=None,
        with_args={},
        step_id_template="o",
        span=SourceSpan(1, 0, 1, 10, ""),
    )
    loader = _StubSkillLoader({"paper-outline-author": {}})
    assert _resolve_kind(inv, skill_loader=loader, skill_name="meta-x", phase_index=1) == "agent"


def test_resolve_kind_explicit_as_agent_overrides_entrypoint() -> None:
    from opensquilla.skills.meta.sop_compiler import (
        SOPInvocation,
        SourceSpan,
        _resolve_kind,
    )

    inv = SOPInvocation(
        skill_name="multi-search-engine",
        kind_hint="agent",  # explicit override
        with_args={},
        step_id_template="s",
        span=SourceSpan(1, 0, 1, 10, ""),
    )
    loader = _StubSkillLoader({"multi-search-engine": {"entrypoint": {"command": "x"}}})
    assert _resolve_kind(inv, skill_loader=loader, skill_name="meta-x", phase_index=1) == "agent"


def test_resolve_kind_unknown_skill_raises() -> None:
    from opensquilla.skills.meta.sop_compiler import (
        SOPCompileError,
        SOPInvocation,
        SourceSpan,
        _resolve_kind,
    )

    inv = SOPInvocation(
        skill_name="nonexistent-skill",
        kind_hint=None,
        with_args={},
        step_id_template="x",
        span=SourceSpan(5, 0, 5, 30, "Run `nonexistent-skill`. Save as `x`."),
    )
    loader = _StubSkillLoader({})
    with pytest.raises(SOPCompileError, match="not registered"):
        _resolve_kind(inv, skill_loader=loader, skill_name="meta-x", phase_index=1)


# ---------------------------------------------------------------------------
# Stage 4: Emitter (sequential)
# ---------------------------------------------------------------------------


def test_emit_sequential_phases_default_depends_on() -> None:
    from opensquilla.skills.meta.sop_compiler import _emit, _lex, _parse

    body = (
        "## Phase 1: First\n"
        "Run `paper-experiment-stub`. Save as `s1`.\n"
        "## Phase 2: Second\n"
        "Run `paper-plot-stub`. Save as `s2`.\n"
        "## Phase 3: Third\n"
        "Invoke `paper-outline-author` as agent with:\n"
        "- topic: `t`\n"
        "Save as `s3`.\n"
    )
    doc = _parse(list(_lex(body)), skill_name="meta-x")
    loader = _StubSkillLoader(
        {
            "paper-experiment-stub": {"entrypoint": {"command": "x"}},
            "paper-plot-stub": {"entrypoint": {"command": "x"}},
            "paper-outline-author": {},
        },
    )
    composition = _emit(doc, skill_loader=loader, skill_name="meta-x")
    steps = composition["steps"]
    assert [s["id"] for s in steps] == ["s1", "s2", "s3"]
    assert steps[0].get("depends_on", []) == []
    assert steps[1]["depends_on"] == ["s1"]
    assert steps[2]["depends_on"] == ["s2"]
    assert steps[0]["kind"] == "skill_exec"
    assert steps[2]["kind"] == "agent"
    assert steps[2]["with"] == {"topic": "`t`"} or steps[2]["with"] == {"topic": "t"}


def test_emit_parallel_phase_yields_sibling_steps() -> None:
    from opensquilla.skills.meta.sop_compiler import _emit, _lex, _parse

    body = (
        "## Phase 1: First\n"
        "Run `paper-experiment-stub`. Save as `s1`.\n"
        "## Phase 2: Foundation [parallel]\n"
        "Run `paper-plot-stub`. Save as `s2a`.\n"
        "Run `paper-refbib-stub`. Save as `s2b`.\n"
        "## Phase 3: Third\n"
        "Run `paper-plot-stub`. Save as `s3`.\n"
    )
    doc = _parse(list(_lex(body)), skill_name="meta-x")
    loader = _StubSkillLoader(
        {
            "paper-experiment-stub": {"entrypoint": {"command": "x"}},
            "paper-plot-stub": {"entrypoint": {"command": "x"}},
            "paper-refbib-stub": {"entrypoint": {"command": "x"}},
        },
    )
    composition = _emit(doc, skill_loader=loader, skill_name="meta-x")
    steps = {s["id"]: s for s in composition["steps"]}
    # s2a and s2b are siblings: both depend on s1, neither on each other
    assert steps["s2a"]["depends_on"] == ["s1"]
    assert steps["s2b"]["depends_on"] == ["s1"]
    # s3 depends on both s2 steps
    assert set(steps["s3"]["depends_on"]) == {"s2a", "s2b"}


def test_emit_for_each_expands_into_n_steps_with_loop_substitution() -> None:
    from opensquilla.skills.meta.sop_compiler import _emit, _lex, _parse

    body = (
        "## Phase 1: First\n"
        "Run `paper-experiment-stub`. Save as `s1`.\n"
        "## Phase 2: Drafting [parallel for_each: section]\n"
        "```yaml for_each\n"
        "section:\n"
        "  - {id: draft_a, name: abstract}\n"
        "  - {id: draft_b, name: introduction}\n"
        "```\n"
        "Invoke `paper-outline-author` with:\n"
        "- section: `{{ section.name }}`\n"
        "- outline: `{{ outputs.s1 }}`\n"
        "Save as `{{ section.id }}`.\n"
    )
    doc = _parse(list(_lex(body)), skill_name="meta-x")
    loader = _StubSkillLoader(
        {
            "paper-experiment-stub": {"entrypoint": {"command": "x"}},
            "paper-outline-author": {},
        },
    )
    composition = _emit(doc, skill_loader=loader, skill_name="meta-x")
    steps = {s["id"]: s for s in composition["steps"]}
    assert set(steps.keys()) == {"s1", "draft_a", "draft_b"}
    # Loop substitution happens at compile time: section.name → literal string
    assert steps["draft_a"]["with"]["section"] == "abstract"
    assert steps["draft_b"]["with"]["section"] == "introduction"
    # Runtime templates pass through unchanged
    assert steps["draft_a"]["with"]["outline"] == "{{ outputs.s1 }}"
    assert steps["draft_b"]["with"]["outline"] == "{{ outputs.s1 }}"
    # Both drafts depend on the previous phase
    assert steps["draft_a"]["depends_on"] == ["s1"]
    assert steps["draft_b"]["depends_on"] == ["s1"]


def test_emit_for_each_field_omission_drops_missing_keys() -> None:
    """The acceptance case: figure_path is only emitted for items that
    define it. Other items have NO figure_path key in their with_args."""
    from opensquilla.skills.meta.sop_compiler import _emit, _lex, _parse

    body = (
        "## Phase 1: Drafting [parallel for_each: section]\n"
        "```yaml for_each\n"
        "section:\n"
        "  - {id: draft_a, name: abstract}\n"
        "  - {id: draft_b, name: results, figure_path: paper/figure_1.pdf}\n"
        "```\n"
        "Invoke `paper-outline-author` with:\n"
        "- section: `{{ section.name }}`\n"
        "- figure_path: `{{ section.figure_path }}`\n"
        "Save as `{{ section.id }}`.\n"
    )
    doc = _parse(list(_lex(body)), skill_name="meta-x")
    loader = _StubSkillLoader({"paper-outline-author": {}})
    composition = _emit(doc, skill_loader=loader, skill_name="meta-x")
    steps = {s["id"]: s for s in composition["steps"]}
    # draft_a does NOT define figure_path → key dropped
    assert "figure_path" not in steps["draft_a"]["with"]
    assert steps["draft_a"]["with"]["section"] == "abstract"
    # draft_b defines figure_path → emitted with the literal value
    assert steps["draft_b"]["with"]["figure_path"] == "paper/figure_1.pdf"
    assert steps["draft_b"]["with"]["section"] == "results"


def test_emit_for_each_mixed_template_rejected() -> None:
    """A bullet that mixes a missing loop-var ref with other content must
    raise — silent emission with empty interpolation would be confusing."""
    from opensquilla.skills.meta.sop_compiler import SOPCompileError, _emit, _lex, _parse

    body = (
        "## Phase 1: Drafting [parallel for_each: section]\n"
        "```yaml for_each\n"
        "section:\n"
        "  - {id: draft_a, name: abstract}\n"
        "```\n"
        "Invoke `paper-outline-author` with:\n"
        "- caption: `Figure for {{ section.figure_path }}`\n"
        "Save as `{{ section.id }}`.\n"
    )
    doc = _parse(list(_lex(body)), skill_name="meta-x")
    loader = _StubSkillLoader({"paper-outline-author": {}})
    with pytest.raises(SOPCompileError, match="missing"):
        _emit(doc, skill_loader=loader, skill_name="meta-x")


def test_emit_for_each_duplicate_generated_id_rejected() -> None:
    """If loop expansion produces a step id that clashes with an existing
    step elsewhere, raise with a clear cross-reference."""
    from opensquilla.skills.meta.sop_compiler import SOPCompileError, _emit, _lex, _parse

    body = (
        "## Phase 1: First\n"
        "Run `paper-experiment-stub`. Save as `draft_a`.\n"
        "## Phase 2: Drafting [parallel for_each: section]\n"
        "```yaml for_each\n"
        "section:\n"
        "  - {id: draft_a, name: abstract}\n"
        "```\n"
        "Invoke `paper-outline-author` with:\n"
        "- section: `{{ section.name }}`\n"
        "Save as `{{ section.id }}`.\n"
    )
    doc = _parse(list(_lex(body)), skill_name="meta-x")
    loader = _StubSkillLoader(
        {
            "paper-experiment-stub": {"entrypoint": {"command": "x"}},
            "paper-outline-author": {},
        },
    )
    with pytest.raises(SOPCompileError, match="duplicate"):
        _emit(doc, skill_loader=loader, skill_name="meta-x")


def test_emit_depends_on_single_id_override() -> None:
    from opensquilla.skills.meta.sop_compiler import _emit, _lex, _parse

    body = (
        "## Phase 1: A\n"
        "Run `paper-experiment-stub`. Save as `a`.\n"
        "## Phase 2: B\n"
        "Run `paper-plot-stub`. Save as `b`.\n"
        "## Phase 3: C [depends_on: a]\n"
        "Run `paper-refbib-stub`. Save as `c`.\n"
    )
    doc = _parse(list(_lex(body)), skill_name="meta-x")
    loader = _StubSkillLoader(
        {
            "paper-experiment-stub": {"entrypoint": {"command": "x"}},
            "paper-plot-stub": {"entrypoint": {"command": "x"}},
            "paper-refbib-stub": {"entrypoint": {"command": "x"}},
        },
    )
    composition = _emit(doc, skill_loader=loader, skill_name="meta-x")
    steps = {s["id"]: s for s in composition["steps"]}
    assert steps["c"]["depends_on"] == ["a"]


def test_emit_depends_on_list_override() -> None:
    from opensquilla.skills.meta.sop_compiler import _emit, _lex, _parse

    body = (
        "## Phase 1: A\n"
        "Run `paper-experiment-stub`. Save as `a`.\n"
        "## Phase 2: B\n"
        "Run `paper-plot-stub`. Save as `b`.\n"
        "## Phase 3: C [depends_on: [a, b]]\n"
        "Run `paper-refbib-stub`. Save as `c`.\n"
    )
    doc = _parse(list(_lex(body)), skill_name="meta-x")
    loader = _StubSkillLoader(
        {
            "paper-experiment-stub": {"entrypoint": {"command": "x"}},
            "paper-plot-stub": {"entrypoint": {"command": "x"}},
            "paper-refbib-stub": {"entrypoint": {"command": "x"}},
        },
    )
    composition = _emit(doc, skill_loader=loader, skill_name="meta-x")
    steps = {s["id"]: s for s in composition["steps"]}
    assert set(steps["c"]["depends_on"]) == {"a", "b"}


def test_emit_depends_on_unknown_id_rejected() -> None:
    from opensquilla.skills.meta.sop_compiler import SOPCompileError, _emit, _lex, _parse

    body = (
        "## Phase 1: A\n"
        "Run `paper-experiment-stub`. Save as `a`.\n"
        "## Phase 2: B [depends_on: nonexistent]\n"
        "Run `paper-plot-stub`. Save as `b`.\n"
    )
    doc = _parse(list(_lex(body)), skill_name="meta-x")
    loader = _StubSkillLoader(
        {
            "paper-experiment-stub": {"entrypoint": {"command": "x"}},
            "paper-plot-stub": {"entrypoint": {"command": "x"}},
        },
    )
    with pytest.raises(SOPCompileError, match="nonexistent"):
        _emit(doc, skill_loader=loader, skill_name="meta-x")


# ---------------------------------------------------------------------------
# Public compile() API
# ---------------------------------------------------------------------------


def test_compile_produces_meta_spec_from_meta_sop() -> None:
    from opensquilla.skills.meta.sop_compiler import compile as sop_compile
    from opensquilla.skills.types import SkillLayer, SkillSpec

    body = (
        "## Phase 1: First\n"
        "Run `paper-experiment-stub`. Save as `s1`.\n"
        "## Phase 2: Second\n"
        "Run `paper-plot-stub`. Save as `s2`.\n"
    )
    spec_in = SkillSpec(
        name="meta-tiny",
        description="t",
        layer=SkillLayer.BUNDLED,
        always=False,
        triggers=["test"],
        content=body,
        kind="meta_sop",
    )
    loader = _StubSkillLoader(
        {
            "paper-experiment-stub": {"entrypoint": {"command": "x"}},
            "paper-plot-stub": {"entrypoint": {"command": "x"}},
        },
    )
    spec_out = sop_compile(spec_in, skill_loader=loader)
    assert spec_out.kind == "meta"
    assert spec_out.name == "meta-tiny"
    assert spec_out.composition_raw is not None
    steps = spec_out.composition_raw["steps"]
    assert [s["id"] for s in steps] == ["s1", "s2"]


def test_compile_preserves_triggers_and_priority() -> None:
    from opensquilla.skills.meta.sop_compiler import compile as sop_compile
    from opensquilla.skills.types import SkillLayer, SkillSpec

    body = (
        "## Phase 1: Only\n"
        "Run `paper-experiment-stub`. Save as `s`.\n"
    )
    spec_in = SkillSpec(
        name="meta-tiny",
        description="t",
        layer=SkillLayer.BUNDLED,
        always=False,
        triggers=["t1", "t2"],
        content=body,
        kind="meta_sop",
        meta_priority=42,
    )
    loader = _StubSkillLoader({"paper-experiment-stub": {"entrypoint": {"command": "x"}}})
    spec_out = sop_compile(spec_in, skill_loader=loader)
    assert spec_out.triggers == ["t1", "t2"]
    assert spec_out.meta_priority == 42


def test_compile_rejects_non_meta_sop_input() -> None:
    """Calling compile on a regular meta skill is a programmer error."""
    from opensquilla.skills.meta.sop_compiler import compile as sop_compile
    from opensquilla.skills.types import SkillLayer, SkillSpec

    spec_in = SkillSpec(
        name="meta-regular",
        description="t",
        layer=SkillLayer.BUNDLED,
        always=False,
        triggers=["t"],
        content="",
        kind="meta",
    )
    with pytest.raises(ValueError, match="meta_sop"):
        sop_compile(spec_in, skill_loader=_StubSkillLoader({}))


# ---------------------------------------------------------------------------
# Loader integration
# ---------------------------------------------------------------------------


def test_loader_compiles_meta_sop_at_load_time(tmp_path: Path) -> None:
    """A SKILL.md with kind: meta_sop is detected by the loader and
    compiled before downstream callers see it."""

    # Bundle two skills the SOP will reference + the SOP skill itself.
    bundled = tmp_path / "bundled"
    bundled.mkdir()

    (bundled / "tiny-runner").mkdir()
    (bundled / "tiny-runner" / "SKILL.md").write_text(
        "---\n"
        "name: tiny-runner\n"
        "description: stub\n"
        "entrypoint:\n"
        "  command: python -c 'pass'\n"
        "---\n"
        "# tiny-runner\n",
    )

    (bundled / "meta-tiny").mkdir()
    (bundled / "meta-tiny" / "SKILL.md").write_text(
        "---\n"
        "name: meta-tiny\n"
        "description: tiny SOP\n"
        "kind: meta_sop\n"
        "triggers: [tiny]\n"
        "---\n"
        "## Phase 1: First\n"
        "Run `tiny-runner`. Save as `s1`.\n",
    )

    from opensquilla.skills.loader import SkillLoader

    snapshot = tmp_path / "snap.json"
    loader = SkillLoader(bundled_dir=bundled, snapshot_path=snapshot)
    loader.invalidate_cache()
    specs = {s.name: s for s in loader.load_all()}

    sop = specs.get("meta-tiny")
    assert sop is not None
    # After compilation, the spec should look like a regular meta skill
    assert sop.kind == "meta"
    assert sop.composition_raw is not None
    assert sop.composition_raw["steps"][0]["id"] == "s1"


def test_loader_skips_malformed_meta_sop(tmp_path: Path) -> None:
    """If the SOP fails to compile, the loader logs and skips the skill
    (matches behaviour for malformed regular skills)."""

    bundled = tmp_path / "bundled"
    bundled.mkdir()

    (bundled / "tiny-runner").mkdir()
    (bundled / "tiny-runner" / "SKILL.md").write_text(
        "---\n"
        "name: tiny-runner\n"
        "description: stub\n"
        "entrypoint:\n"
        "  command: python -c 'pass'\n"
        "---\n"
        "# tiny-runner\n",
    )

    (bundled / "meta-broken").mkdir()
    (bundled / "meta-broken" / "SKILL.md").write_text(
        "---\n"
        "name: meta-broken\n"
        "description: broken SOP\n"
        "kind: meta_sop\n"
        "triggers: [broken]\n"
        "---\n"
        "## Phase 1: Has when annotation [when: outputs.x == 'no']\n"
        "Run `tiny-runner`. Save as `s1`.\n",
    )

    from opensquilla.skills.loader import SkillLoader

    snapshot = tmp_path / "snap.json"
    loader = SkillLoader(bundled_dir=bundled, snapshot_path=snapshot)
    loader.invalidate_cache()
    specs = {s.name: s for s in loader.load_all()}

    # Compilation failed → the skill is not in the loaded set.
    assert "meta-broken" not in specs


# ---------------------------------------------------------------------------
# CLI subcommands
# ---------------------------------------------------------------------------


def test_cli_skills_inspect_prints_compiled_dag(tmp_path: Path) -> None:
    """`opensquilla skills inspect <name>` writes the compiled composition to stdout."""

    bundled = tmp_path / "bundled"
    bundled.mkdir()
    (bundled / "tiny-runner").mkdir()
    (bundled / "tiny-runner" / "SKILL.md").write_text(
        "---\nname: tiny-runner\ndescription: x\nentrypoint:\n  command: python -c 'pass'\n---\n",
    )
    (bundled / "meta-tiny").mkdir()
    (bundled / "meta-tiny" / "SKILL.md").write_text(
        "---\nname: meta-tiny\ndescription: t\nkind: meta_sop\ntriggers: [t]\n---\n"
        "## Phase 1: First\nRun `tiny-runner`. Save as `s1`.\n",
    )

    from opensquilla.cli.skills_cmd import inspect_compiled_dag

    output = inspect_compiled_dag(name="meta-tiny", bundled_dir=bundled)
    assert "s1" in output
    assert "tiny-runner" in output


def test_cli_skills_inspect_finds_managed_layer_skill(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """`inspect` (no explicit bundled_dir) must see managed-layer meta-skills.

    Regression: the helper used to build SkillLoader(bundled_dir=...) only,
    leaving managed_dir=None, so a user meta-skill installed under
    ~/.opensquilla/skills was always reported "not loaded".
    """

    managed = tmp_path / "managed"
    (managed / "meta-managed").mkdir(parents=True)
    (managed / "meta-managed" / "SKILL.md").write_text(
        "---\n"
        "name: meta-managed\n"
        "description: d\n"
        "kind: meta\n"
        "triggers: [m]\n"
        "composition:\n"
        "  steps:\n"
        "    - id: only\n"
        "      kind: llm_chat\n"
        "      with:\n"
        "        system: s\n"
        "        task: t\n"
        "---\n"
        "# body\n",
    )

    from opensquilla.skills import paths as paths_mod
    from opensquilla.skills.paths import SkillLayerDirs

    monkeypatch.setattr(
        paths_mod,
        "resolve_skill_layer_dirs",
        lambda **_kw: SkillLayerDirs(bundled_dir=None, managed_dir=managed),
    )

    from opensquilla.cli.skills_cmd import inspect_compiled_dag

    output = inspect_compiled_dag(name="meta-managed")
    assert "not loaded" not in output, output
    assert "only" in output
    assert "llm_chat" in output


# ---------------------------------------------------------------------------
# Acceptance test
# ---------------------------------------------------------------------------


def _normalize_plan(plan: Any) -> dict[str, Any]:
    """Reduce a MetaPlan to a dict that's comparable across YAML key orderings.

    We don't compare top-level `name` / `priority` / `triggers` because
    the SOP and handwritten variants share those frontmatter fields.
    Only the per-step content matters for acceptance.
    """

    return {
        step.id: {
            "kind": step.kind,
            "skill": step.skill,
            "depends_on": sorted(step.depends_on),
            "with_args": dict(step.with_args),
            "route": [(c.when, c.to) for c in step.route],
            "tool": step.tool,
            "tool_args": dict(step.tool_args),
            "tool_allowlist": sorted(step.tool_allowlist),
            "output_choices": list(step.output_choices),
        }
        for step in plan.steps
    }


def test_acceptance_meta_paper_write_sop_compiles_to_equivalent_dag() -> None:
    """The historical SOP-form meta-paper-write compiled to the handwritten
    fixture. The bundled skill may now be authored directly as YAML, but this
    acceptance test still verifies the active bundled plan parses and retains
    the paper quality gates."""

    from opensquilla.skills.loader import SkillLoader
    from opensquilla.skills.meta.parser import parse_meta_plan

    project_root = Path(__file__).resolve().parents[2]
    bundled = project_root / "src" / "opensquilla" / "skills" / "bundled"

    snapshot = Path("/tmp/_sop_acceptance_snap.json")
    if snapshot.exists():
        snapshot.unlink()
    loader = SkillLoader(bundled_dir=bundled, snapshot_path=snapshot)
    loader.invalidate_cache()
    loader.load_all()
    sop_spec = loader.get_by_name("meta-paper-write")
    assert sop_spec is not None
    assert sop_spec.kind == "meta"  # after compilation
    sop_plan = parse_meta_plan(sop_spec)
    assert sop_plan is not None
    if not getattr(sop_spec, "sop_source", None):
        step_ids = {step.id for step in sop_plan.steps}
        assert {
            "final_manuscript_package",
            "paper_length_gate",
            "citation_integrity_gate",
            "latex_sanitizer",
        } <= step_ids
        return

    # Load the handwritten fixture as a bare meta SkillSpec
    fixture_path = (
        project_root / "tests" / "_fixtures" / "meta-paper-write-handwritten.SKILL.md"
    )
    handwritten_text = fixture_path.read_text()
    import yaml as _yaml

    _, _, after_frontmatter = handwritten_text.partition("---")
    fm_yaml, _, _ = after_frontmatter.partition("---")
    fm = _yaml.safe_load(fm_yaml)

    from opensquilla.skills.types import SkillLayer, SkillSpec

    handwritten_spec = SkillSpec(
        name=fm["name"],
        description=fm.get("description", ""),
        layer=SkillLayer.BUNDLED,
        always=False,
        triggers=fm.get("triggers", []),
        content="",
        kind=fm.get("kind", "meta"),
        composition_raw=fm.get("composition"),
    )
    handwritten_plan = parse_meta_plan(handwritten_spec)
    assert handwritten_plan is not None

    sop_norm = _normalize_plan(sop_plan)
    handwritten_norm = _normalize_plan(handwritten_plan)

    assert set(sop_norm.keys()) == set(handwritten_norm.keys()), (
        f"step id sets differ:\n"
        f"  SOP only: {set(sop_norm.keys()) - set(handwritten_norm.keys())}\n"
        f"  Hand only: {set(handwritten_norm.keys()) - set(sop_norm.keys())}"
    )

    # Acceptance allows the SOP to have STRICTER depends_on (more deps)
    # than the handwritten YAML — the SOP's sequential phase defaults add
    # safe dependencies. We assert handwritten.depends_on ⊆ SOP.depends_on
    # and equal otherwise.
    for step_id, hand in handwritten_norm.items():
        sop = sop_norm[step_id]
        for key in (
            "kind",
            "skill",
            "with_args",
            "route",
            "tool",
            "tool_args",
            "tool_allowlist",
            "output_choices",
        ):
            assert sop[key] == hand[key], (
                f"step {step_id} field {key} differs:\n"
                f"  SOP:  {sop[key]!r}\n"
                f"  Hand: {hand[key]!r}"
            )
        assert set(hand["depends_on"]) <= set(sop["depends_on"]), (
            f"step {step_id} depends_on: handwritten {hand['depends_on']!r} "
            f"not subset of SOP {sop['depends_on']!r}"
        )
