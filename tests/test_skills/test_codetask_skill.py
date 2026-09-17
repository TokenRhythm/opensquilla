"""Bundled code-task skill: manifest parses and declares its requirements."""

from __future__ import annotations

from pathlib import Path

import pytest

from opensquilla.skills.loader import SkillLoader

BUNDLED = (
    Path(__file__).resolve().parents[1].parent / "src" / "opensquilla" / "skills" / "bundled"
)


@pytest.fixture
def loader(tmp_path):
    loader = SkillLoader(bundled_dir=BUNDLED, snapshot_path=tmp_path / "snapshot.json")
    loader.invalidate_cache()
    loader.load_all()
    return loader


def test_codetask_skill_loads(loader):
    spec = loader.get_by_name("code-task")
    assert spec is not None
    assert spec.layer.value == "bundled"
    assert "real" in spec.description.lower() or "repository" in spec.description.lower()


def test_codetask_skill_declares_requirements(loader):
    spec = loader.get_by_name("code-task")
    assert spec is not None
    assert set(spec.requires_tools) == {"background_process", "exec_command", "process"}
    meta = spec.metadata
    assert meta is not None
    assert meta.requires is not None
    assert "git" in meta.requires.bins
    # No env-var requirement: the subagent inherits the operator's configured
    # provider (any provider), so gating on one vendor's key would hide the
    # skill from every non-OpenRouter user (issue #499).
    assert meta.requires.env == []


def test_codetask_skill_body_documents_cli_and_states(loader):
    spec = loader.get_by_name("code-task")
    assert spec is not None
    assert "opensquilla code-task solve" in spec.content
    # The six result states must be documented for the agent to interpret.
    for state in ("verified", "already_satisfied", "not_testable", "environment_blocked"):
        assert state in spec.content
    # gh fallback guidance must be present (codex review #7).
    assert "gh auth login" in spec.content
