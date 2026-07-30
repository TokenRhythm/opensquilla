from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from opensquilla.identity.bootstrap import ensure_agent_workspace
from opensquilla.identity.workspace import load_workspace_files


def _windows_native_path(path: Path) -> str:
    value = os.path.abspath(path)
    if value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return f"\\\\?\\UNC\\{value[2:]}"
    return f"\\\\?\\{value}"


def test_fresh_workspace_seeds_agents_template(tmp_path) -> None:
    result = ensure_agent_workspace(tmp_path)

    assert (tmp_path / "AGENTS.md").is_file()
    assert (tmp_path / "SOUL.md").is_file()
    assert (tmp_path / "USER.md").is_file()
    assert (tmp_path / "MEMORY.md").is_file()
    assert (tmp_path / "BOOTSTRAP.md").is_file()
    assert (tmp_path / "memory").is_dir()
    assert "AGENTS.md" in result.created_files
    assert "MEMORY.md" in result.created_files


def test_existing_workspace_backfills_missing_agents_template(tmp_path) -> None:
    (tmp_path / "SOUL.md").write_text("custom soul\n", encoding="utf-8")
    (tmp_path / "USER.md").write_text("custom user\n", encoding="utf-8")

    result = ensure_agent_workspace(tmp_path)

    assert (tmp_path / "AGENTS.md").is_file()
    assert "AGENTS.md" in result.created_files
    assert (tmp_path / "SOUL.md").read_text(encoding="utf-8") == "custom soul\n"
    assert (tmp_path / "USER.md").read_text(encoding="utf-8") == "custom user\n"


def test_seed_templates_false_does_not_create_agents_template(tmp_path) -> None:
    result = ensure_agent_workspace(tmp_path, seed_templates=False)

    assert not (tmp_path / "AGENTS.md").exists()
    assert result.created_files == ()


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows long paths")
def test_extended_length_workspace_bootstrap_keeps_logical_paths(tmp_path: Path) -> None:
    long_root = tmp_path / "long-workspace"
    workspace = long_root
    index = 0
    while len(str(workspace)) <= 275:
        workspace /= f"segment-{index:02d}-" + ("x" * 42)
        index += 1
    assert len(str(workspace)) > 260

    try:
        result = ensure_agent_workspace(workspace)

        assert result.workspace_dir == workspace
        assert result.state_path == workspace / ".opensquilla" / "workspace-state.json"
        assert result.bootstrap_path == workspace / "BOOTSTRAP.md"
        assert not str(result.workspace_dir).startswith("\\\\?\\")
        assert os.path.isfile(_windows_native_path(workspace / "AGENTS.md"))
        assert os.path.isfile(_windows_native_path(workspace / "BOOTSTRAP.md"))
        assert os.path.isdir(_windows_native_path(workspace / "memory"))
        with open(
            _windows_native_path(result.state_path),
            encoding="utf-8",
        ) as handle:
            state = json.load(handle)
        assert state["workspace_dir"] == str(workspace)

        loaded = load_workspace_files(workspace)
        assert "AGENTS.md" in loaded
        assert "USER.md" in loaded

        repeated = ensure_agent_workspace(workspace)
        assert repeated.workspace_dir == workspace
        assert repeated.created_files == ()
        assert repeated.bootstrap_seeded is True
    finally:
        native_root = _windows_native_path(long_root)
        if os.path.exists(native_root):
            shutil.rmtree(native_root)
