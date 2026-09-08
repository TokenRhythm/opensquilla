"""Integration: proposal → meta accept → MANAGED layer loads the new skill."""

from __future__ import annotations

from pathlib import Path

from opensquilla.skills.loader import SkillLoader
from opensquilla.skills.proposals_lib import accept_proposal, write_proposal

VALID_SKILL_MD = """---
name: accept-flow-test-skill
description: "Accept-flow integration test sample skill: minimal placeholder."
kind: meta
meta_priority: 50
triggers:
  - "accept flow test phrase"
provenance:
  origin: opensquilla-user
composition:
  steps:
    - id: only
      skill: docx
      with:
        task: "{{ inputs.user_message | xml_escape | truncate(512) }}"
---
"""


def _write(home: Path, skill_md: str) -> dict:
    return write_proposal(
        home,
        skill_md,
        {"G1": {"passed": True}, "G2": {"passed": True}},
        {"G3": {"passed": True}, "G4": {"passed": True}},
    )


def test_accept_flow_moves_to_skills_dir_and_loader_picks_up(tmp_path: Path) -> None:
    home = tmp_path / ".opensquilla"

    # Step 1: write proposal (eligible)
    out = _write(home, VALID_SKILL_MD)
    proposal_id = out["proposal_id"]

    # Step 2: accept
    out = accept_proposal(home, proposal_id)
    assert out["status"] == "ok"
    skill_path = Path(out["skill_path"])
    assert skill_path.is_dir()
    assert (skill_path / "SKILL.md").is_file()

    # Step 3: loader picks it up via MANAGED layer
    loader = SkillLoader(
        managed_dir=home / "skills",
        snapshot_path=tmp_path / "snap.json",
    )
    loader.invalidate_cache()
    names = {s.name for s in loader.load_all()}
    assert "accept-flow-test-skill" in names

    # Move semantics: source proposal should no longer exist after accept
    assert not (home / "proposals" / proposal_id).exists(), (
        "accept should MOVE the proposal, not copy it"
    )


# N3 regression: creator-generated SKILL.md uses tojson which produces
# quoted names (name: "synth-quoted-name-test"). The previous unquoted-only
# regex `^name:\s*([\w\-]+)\s*$` silently failed with "cannot parse skill
# name from SKILL.md" on any proposal created through the normal creator path.
QUOTED_NAME_SKILL_MD = """---
name: "synth-quoted-name-test"
description: "Sample for quoted-name accept regression."
kind: meta
meta_priority: 50
triggers:
  - "synth quoted name test"
provenance:
  origin: opensquilla-user
composition:
  steps:
    - id: only
      skill: docx
      with:
        task: "{{ inputs.user_message | xml_escape | truncate(512) }}"
---
"""


def test_accept_quoted_name_from_tojson_template(tmp_path: Path) -> None:
    """N3 regression: cmd_accept must parse names in both quoted and unquoted
    YAML form. Creator-generated SKILL.md uses `name: "synth-pipeline"` (tojson
    output); the previous regex only matched bare `name: synth-pipeline`."""
    home = tmp_path / ".opensquilla"

    out = _write(home, QUOTED_NAME_SKILL_MD)
    proposal_id = out["proposal_id"]

    out = accept_proposal(home, proposal_id)
    assert out["status"] == "ok", (
        f"accept failed with quoted name; got: {out}"
    )
    assert out["name"] == "synth-quoted-name-test"
    assert Path(out["skill_path"]).is_dir()
