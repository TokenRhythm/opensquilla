"""Tests for skills.creator.auto_propose (Path 1+2 library function).

These tests cover the deterministic skeleton — pattern aggregation,
filtering, deduplication, provenance patching, fault tolerance — using
a mock MetaOrchestrator so no LLM calls are required. The LLM-driven
parts of the meta-skill-creator DAG itself are covered by
test_creator_proposer + test_meta_skill_creator_e2e.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from opensquilla.skills.creator.auto_propose import (
    _META_SKILL_CREATOR_TRIGGERS,
    AutoProposeResult,
    _chain_signature,
    _synthesise_user_message,
    auto_propose,
    is_auto_propose_disabled,
    try_auto_enable_proposal,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _seed_decision_log(
    log_dir: Path,
    chain: list[str],
    *,
    count: int,
    when: datetime | None = None,
    intent: str = "",
) -> None:
    """Append ``count`` decision entries with the given skills chain."""
    when = when or datetime.now(UTC)
    log_dir.mkdir(parents=True, exist_ok=True)
    day = when.strftime("%Y%m%d")
    path = log_dir / f"decisions-{day}.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        for _ in range(count):
            fh.write(json.dumps({
                "ts": when.isoformat(),
                "skills_invoked": list(chain),
                "user_message": intent,
            }) + "\n")


def _stub_loader_with_creator(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Build a SkillLoader-shaped mock whose only kind=meta entry is the
    real bundled meta-skill-creator spec (so parse_meta_plan succeeds)."""
    from opensquilla.skills.loader import SkillLoader

    root = Path(__file__).resolve().parents[2]
    real = SkillLoader(
        bundled_dir=root / "src" / "opensquilla" / "skills" / "bundled",
        snapshot_path=root / ".pytest_cache" / "auto_propose_snap.json",
    )
    real.invalidate_cache()
    real.load_all()
    return real  # use the real one — easier than mocking


def _make_proposer_orchestrator(
    proposals_dir: Path,
    *,
    proposal_ids: list[str] | None = None,
    skill_md: str = "---\nname: synth-skill\nkind: meta\n---\n",
    raises: Exception | None = None,
) -> MagicMock:
    """Mock orchestrator whose .run() writes synthetic proposal dirs.

    Mirrors meta-skill-creator's persist step: writes proposal_dir/SKILL.md
    and gates.json for each requested proposal_id, then resolves.
    """
    proposal_ids = list(proposal_ids or [])
    orch = MagicMock()

    async def fake_run(_match: Any) -> Any:
        if raises is not None:
            raise raises
        for pid in proposal_ids:
            d = proposals_dir / pid
            d.mkdir(parents=True, exist_ok=True)
            (d / "SKILL.md").write_text(
                skill_md, encoding="utf-8",
            )
            (d / "gates.json").write_text(json.dumps({
                "lint": {"G1": {"passed": True}, "G2": {"passed": True}},
                "smoke": {"G3": {"passed": True}, "G4": {"passed": True}},
                "auto_enable_eligible": True,
            }), encoding="utf-8")
        from opensquilla.skills.meta.types import MetaResult
        return MetaResult(ok=True, final_text="ok")

    orch.run = AsyncMock(side_effect=fake_run)
    return orch


def _loader_with_managed_dir(home: Path) -> Any:
    """Real SkillLoader with bundled skills plus temp MANAGED layer."""
    from opensquilla.skills.loader import SkillLoader

    root = Path(__file__).resolve().parents[2]
    loader = SkillLoader(
        bundled_dir=root / "src" / "opensquilla" / "skills" / "bundled",
        managed_dir=home / "skills",
        snapshot_path=home / "cache" / "skills_snapshot.json",
    )
    loader.invalidate_cache()
    loader.load_all()
    return loader


def _write_managed_skill(home: Path, name: str, skill_md: str) -> None:
    skill_dir = home / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")


def test_chain_signature_keeps_order_and_intent_separate() -> None:
    assert _chain_signature(["a", "b"], "invoice cleanup") != _chain_signature(
        ["b", "a"], "invoice cleanup",
    )
    assert _chain_signature(["a", "b"], "invoice cleanup") != _chain_signature(
        ["a", "b"], "travel planning",
    )


def test_unknown_historical_skills_are_skipped_before_creator_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_dir = tmp_path / "logs"
    proposals_dir = tmp_path / "proposals"
    _seed_decision_log(
        log_dir,
        ["removed-old-skill", "docx"],
        count=5,
        intent="docx old workflow output",
    )
    loader = _stub_loader_with_creator(monkeypatch)
    orch = _make_proposer_orchestrator(proposals_dir, proposal_ids=["aaaaaaaa"])

    result = asyncio.run(auto_propose(
        orchestrator=orch,
        skill_loader=loader,
        log_dir=log_dir,
        min_freq=3,
        proposals_dir=proposals_dir,
    ))

    assert result.proposals_created == []
    assert result.errors == []
    assert result.skipped == [
        {
            "skills": ["removed-old-skill", "docx"],
            "freq": 5,
            "reason": "unknown_skill",
        }
    ]
    orch.run.assert_not_called()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_empty_log_dir_produces_no_proposals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_dir = tmp_path / "logs"
    proposals_dir = tmp_path / "proposals"
    loader = _stub_loader_with_creator(monkeypatch)
    orch = _make_proposer_orchestrator(proposals_dir, proposal_ids=["aaaaaaaa"])

    result = await auto_propose(
        orchestrator=orch,
        skill_loader=loader,
        log_dir=log_dir,
        proposals_dir=proposals_dir,
    )
    assert result.proposals_created == []
    assert result.skipped == []
    assert result.errors == []
    orch.run.assert_not_called()


@pytest.mark.asyncio
async def test_pattern_below_min_freq_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_dir = tmp_path / "logs"
    proposals_dir = tmp_path / "proposals"
    _seed_decision_log(log_dir, ["pdf-toolkit", "docx"], count=2)
    loader = _stub_loader_with_creator(monkeypatch)
    orch = _make_proposer_orchestrator(proposals_dir, proposal_ids=["aaaaaaaa"])

    result = await auto_propose(
        orchestrator=orch,
        skill_loader=loader,
        log_dir=log_dir,
        min_freq=3,
        proposals_dir=proposals_dir,
    )
    assert result.proposals_created == []
    assert len(result.skipped) == 1
    assert result.skipped[0]["reason"] == "below_min_freq"
    orch.run.assert_not_called()


@pytest.mark.asyncio
async def test_pattern_at_threshold_creates_proposal_with_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_dir = tmp_path / "logs"
    proposals_dir = tmp_path / "proposals"
    _seed_decision_log(
        log_dir,
        ["pdf-toolkit", "docx"],
        count=5,
        intent="docx a PDF and save the digest",
    )
    loader = _stub_loader_with_creator(monkeypatch)
    orch = _make_proposer_orchestrator(proposals_dir, proposal_ids=["cafe1234"])

    result = await auto_propose(
        orchestrator=orch,
        skill_loader=loader,
        log_dir=log_dir,
        min_freq=3,
        triggered_by="cron",
        proposals_dir=proposals_dir,
    )
    assert result.proposals_created == ["cafe1234"]
    assert result.errors == []
    orch.run.assert_called_once()

    # Provenance was patched onto gates.json
    gates = json.loads((proposals_dir / "cafe1234" / "gates.json").read_text())
    assert gates["provenance"]["triggered_by"] == "auto_cron"
    assert gates["provenance"]["auto_propose_meta"]["skills"] == ["pdf-toolkit", "docx"]
    assert gates["provenance"]["auto_propose_meta"]["freq"] == 5
    assert isinstance(gates["provenance"]["chain_hash"], str)
    assert gates["provenance"]["auto_propose_meta"]["intent_digest"]
    # Lint / smoke payload preserved (provenance is additive, not destructive)
    assert gates["lint"]["G1"]["passed"] is True
    assert gates["auto_enable_eligible"] is True

    match = orch.run.call_args.args[0]
    assert match.inputs["user_message"].startswith("auto-proposal:")
    assert "FULL_GATED validation" in match.inputs["user_message"]
    assert "runtime E2E comparison" in match.inputs["user_message"]
    assert "docx a PDF and save the digest" in match.inputs["system_prompt"]
    assert match.inputs["system_prompt"].startswith("Unattended meta-skill auto-propose run.")


@pytest.mark.asyncio
async def test_auto_enable_accepts_medium_risk_eligible_proposal(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    log_dir = home / "logs"
    proposals_dir = home / "proposals"
    _seed_decision_log(log_dir, ["history-explorer", "docx"], count=5)
    loader = _loader_with_managed_dir(home)
    skill_md = """---
name: synth-history-summary
kind: meta
triggers:
  - synth history summary
composition:
  steps:
    - id: explore
      skill: history-explorer
      with:
        query: "{{ inputs.user_message | xml_escape | truncate(512) }}"
    - id: docx
      skill: docx
      depends_on: [explore]
      with:
        text: "{{ outputs.explore | truncate(2000) }}"
---
"""
    orch = _make_proposer_orchestrator(
        proposals_dir,
        proposal_ids=["cafe1234"],
        skill_md=skill_md,
    )

    result = await auto_propose(
        orchestrator=orch,
        skill_loader=loader,
        log_dir=log_dir,
        min_freq=3,
        triggered_by="cron",
        proposals_dir=proposals_dir,
        auto_enable=True,
        auto_enable_max_risk="medium",
    )

    assert result.proposals_created == ["cafe1234"]
    assert result.proposals_enabled == ["cafe1234"]
    assert result.auto_enable[0]["status"] == "enabled"
    assert not (proposals_dir / "cafe1234").exists()

    accepted_dir = home / "skills" / "synth-history-summary"
    assert (accepted_dir / "SKILL.md").read_text(encoding="utf-8") == skill_md
    gates = json.loads((accepted_dir / "gates.json").read_text(encoding="utf-8"))
    assert gates["auto_enable"]["status"] == "enabled"
    assert gates["auto_enable"]["risk_level"] == "medium"
    assert gates["provenance"]["triggered_by"] == "auto_cron"
    assert loader.get_by_name("synth-history-summary") is not None


@pytest.mark.asyncio
async def test_auto_enable_keeps_unescaped_user_input_proposal_pending(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    log_dir = home / "logs"
    proposals_dir = home / "proposals"
    _seed_decision_log(log_dir, ["history-explorer", "docx"], count=5)
    loader = _loader_with_managed_dir(home)
    skill_md = """---
name: synth-unsafe-input
kind: meta
triggers:
  - synth unsafe input
composition:
  steps:
    - id: explore
      skill: history-explorer
      with:
        query: "{{ inputs.user_message }}"
    - id: docx
      skill: docx
      depends_on: [explore]
      with:
        text: "{{ outputs.explore | truncate(2000) }}"
---
"""
    orch = _make_proposer_orchestrator(
        proposals_dir,
        proposal_ids=["beef1234"],
        skill_md=skill_md,
    )

    result = await auto_propose(
        orchestrator=orch,
        skill_loader=loader,
        log_dir=log_dir,
        min_freq=3,
        proposals_dir=proposals_dir,
        auto_enable=True,
        auto_enable_max_risk="low",
    )

    assert result.proposals_enabled == []
    assert result.auto_enable[0]["status"] == "skipped"
    assert "unsafe_user_input_template:explore.with.query" in (
        result.auto_enable[0]["details"]["reasons"]
    )


@pytest.mark.asyncio
async def test_auto_enable_keeps_unbounded_output_proposal_pending(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    log_dir = home / "logs"
    proposals_dir = home / "proposals"
    _seed_decision_log(log_dir, ["history-explorer", "docx"], count=5)
    loader = _loader_with_managed_dir(home)
    skill_md = """---
name: synth-raw-output
kind: meta
triggers:
  - synth raw output
composition:
  steps:
    - id: explore
      skill: history-explorer
      with:
        query: "{{ inputs.user_message | xml_escape | truncate(512) }}"
    - id: docx
      skill: docx
      depends_on: [explore]
      with:
        text: "{{ outputs.explore }}"
---
"""
    orch = _make_proposer_orchestrator(
        proposals_dir,
        proposal_ids=["face1234"],
        skill_md=skill_md,
    )

    result = await auto_propose(
        orchestrator=orch,
        skill_loader=loader,
        log_dir=log_dir,
        min_freq=3,
        proposals_dir=proposals_dir,
        auto_enable=True,
        auto_enable_max_risk="low",
    )

    assert result.proposals_created == ["face1234"]
    assert result.proposals_enabled == []
    assert result.auto_enable[0]["status"] == "skipped"
    assert result.auto_enable[0]["reason"] == "risk_too_high"
    assert result.auto_enable[0]["risk_level"] == "high"
    details = result.auto_enable[0]["details"]
    assert "unbounded_output_template:docx.with.text" in details["reasons"]
    assert (proposals_dir / "face1234" / "SKILL.md").is_file()
    gates = json.loads((proposals_dir / "face1234" / "gates.json").read_text())
    assert gates["auto_enable"]["details"]["validation_profile"] == "static-safety-v2"


@pytest.mark.asyncio
async def test_auto_enable_keeps_high_risk_proposal_pending(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    log_dir = home / "logs"
    proposals_dir = home / "proposals"
    _seed_decision_log(log_dir, ["docx", "github"], count=5)
    loader = _loader_with_managed_dir(home)
    skill_md = """---
name: synth-docx-github
kind: meta
triggers:
  - synth docx github
composition:
  steps:
    - id: document
      skill: docx
      with:
        task: "{{ inputs.user_message | xml_escape | truncate(512) }}"
    - id: repository
      skill: github
      depends_on: [document]
      with:
        task: "{{ outputs.document | truncate(2000) }}"
---
"""
    orch = _make_proposer_orchestrator(
        proposals_dir,
        proposal_ids=["feed1234"],
        skill_md=skill_md,
    )

    result = await auto_propose(
        orchestrator=orch,
        skill_loader=loader,
        log_dir=log_dir,
        min_freq=3,
        proposals_dir=proposals_dir,
        auto_enable=True,
        auto_enable_max_risk="low",
    )

    assert result.proposals_created == ["feed1234"]
    assert result.proposals_enabled == []
    assert result.auto_enable[0]["status"] == "skipped"
    assert result.auto_enable[0]["reason"] == "risk_too_high"
    assert result.auto_enable[0]["risk_level"] == "high"
    assert (proposals_dir / "feed1234" / "SKILL.md").is_file()
    assert not (home / "skills" / "synth-docx-github").exists()
    gates = json.loads((proposals_dir / "feed1234" / "gates.json").read_text())
    assert gates["auto_enable"]["status"] == "skipped"
    assert gates["auto_enable"]["reason"] == "risk_too_high"


@pytest.mark.asyncio
async def test_auto_enable_uses_manifest_capability_risk(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    log_dir = home / "logs"
    proposals_dir = home / "proposals"
    _seed_decision_log(log_dir, ["artifact-writer", "docx"], count=5)
    _write_managed_skill(
        home,
        "artifact-writer",
        """---
name: artifact-writer
description: Writes a local artifact.
metadata:
  opensquilla:
    capabilities: [filesystem-write]
---
# artifact-writer
""",
    )
    loader = _loader_with_managed_dir(home)
    skill_md = """---
name: synth-artifact-writer
kind: meta
triggers:
  - synth artifact writer
composition:
  steps:
    - id: write
      skill: artifact-writer
      with:
        text: "{{ inputs.user_message | xml_escape | truncate(512) }}"
---
"""
    orch = _make_proposer_orchestrator(
        proposals_dir,
        proposal_ids=["a11fab1e"],
        skill_md=skill_md,
    )

    result = await auto_propose(
        orchestrator=orch,
        skill_loader=loader,
        log_dir=log_dir,
        min_freq=3,
        proposals_dir=proposals_dir,
        auto_enable=True,
        auto_enable_max_risk="low",
    )

    assert result.proposals_enabled == []
    assert result.auto_enable[0]["status"] == "skipped"
    assert result.auto_enable[0]["risk_level"] == "medium"
    assert "capability:artifact-writer:filesystem-write" in (
        result.auto_enable[0]["details"]["reasons"]
    )


@pytest.mark.asyncio
async def test_auto_enable_uses_manifest_explicit_high_risk(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    log_dir = home / "logs"
    proposals_dir = home / "proposals"
    _seed_decision_log(log_dir, ["external-admin", "docx"], count=5)
    _write_managed_skill(
        home,
        "external-admin",
        """---
name: external-admin
description: Controls an external service.
metadata:
  opensquilla:
    risk: high
---
# external-admin
""",
    )
    loader = _loader_with_managed_dir(home)
    skill_md = """---
name: synth-external-admin
kind: meta
triggers:
  - synth external admin
composition:
  steps:
    - id: admin
      skill: external-admin
      with:
        request: "{{ inputs.user_message | xml_escape | truncate(512) }}"
---
"""
    orch = _make_proposer_orchestrator(
        proposals_dir,
        proposal_ids=["b00fab1e"],
        skill_md=skill_md,
    )

    result = await auto_propose(
        orchestrator=orch,
        skill_loader=loader,
        log_dir=log_dir,
        min_freq=3,
        proposals_dir=proposals_dir,
        auto_enable=True,
        auto_enable_max_risk="medium",
    )

    assert result.proposals_enabled == []
    assert result.auto_enable[0]["status"] == "skipped"
    assert result.auto_enable[0]["risk_level"] == "high"
    assert "manifest_risk:external-admin:high" in (
        result.auto_enable[0]["details"]["reasons"]
    )


@pytest.mark.asyncio
async def test_auto_enable_requires_risk_metadata_for_unclassified_skill(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    log_dir = home / "logs"
    proposals_dir = home / "proposals"
    _seed_decision_log(log_dir, ["unclassified-helper", "docx"], count=5)
    _write_managed_skill(
        home,
        "unclassified-helper",
        """---
name: unclassified-helper
description: Synthetic skill intentionally missing risk metadata.
---
# unclassified-helper
""",
    )
    loader = _loader_with_managed_dir(home)
    skill_md = """---
name: synth-unclassified-helper
kind: meta
triggers:
  - synth unclassified helper
composition:
  steps:
    - id: helper
      skill: unclassified-helper
      with:
        text: "{{ inputs.user_message | xml_escape | truncate(512) }}"
---
"""
    orch = _make_proposer_orchestrator(
        proposals_dir,
        proposal_ids=["badc0de1"],
        skill_md=skill_md,
    )

    result = await auto_propose(
        orchestrator=orch,
        skill_loader=loader,
        log_dir=log_dir,
        min_freq=3,
        proposals_dir=proposals_dir,
        auto_enable=True,
        auto_enable_max_risk="low",
    )

    assert result.proposals_created == ["badc0de1"]
    assert result.proposals_enabled == []
    assert result.auto_enable[0]["status"] == "skipped"
    assert result.auto_enable[0]["reason"] == "risk_too_high"
    assert result.auto_enable[0]["risk_level"] == "high"
    assert "missing_risk_metadata:unclassified-helper" in (
        result.auto_enable[0]["details"]["reasons"]
    )


@pytest.mark.asyncio
async def test_pattern_fully_covered_by_existing_meta_skill_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_dir = tmp_path / "logs"
    proposals_dir = tmp_path / "proposals"
    # meta-paper-write composes multi-search-engine + paper-refbib-stub.
    # (The previous paper-experiment-stub + paper-plot-stub pair was
    # removed from the meta-skill after the experiment_design pipeline
    # rewrite, so we use the search→refbib pair which is still there.)
    _seed_decision_log(
        log_dir, ["multi-search-engine", "paper-refbib-stub"], count=5,
    )
    loader = _stub_loader_with_creator(monkeypatch)
    orch = _make_proposer_orchestrator(proposals_dir, proposal_ids=["aaaaaaaa"])

    result = await auto_propose(
        orchestrator=orch,
        skill_loader=loader,
        log_dir=log_dir,
        min_freq=3,
        proposals_dir=proposals_dir,
    )
    assert result.proposals_created == []
    assert any(s["reason"] == "already_covered" for s in result.skipped)
    orch.run.assert_not_called()


@pytest.mark.asyncio
async def test_duplicate_pending_proposal_is_skipped_by_chain_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_dir = tmp_path / "logs"
    proposals_dir = tmp_path / "proposals"
    # Pick a chain that no bundled meta-skill composes — otherwise the
    # already_covered branch fires first.
    chain = ["docx", "github"]
    _seed_decision_log(log_dir, chain, count=5)
    loader = _stub_loader_with_creator(monkeypatch)

    # Seed a pre-existing proposal carrying the same chain_hash
    from opensquilla.skills.creator.auto_propose import _chain_hash
    existing = proposals_dir / "dead1234"
    existing.mkdir(parents=True)
    (existing / "SKILL.md").write_text("---\nname: dup\nkind: meta\n---\n")
    (existing / "gates.json").write_text(json.dumps({
        "provenance": {"chain_hash": _chain_hash(chain)},
    }))

    orch = _make_proposer_orchestrator(proposals_dir, proposal_ids=["aaaaaaaa"])
    result = await auto_propose(
        orchestrator=orch,
        skill_loader=loader,
        log_dir=log_dir,
        min_freq=3,
        proposals_dir=proposals_dir,
    )
    assert result.proposals_created == []
    assert any(s["reason"] == "duplicate_pending" for s in result.skipped)
    orch.run.assert_not_called()


@pytest.mark.asyncio
async def test_orchestrator_exception_is_collected_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_dir = tmp_path / "logs"
    proposals_dir = tmp_path / "proposals"
    _seed_decision_log(log_dir, ["pdf-toolkit", "docx"], count=5)
    loader = _stub_loader_with_creator(monkeypatch)
    orch = _make_proposer_orchestrator(
        proposals_dir, raises=RuntimeError("provider blew up"),
    )

    result = await auto_propose(
        orchestrator=orch,
        skill_loader=loader,
        log_dir=log_dir,
        min_freq=3,
        proposals_dir=proposals_dir,
    )
    assert result.proposals_created == []
    assert len(result.errors) == 1
    assert "provider blew up" in result.errors[0]["error"]


@pytest.mark.asyncio
async def test_asyncio_cancelled_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_dir = tmp_path / "logs"
    proposals_dir = tmp_path / "proposals"
    _seed_decision_log(log_dir, ["pdf-toolkit", "docx"], count=5)
    loader = _stub_loader_with_creator(monkeypatch)
    orch = _make_proposer_orchestrator(
        proposals_dir, raises=asyncio.CancelledError(),
    )
    with pytest.raises(asyncio.CancelledError):
        await auto_propose(
            orchestrator=orch,
            skill_loader=loader,
            log_dir=log_dir,
            min_freq=3,
            proposals_dir=proposals_dir,
        )


@pytest.mark.asyncio
async def test_dag_produced_no_proposal_is_skipped_not_errored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the meta-skill-creator DAG completes but its lint/smoke gates
    fail mid-DAG (no proposal lands), classify as 'skipped', not error."""
    log_dir = tmp_path / "logs"
    proposals_dir = tmp_path / "proposals"
    _seed_decision_log(log_dir, ["pdf-toolkit", "docx"], count=5)
    loader = _stub_loader_with_creator(monkeypatch)
    # Empty proposal_ids list — DAG "runs" but writes nothing.
    orch = _make_proposer_orchestrator(proposals_dir, proposal_ids=[])

    result = await auto_propose(
        orchestrator=orch,
        skill_loader=loader,
        log_dir=log_dir,
        min_freq=3,
        proposals_dir=proposals_dir,
    )
    assert result.proposals_created == []
    assert any(s["reason"] == "dag_produced_no_proposal" for s in result.skipped)
    assert result.errors == []


@pytest.mark.asyncio
async def test_chain_with_only_meta_members_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When every member of a candidate chain is itself a meta-skill,
    skip — the runtime cannot nest meta-skills and the LLM would just
    waste a call producing a G1.2-rejected proposal."""
    log_dir = tmp_path / "logs"
    proposals_dir = tmp_path / "proposals"
    # Both members are real bundled meta-skills.
    _seed_decision_log(log_dir, ["meta-skill-creator", "meta-paper-write"], count=5)
    loader = _stub_loader_with_creator(monkeypatch)
    orch = _make_proposer_orchestrator(proposals_dir, proposal_ids=["aaaaaaaa"])

    result = await auto_propose(
        orchestrator=orch,
        skill_loader=loader,
        log_dir=log_dir,
        min_freq=3,
        proposals_dir=proposals_dir,
    )
    assert result.proposals_created == []
    assert any(s["reason"] == "only_meta_after_filter" for s in result.skipped)
    orch.run.assert_not_called()


@pytest.mark.asyncio
async def test_chain_with_mixed_members_keeps_only_non_meta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chain like [meta-X, A, B, C] should have meta-X stripped before
    being shown to the LLM. The proposal still gets attempted with A+B+C
    as the seed pattern."""
    log_dir = tmp_path / "logs"
    proposals_dir = tmp_path / "proposals"
    # meta-skill-creator is meta; docx + github are normal skills.
    _seed_decision_log(log_dir, ["meta-skill-creator", "docx", "github"], count=5)
    loader = _stub_loader_with_creator(monkeypatch)
    orch = _make_proposer_orchestrator(proposals_dir, proposal_ids=["abcd1234"])

    result = await auto_propose(
        orchestrator=orch,
        skill_loader=loader,
        log_dir=log_dir,
        min_freq=3,
        proposals_dir=proposals_dir,
    )
    # The DAG runs because two non-meta members survived the filter.
    assert result.proposals_created == ["abcd1234"]
    orch.run.assert_called_once()
    # And the provenance records the FILTERED chain (not the raw one).
    gates = json.loads((proposals_dir / "abcd1234" / "gates.json").read_text())
    assert gates["provenance"]["auto_propose_meta"]["skills"] == ["docx", "github"]


def test_synthesised_user_message_avoids_meta_skill_creator_triggers() -> None:
    """The synth message must NOT contain any meta-skill-creator trigger
    phrase — otherwise auto_propose could recursively trigger itself
    if the synth message were ever fed back into the resolver."""
    msg = _synthesise_user_message(["pdf-toolkit", "docx"], 5, 30)
    lower = msg.lower()
    for trig in _META_SKILL_CREATOR_TRIGGERS:
        assert trig.lower() not in lower, (
            f"synth message contains trigger {trig!r}: {msg!r}"
        )


def test_synthesise_user_message_raises_on_trigger_substring() -> None:
    """D6: the recursion guard inside ``_synthesise_user_message`` must
    fire even when ``python -O`` strips ``assert`` statements. Pass a
    skill list whose name interpolates one of the meta-skill-creator
    triggers into the synth message body and assert that
    ``RuntimeError`` is raised rather than the message silently being
    returned to the caller. A regression would let auto_propose
    re-fire the resolver against its own output."""
    # Use a real trigger phrase verbatim as a skill name. The synth
    # message concatenates ``", ".join(skills)`` into its body, so the
    # trigger substring will end up in the output unless the guard
    # rejects it. ``python -O`` would strip the prior ``assert`` form
    # and let the message through — the ``raise`` form keeps the
    # check active in every build.
    trigger_phrase = _META_SKILL_CREATOR_TRIGGERS[0]
    with pytest.raises(RuntimeError, match="recursively trigger"):
        _synthesise_user_message([trigger_phrase, "docx"], 5, 30)


def test_summary_string_shape() -> None:
    result = AutoProposeResult(
        proposals_created=["a", "b"],
        skipped=[{"reason": "x"}],
        errors=[{"error": "y"}],
        triggered_by="dream",
    )
    s = result.summary()
    assert "proposals=2" in s
    assert "skipped=1" in s
    assert "errors=1" in s
    assert "via=dream" in s


# ── D8: operator kill switch is honoured from every entry point ──


def test_is_auto_propose_disabled_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shared kill switch predicate reads
    ``OPENSQUILLA_AUTO_PROPOSE_DISABLED`` and only treats the literal
    value ``"1"`` as off — any other value (including ``"true"``,
    ``""``, ``"0"``) leaves auto-propose enabled."""
    monkeypatch.delenv("OPENSQUILLA_AUTO_PROPOSE_DISABLED", raising=False)
    assert is_auto_propose_disabled() is False

    monkeypatch.setenv("OPENSQUILLA_AUTO_PROPOSE_DISABLED", "1")
    assert is_auto_propose_disabled() is True

    monkeypatch.setenv("OPENSQUILLA_AUTO_PROPOSE_DISABLED", "true")
    assert is_auto_propose_disabled() is False

    monkeypatch.setenv("OPENSQUILLA_AUTO_PROPOSE_DISABLED", "0")
    assert is_auto_propose_disabled() is False


@pytest.mark.asyncio
async def test_auto_propose_short_circuits_on_kill_switch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """D8: the kill switch must halt ``auto_propose`` itself, not just
    the cron pre-check. Without the source-level guard, the dream
    callback (which bypasses the cron handler) would silently run the
    full pipeline. Pass mocks that would raise if invoked to prove the
    pipeline never starts."""
    monkeypatch.setenv("OPENSQUILLA_AUTO_PROPOSE_DISABLED", "1")
    loader = MagicMock()
    loader.get_by_name.side_effect = AssertionError(
        "skill_loader must not be touched once the kill switch is on",
    )
    orch = MagicMock()
    orch.run.side_effect = AssertionError(
        "orchestrator must not be invoked once the kill switch is on",
    )

    result = await auto_propose(
        orchestrator=orch,
        skill_loader=loader,
        log_dir=tmp_path / "logs",
        proposals_dir=tmp_path / "proposals",
        triggered_by="dream",
    )

    assert result.proposals_created == []
    assert result.proposals_enabled == []
    assert result.errors == []
    assert len(result.skipped) == 1
    assert result.skipped[0]["reason"] == "kill_switch_disabled"
    assert result.triggered_by == "dream"


def test_try_auto_enable_proposal_refuses_under_kill_switch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """D8: the manual creator persist path calls
    ``try_auto_enable_proposal`` directly (not the cron handler) and
    must also honour the kill switch. The wrapper returns a
    structured ``refused`` decision so the caller still sees a
    well-formed payload."""
    monkeypatch.setenv("OPENSQUILLA_AUTO_PROPOSE_DISABLED", "1")
    loader = MagicMock()
    loader.get_by_name.side_effect = AssertionError(
        "skill_loader must not be touched once the kill switch is on",
    )

    decision = try_auto_enable_proposal(
        proposals_dir=tmp_path / "proposals",
        proposal_id="abcd1234",
        skill_loader=loader,
        triggered_by="manual_persist",
        max_risk="low",
    )

    assert decision["decision"] == "refused"
    assert decision["reason"] == "kill_switch_disabled"
    assert decision["kill_switch"] is True
