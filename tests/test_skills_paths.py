from __future__ import annotations

from pathlib import Path

from opensquilla.skills.paths import resolve_skill_layer_dirs


def test_default_managed_dir_is_kept_before_directory_exists(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path))

    layer_dirs = resolve_skill_layer_dirs(allow_bundled=False)

    assert layer_dirs.managed_dir == tmp_path / "skills"
    assert not layer_dirs.managed_dir.exists()


def test_all_runtime_roots_are_kept_before_directories_exist(
    tmp_path, monkeypatch
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    layer_dirs = resolve_skill_layer_dirs(
        allow_bundled=False,
        workspace_root=workspace,
    )

    assert layer_dirs.workspace_dir == workspace / "skills"
    assert layer_dirs.personal_agents_dir == home / ".agents" / "skills"
    assert layer_dirs.project_agents_dir == workspace / ".agents" / "skills"
