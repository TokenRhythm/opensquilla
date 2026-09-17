from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from opensquilla.sandbox.guest_profile import (
    GuestProfileFactory,
    cleanup_guest_profile_root,
)
from opensquilla.sandbox.run_mode import RunMode


def test_guest_profile_mounts_default_workspace_and_bundled_runtime(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    state_dir = tmp_path / "state"
    profile = GuestProfileFactory.create(
        "task/unsafe",
        state_dir=state_dir,
        runtime_roots=(runtime,),
    )

    assert profile.host_home_mounted is False
    assert {mount.kind for mount in profile.mounts} == {
        "workspace",
        "home",
        "temp",
        "bundled-runtime",
    }
    assert profile.run_context().run_mode is RunMode.SAFE
    assert profile.run_context().workspace == str(profile.workspace)
    assert profile.home.parent == profile.root
    assert profile.temp.parent == profile.root

    profile.cleanup()


def test_guest_environment_does_not_inherit_host_secrets(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("OPENAI_API_KEY", "secret")

    profile = GuestProfileFactory.create("task", state_dir=tmp_path / "state")

    assert "AWS_SECRET_ACCESS_KEY" not in profile.environment
    assert "OPENAI_API_KEY" not in profile.environment
    assert profile.environment["HOME"] == str(profile.home)
    assert profile.environment["USERPROFILE"] == str(profile.home)
    assert profile.environment["PATH"] == ""
    profile.cleanup()


def test_guest_cleanup_removes_entire_task_root(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    profile = GuestProfileFactory.create("task", state_dir=state_dir)
    marker = profile.root / "result.txt"
    marker.write_text("guest", encoding="utf-8")
    root = profile.root

    profile.cleanup()
    profile.cleanup()

    assert not root.exists()
    assert profile.managed_root.exists()


def test_guest_cleanup_rejects_non_guest_directory(tmp_path: Path) -> None:
    ordinary = tmp_path / "ordinary"
    ordinary.mkdir()

    assert cleanup_guest_profile_root(
        ordinary,
        managed_root=tmp_path / "state-guest-workspaces",
    ) is False
    assert ordinary.exists()


def test_guest_profile_rejects_retargeted_scratch_parent(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    outside = tmp_path / "outside"
    state_dir.mkdir()
    outside.mkdir()
    scratch_parent = tmp_path / "state-guest-workspaces"
    try:
        scratch_parent.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        if os.name != "nt":
            pytest.skip(f"directory symlinks unavailable: {exc}")
        result = subprocess.run(
            ["cmd", "/d", "/c", "mklink", "/J", str(scratch_parent), str(outside)],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            pytest.skip(f"directory aliases unavailable: {result.stderr or result.stdout}")

    with pytest.raises(RuntimeError, match="GUEST_DEFAULT_WORKSPACE_UNSAFE"):
        GuestProfileFactory.create("task", state_dir=state_dir)

    assert list(outside.iterdir()) == []
