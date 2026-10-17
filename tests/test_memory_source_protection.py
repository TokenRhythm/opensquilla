from __future__ import annotations

import pytest

from opensquilla.agents.scope import default_workspace_dir
from opensquilla.gateway.config import GatewayConfig
from opensquilla.memory.source_protection import SourceMaintenancePolicy, paths_overlap


def test_custom_state_keeps_maintenance_but_execution_overlap_does_not(tmp_path):
    project = tmp_path / "project"
    state = tmp_path / "custom-state"
    config = GatewayConfig(workspace_dir=str(project), state_dir=str(state))
    policy = SourceMaintenancePolicy(config)
    assert policy.allows("main", state / "agents/main", state / "agents/main/memory")
    assert not policy.bootstrap_allowed("main")
    assert not policy.allows("main", project, project / "memory")
    assert not policy.allows("main", state, project / "notes")
    assert not policy.allows("main", tmp_path)


def test_project_registration_dynamically_protects_default_agent_sources(tmp_path):
    roots: set[str] = set()
    policy = SourceMaintenancePolicy(GatewayConfig(), lambda: roots)
    assert policy.bootstrap_allowed("main")
    assert policy.bootstrap_allowed("side")
    roots.add(str(default_workspace_dir() / "memory"))
    assert not policy.bootstrap_allowed("main")
    assert policy.bootstrap_allowed("side")


def test_explicit_agent_workspace_protected_without_disabling_other_agent(tmp_path):
    config = GatewayConfig(agents=[{"id": "side", "workspace": str(tmp_path / "project")}])
    policy = SourceMaintenancePolicy(config)
    assert policy.bootstrap_allowed("main")
    assert not policy.bootstrap_allowed("side")


def test_explicit_default_workspace_is_shared_execution_root():
    config = GatewayConfig(workspace_dir=str(default_workspace_dir()))
    assert config.workspace_dir_source == "configured"
    assert not SourceMaintenancePolicy(config).bootstrap_allowed("main")


def test_overlap_uses_path_segments_and_native_aliases(tmp_path):
    assert paths_overlap(tmp_path / "a", tmp_path / "a/b")
    assert not paths_overlap(tmp_path / "a", tmp_path / "another")
    from opensquilla.paths import native_io_path

    assert paths_overlap(native_io_path(tmp_path / "a"), tmp_path / "a/b")


def test_cwd_data_needs_explicit_state_ownership(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert not SourceMaintenancePolicy(GatewayConfig()).legacy_data_migration_allowed("data")
    config = GatewayConfig(state_dir=str(tmp_path / "data"))
    policy = SourceMaintenancePolicy(config)
    assert policy.legacy_data_migration_allowed("data")
    config.workspace_dir = str(tmp_path)
    assert not policy.legacy_data_migration_allowed("data")


def test_bootstrap_only_seeds_independent_app_sources(tmp_path):
    from opensquilla.gateway.boot import _ensure_configured_agent_workspaces

    project = tmp_path / "project"
    project.mkdir()
    (project / "AGENTS.md").write_bytes(b"")
    _ensure_configured_agent_workspaces(GatewayConfig(workspace_dir=str(project)))
    assert list(project.iterdir()) == [project / "AGENTS.md"]
    _ensure_configured_agent_workspaces(GatewayConfig())
    assert (default_workspace_dir() / "USER.md").is_file()


@pytest.mark.asyncio
async def test_manager_preserves_mixed_project_sources_and_filters_generated_content(tmp_path):
    from opensquilla.memory.manager import build_memory_managers

    project = tmp_path / "project"
    notes = project / "memory"
    notes.mkdir(parents=True)
    raw = notes / "2020-01-01-reset-123.md"
    raw.write_text("# Raw flush (reset)\ntransient capture", encoding="utf-8")
    curated = notes / "archive/2020-01-01-notes.md"
    curated.parent.mkdir()
    curated.write_text("curated evergreen user note", encoding="utf-8")
    before = {path.relative_to(project): path.read_bytes() for path in project.rglob("*.md")}
    config = GatewayConfig(
        workspace_dir=str(project), state_dir=str(tmp_path / "state"),
        memory={"source": "workspace", "embedding": {"provider": "none"},
                "entry_ttl_days": 1, "ttl_sweep_interval_minutes": 1},
    )
    managers = await build_memory_managers(config, ["main"])
    try:
        after = {path.relative_to(project): path.read_bytes() for path in project.rglob("*.md")}
        assert after == before
        assert not (notes / ".raw_fallbacks").exists()
        paths = await managers["main"].store.list_paths()
        assert "memory/archive/2020-01-01-notes.md" in paths
        assert "memory/2020-01-01-reset-123.md" not in paths
    finally:
        await managers["main"].close()


@pytest.mark.asyncio
async def test_manager_does_not_seed_missing_workspace_memory(tmp_path):
    from opensquilla.memory.manager import build_memory_managers

    project = tmp_path / "project"
    config = GatewayConfig(
        workspace_dir=str(project), state_dir=str(tmp_path / "state"),
        memory={"source": "workspace", "embedding": {"provider": "none"}},
    )
    managers = await build_memory_managers(config, ["main"])
    try:
        assert not project.exists()
    finally:
        await managers["main"].close()
