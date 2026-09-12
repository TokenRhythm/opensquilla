"""Dogfood: meta-skill-creator/SKILL.md itself must pass G1+G2."""

from __future__ import annotations

from pathlib import Path

from opensquilla.skills.creator.lint_runtime import lint_meta_skill

REPO = Path(__file__).resolve().parents[2]
CREATOR_MD = (
    REPO / "src" / "opensquilla" / "skills" / "bundled"
    / "meta-skill-creator" / "SKILL.md"
)


def test_meta_skill_creator_passes_g1_g2() -> None:
    out = lint_meta_skill(CREATOR_MD.read_text(encoding="utf-8"))
    assert out["G1"]["passed"] is True, f"G1 fail: {out['G1']['diagnostics']}"
    assert out["G2"]["passed"] is True, f"G2 fail: {out['G2']['diagnostics']}"
