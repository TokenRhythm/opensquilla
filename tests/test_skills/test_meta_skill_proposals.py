"""Tests for the in-process MetaSkill proposal store."""

from __future__ import annotations

import json
from pathlib import Path

from opensquilla.skills.proposals_lib import accept_proposal, list_proposals, write_proposal


def _run(action: str, *args, home: Path, **kwargs) -> dict:
    del args
    if action == "write_proposal":
        return write_proposal(
            home,
            kwargs.pop("skill_md_inline"),
            json.loads(kwargs.pop("lint_result")),
            json.loads(kwargs.pop("smoke_result")),
            creator_mode=kwargs.pop("creator_mode", ""),
            acceptance_result=kwargs.pop("acceptance_result", None),
            runtime_e2e_result=kwargs.pop("runtime_e2e_result", None),
            **kwargs,
        )
    if action == "accept":
        return accept_proposal(home, kwargs["proposal_id"])
    if action == "list":
        return list_proposals(home)
    raise AssertionError(action)


SAMPLE_SKILL_MD = """---
name: synth-test-pipeline
description: "Sample synthetic pipeline for proposals tests"
kind: meta
meta_priority: 50
triggers:
  - "synth test trigger"
provenance:
  origin: opensquilla-user
composition:
  steps:
    - id: a
      skill: docx
      with:
        task: "{{ inputs.user_message | xml_escape | truncate(512) }}"
---
"""


def test_write_proposal_creates_directory(tmp_path: Path) -> None:
    home = tmp_path / ".opensquilla"
    out = _run(
        "write_proposal", home=home,
        skill_md_inline=SAMPLE_SKILL_MD,
        lint_result=json.dumps({"G1": {"passed": True}, "G2": {"passed": True}}),
        smoke_result=json.dumps({"G3": {"passed": True}, "G4": {"passed": True}}),
    )
    assert out["status"] == "ok"
    proposal_id = out["proposal_id"]
    proposal_dir = home / "proposals" / proposal_id
    assert (proposal_dir / "SKILL.md").exists()
    assert (proposal_dir / "gates.json").exists()
    gates = json.loads((proposal_dir / "gates.json").read_text())
    assert gates["auto_enable_eligible"] is True


def test_write_proposal_marks_ineligible_on_g3_fail(tmp_path: Path) -> None:
    home = tmp_path / ".opensquilla"
    out = _run(
        "write_proposal", home=home,
        skill_md_inline=SAMPLE_SKILL_MD,
        lint_result=json.dumps({"G1": {"passed": True}, "G2": {"passed": True}}),
        smoke_result=json.dumps({"G3": {"passed": False, "reason": "classifier missed"},
                                  "G4": {"passed": True}}),
    )
    gates = json.loads((home / "proposals" / out["proposal_id"] / "gates.json").read_text())
    assert gates["auto_enable_eligible"] is False


def test_write_proposal_marks_full_gated_ineligible_on_compare_loss(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".opensquilla"
    out = _run(
        "write_proposal", home=home,
        skill_md_inline=SAMPLE_SKILL_MD,
        lint_result=json.dumps({"G1": {"passed": True}, "G2": {"passed": True}}),
        smoke_result=json.dumps({"G3": {"passed": True}, "G4": {"passed": True}}),
        creator_mode="FULL_GATED",
        acceptance_result=(
            "WINNER: orchestrated\n"
            "REASONS:\n"
            "- generated skill is clearer\n"
            "REGRESSIONS:\n"
            "- none\n"
            "REQUIRED_IMPROVEMENTS:\n"
            "- none\n"
        ),
        runtime_e2e_result=json.dumps({
            "status": "ok",
            "passed": False,
            "winner": "baseline",
            "cases": [{"prompt": "please use synth test trigger", "winner": "baseline"}],
        }),
    )

    gates = json.loads((home / "proposals" / out["proposal_id"] / "gates.json").read_text())
    assert out["auto_enable_eligible"] is False
    assert gates["runtime_e2e"]["passed"] is False
    assert gates["runtime_e2e"]["winner"] == "baseline"


def test_accept_rejects_path_traversal_proposal_id(tmp_path: Path) -> None:
    """I1 regression: cmd_accept must reject proposal IDs that aren't 8 hex chars."""
    home = tmp_path / ".opensquilla"
    home.mkdir()
    (home / "proposals").mkdir()

    for bad_id in ["../../etc", "../sibling", "abcd1234567890", "ABCDEF12", ""]:
        out = _run("accept", home=home, proposal_id=bad_id)
        assert out["status"] == "error", f"should reject {bad_id!r}, got: {out}"
        assert "invalid proposal_id" in out["reason"]
