"""Tests for the in-process MetaSkill creator G1/G2 gates."""

from __future__ import annotations

from pathlib import Path

import pytest

from opensquilla.skills.creator.lint_runtime import lint_meta_skill

REPO = Path(__file__).resolve().parents[2]
_BUNDLED_DIR = REPO / "src" / "opensquilla" / "skills" / "bundled"
_EXP_DIR = REPO / "src" / "opensquilla" / "skills" / "exp"


def _run_lint(skill_md: str, gates: str = "G1,G2") -> dict:
    return lint_meta_skill(skill_md, gates=gates)


VALID_P1 = """---
name: lint-test-p1
description: "Lint-test P1 sequential meta-skill: extract then summarize."
kind: meta
meta_priority: 50
triggers:
  - "lint test trigger"
provenance:
  origin: opensquilla-user
composition:
  steps:
    - id: extract
      skill: pdf-toolkit
      with:
        task: "Extract: {{ inputs.user_message | xml_escape | truncate(512) }}"
    - id: digest
      skill: docx
      depends_on: [extract]
      with:
        text: "{{ outputs.extract | truncate(2000) }}"
---
# Lint test P1
"""


def test_g1_passes_on_valid_p1() -> None:
    out = _run_lint(VALID_P1)
    assert out["G1"]["passed"] is True


def test_g1_fails_on_missing_xml_escape() -> None:
    bad = VALID_P1.replace("{{ inputs.user_message | xml_escape | truncate(512) }}",
                            "{{ inputs.user_message }}")
    out = _run_lint(bad)
    assert out["G1"]["passed"] is False
    assert any("xml_escape" in d.lower() for d in out["G1"]["diagnostics"])


def test_g1_fails_on_unknown_skill_reference() -> None:
    bad = VALID_P1.replace("skill: pdf-toolkit", "skill: this-skill-does-not-exist")
    out = _run_lint(bad)
    assert out["G1"]["passed"] is False
    assert any("does-not-exist" in d for d in out["G1"]["diagnostics"])


def test_g2_passes_on_valid_p1() -> None:
    out = _run_lint(VALID_P1)
    assert out["G2"]["passed"] is True


EXISTING_META_BUNDLES = [
    "meta-paper-write",
    "meta-short-drama",
    "meta-skill-creator",
]


def _existing_meta_skill_md(name: str) -> Path:
    for root in (_BUNDLED_DIR, _EXP_DIR):
        candidate = root / name / "SKILL.md"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(name)


def test_g1_fails_on_non_xml_escape_first_filter() -> None:
    """G1.6 fires when inputs.user_message has a filter but xml_escape is not
    the first one. Catches the bug class that motivated tightening the regex
    in the first place (truncate-without-escape was a real injection risk in
    3 bundles before this rule was enforced).
    """
    bad = VALID_P1.replace(
        "{{ inputs.user_message | xml_escape | truncate(512) }}",
        "{{ inputs.user_message | truncate(512) }}",
    )
    out = _run_lint(bad)
    assert out["G1"]["passed"] is False
    assert any("xml_escape" in d.lower() for d in out["G1"]["diagnostics"])


def test_g1_passes_on_non_meta_skill_without_xml_escape() -> None:
    """G1.6 must be scoped to kind: meta. A non-meta skill using
    {{ inputs.user_message | truncate }} is legitimate (the escape happens
    at a different layer). Linter should not false-positive here."""
    non_meta = """---
name: non-meta-test
description: "A regular non-meta skill using inputs.user_message without xml_escape."
kind: skill
provenance:
  origin: opensquilla-user
---
# Non-meta skill body
"""
    # Note: this won't have a meta plan, so G1.1 will fail with
    # "parse_meta_plan returned None (kind != meta?)" — but the failure
    # diagnostic must NOT mention G1.6. The G1.6 grep should silently skip.
    out = _run_lint(non_meta)
    # G1 fails overall (no meta plan), but the cause is NOT G1.6
    assert not any("xml_escape" in d.lower() for d in out["G1"]["diagnostics"]), (
        f"G1.6 should not fire on non-meta; diagnostics={out['G1']['diagnostics']}"
    )


def test_g1_rejects_nested_meta_skill_reference() -> None:
    """N5 regression: G1.2 must reject steps that reference another kind=meta
    bundle. The agent executor refuses nested meta-skills at runtime with
    'cannot compose another meta-skill', but the old set-based catalog check
    only verified existence — kind=meta bundles passed G1 and G2 silently,
    producing misleading auto_enable_eligible=true proposals that crashed at
    runtime."""
    nested_meta = VALID_P1.replace(
        "skill: pdf-toolkit", "skill: meta-paper-write"
    )
    out = _run_lint(nested_meta)
    assert out["G1"]["passed"] is False
    assert any(
        "meta-paper-write" in d
        and ("unknown" in d.lower() or "nested" in d.lower() or "kind: meta" in d)
        for d in out["G1"]["diagnostics"]
    ), f"Expected nested meta-skill diagnostic; got: {out['G1']['diagnostics']}"


@pytest.mark.parametrize("bundle", EXISTING_META_BUNDLES)
def test_linter_passes_existing_meta_bundle(bundle: str) -> None:
    """Regression: linter must accept every existing kind=meta bundle.
    Catches over-strict lint rules."""
    skill_md = _existing_meta_skill_md(bundle).read_text(encoding="utf-8")
    out = _run_lint(skill_md)
    assert out["G1"]["passed"] is True, f"{bundle} G1 fail: {out['G1']['diagnostics']}"
    assert out["G2"]["passed"] is True, f"{bundle} G2 fail: {out['G2']['diagnostics']}"


def test_g1_6_catches_quoted_kind_with_unsafe_template() -> None:
    """N13: G1.6 must not be bypassable by quoting the kind value.
    Previously the gating used a regex on raw YAML text that only matched
    `kind: meta` (unquoted). Quoting as `kind: \"meta\"` is semantically
    identical in YAML but bypassed the regex. The fix uses the parsed
    spec.kind from SkillLoader instead."""
    bad = '''---
name: quoted-kind-bypass-test
description: "Demonstrates that quoting kind cannot bypass G1.6 xml_escape check."
kind: "meta"
meta_priority: 50
triggers:
  - "quoted kind bypass test"
provenance:
  origin: opensquilla-user
composition:
  steps:
    - id: a
      skill: docx
      with:
        task: "{{ inputs.user_message }}"
    - id: b
      skill: pdf-toolkit
      depends_on: [a]
      with:
        text: "{{ outputs.a }}"
---
# bypass test
'''
    out = _run_lint(bad)
    assert out["G1"]["passed"] is False, (
        "N13: quoted kind: \"meta\" must NOT bypass G1.6 xml_escape requirement"
    )
    assert any("xml_escape" in d.lower() for d in out["G1"]["diagnostics"]), (
        f"N13: expected G1.6 xml_escape diagnostic; got: {out['G1']['diagnostics']}"
    )
