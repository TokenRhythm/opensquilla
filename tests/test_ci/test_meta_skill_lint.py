"""Lint gate for bundled meta-skill SKILL.md files (Step C).

Mechanical checks corresponding to pptx slide 5's "写法要点" (writing
requirements) for meta-skills, plus a few OpenSquilla-specific gates:

* **L1**: ``description`` is non-empty and at least 30 chars (so it
  conveys *when* to use the skill, not just *what* it does).
* **L2**: ``triggers`` contains at least one entry (the skill needs a
  resolvable entry surface, even if the LLM-driven soft path also
  works without one). Fully disabled compatibility tombstones are exempt:
  an empty trigger list is part of their retirement contract.
* **L3**: ``meta_priority`` is explicitly declared (not relying on the
  default 0, which silently puts new meta-skills at the bottom of the
  hard-takeover resolution order).
* **L4**: the parsed plan has at least one step.

These checks run against every ``kind: meta`` and ``kind: meta_sop``
bundle under ``src/opensquilla/skills/bundled/``. SKILL.md files
that are not meta-skills (kind: skill, or no kind field) are skipped.

The intent is a *floor*, not a ceiling — passing this lint does not
make a meta-skill production-grade; it just rules out the most common
ways for a new bundle to ship with empty/incomplete metadata.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import pytest

from opensquilla.skills.loader import SkillLoader
from opensquilla.skills.meta.parser import parse_meta_plan

_BUNDLED_DIR = Path(__file__).resolve().parents[2] / "src" / "opensquilla" / "skills" / "bundled"

_MIN_DESCRIPTION_CHARS = 30


@dataclass(frozen=True)
class LintFinding:
    rule: str
    skill: str
    message: str

    def __str__(self) -> str:
        return f"[{self.rule}] {self.skill}: {self.message}"


def _is_meta_skill(spec: object) -> bool:
    kind = getattr(spec, "kind", "skill")
    return kind in ("meta", "meta_sop")


def _lint_one(spec: object) -> list[LintFinding]:
    findings: list[LintFinding] = []
    name = getattr(spec, "name", "<unknown>")

    description = (getattr(spec, "description", "") or "").strip()
    if len(description) < _MIN_DESCRIPTION_CHARS:
        findings.append(
            LintFinding(
                rule="L1",
                skill=name,
                message=(
                    f"description is {len(description)} chars, need ≥ "
                    f"{_MIN_DESCRIPTION_CHARS}; describe when to use this "
                    f"skill, not just what it does"
                ),
            ),
        )

    triggers = list(getattr(spec, "triggers", None) or [])
    retired_tombstone = (
        getattr(spec, "user_invocable", True) is False
        and getattr(spec, "disable_model_invocation", False) is True
    )
    if not triggers and not retired_tombstone:
        findings.append(
            LintFinding(
                rule="L2",
                skill=name,
                message="triggers list is empty; need at least one entry",
            ),
        )

    # L3: meta_priority must be an explicit numeric attribute, not the
    # implicit default of 0. SkillSpec does not preserve frontmatter
    # provenance, so we re-read the SKILL.md file's YAML head to check
    # for the literal ``meta_priority:`` key.
    skill_path = getattr(spec, "path", None) or getattr(spec, "base_dir", None)
    declared_meta_priority = False
    if skill_path is not None:
        skill_file = Path(skill_path)
        if skill_file.is_dir():
            skill_file = skill_file / "SKILL.md"
        if skill_file.is_file():
            try:
                text = skill_file.read_text(encoding="utf-8")
            except OSError:
                text = ""
            # Strict: line begins with the key (allow indentation). YAML
            # frontmatter is between two ``---`` delimiters at file top.
            if text.startswith("---"):
                fm_end = text.find("\n---", 3)
                if fm_end > 0:
                    fm = text[3:fm_end]
                    for line in fm.splitlines():
                        if line.lstrip().startswith("meta_priority:"):
                            declared_meta_priority = True
                            break
    if not declared_meta_priority:
        findings.append(
            LintFinding(
                rule="L3",
                skill=name,
                message=(
                    "meta_priority is not explicitly declared in "
                    "frontmatter; relying on the default 0 puts this "
                    "skill at the bottom of resolution order"
                ),
            ),
        )

    # L4: parsed plan has steps. We tolerate parser errors here — if
    # the meta-skill doesn't parse at all, the other test files
    # (test_meta_paper_skills, etc.) will surface that more loudly.
    try:
        plan = parse_meta_plan(spec)
    except Exception:  # noqa: BLE001
        plan = None
    if plan is not None and not plan.steps:
        findings.append(
            LintFinding(
                rule="L4",
                skill=name,
                message="parsed plan has zero steps",
            ),
        )

    return findings


@pytest.fixture(scope="module")
def _all_meta_specs(tmp_path_factory: pytest.TempPathFactory) -> list[object]:
    snapshot = tmp_path_factory.mktemp("meta-lint") / "snapshot.json"
    loader = SkillLoader(bundled_dir=_BUNDLED_DIR, snapshot_path=snapshot)
    loader.invalidate_cache()
    return [s for s in loader.load_all() if _is_meta_skill(s)]


def _collect_findings(specs: Iterable[object]) -> list[LintFinding]:
    findings: list[LintFinding] = []
    for spec in specs:
        findings.extend(_lint_one(spec))
    return findings


def test_baseline_lint_findings_are_within_budget(_all_meta_specs: list[object]) -> None:
    """Baseline gate — fails when the count of findings grows beyond
    the recorded floor. Tighten the budget after each cleanup pass.

    The budget tracks the *count* (not the identity) of findings so
    individual cleanups can land in any order without thrashing the
    expected-set.
    """
    findings = _collect_findings(_all_meta_specs)
    grouped: dict[str, int] = {}
    for f in findings:
        grouped[f.rule] = grouped.get(f.rule, 0) + 1

    # Budget recorded against the bundle state at commit C-c baseline.
    # When you add a new meta-skill bundle or relax a rule, update this
    # dict. When you clean up findings, lower the matching number.
    budget = {
        "L1": 0,
        "L2": 0,
        "L3": 0,
        "L4": 0,
    }

    report_lines = [f"{f}" for f in findings]
    report = "\n  ".join(report_lines) if report_lines else "(none)"

    over_budget = {
        rule: (grouped.get(rule, 0), allowed)
        for rule, allowed in budget.items()
        if grouped.get(rule, 0) > allowed
    }
    assert not over_budget, (
        f"meta-skill lint findings exceed budget: {over_budget}\n"
        f"Full findings:\n  {report}\n"
        f"To proceed: either fix the offending bundles or raise the "
        f"budget in this test."
    )


# ---------------------------------------------------------------------------
# G1.6 CI-level enforcement: every bundled meta-skill SKILL.md that
# references `{{ inputs.user_message }}` must immediately follow it with
# `| xml_escape` (or another approved sanitiser).
# ---------------------------------------------------------------------------

_G1_BUNDLED = Path(__file__).resolve().parents[2] / "src" / "opensquilla" / "skills" / "bundled"

_G1_META_BUNDLES = sorted(
    [p.parent.name for p in _G1_BUNDLED.glob("meta-*/SKILL.md")]
    + [p.parent.name for p in _G1_BUNDLED.glob("history-explorer/SKILL.md")]
)

# Match same regex as skill-creator-linter G1.6 (Task 2 + relaxation for slugify).
# The positive lookahead (?=[\s|}]) adds a word boundary so that fields with a
# user_message prefix (e.g. inputs.user_message_body) are not false-positively
# matched.  The \b after each filter name prevents prefix matches.
_XML_ESCAPE_RE = re.compile(
    r"\{\{\s*inputs\.user_message(?=[\s|}])(?!\s*\|\s*(xml_escape|slugify)\b)"
)


@pytest.mark.parametrize("bundle", _G1_META_BUNDLES)
def test_xml_escape_present_on_user_message(bundle: str) -> None:
    """G1.6 enforcement: every bundle's SKILL.md must xml_escape (or slugify)
    `inputs.user_message` references."""
    skill_md = (_G1_BUNDLED / bundle / "SKILL.md").read_text(encoding="utf-8")
    bad = _XML_ESCAPE_RE.findall(skill_md)
    assert not bad, (
        f"{bundle}/SKILL.md: 'inputs.user_message' not immediately followed by "
        f"'| xml_escape' or '| slugify'. Matches: {bad}"
    )
