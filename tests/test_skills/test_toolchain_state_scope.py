from __future__ import annotations

from pathlib import Path

import pytest

from opensquilla.skills.toolchains import manager


def test_toolchains_root_precedence_and_scope_restoration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    default_root = tmp_path / "default"
    environment_state = tmp_path / "environment-state"
    configured_state = tmp_path / "configured-state"
    nested_state = tmp_path / "nested-state"
    explicit_root = tmp_path / "explicit-root"

    monkeypatch.setattr(
        manager,
        "state_dir",
        lambda *parts: default_root.joinpath(*parts),
    )
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_STATE_DIR", str(environment_state))

    assert manager.toolchains_root(explicit_root) == explicit_root
    assert manager.toolchains_root() == environment_state / "toolchains" / "v1"

    with manager.managed_toolchain_state_scope(configured_state):
        assert manager.toolchains_root() == configured_state / "toolchains" / "v1"
        assert manager.toolchains_root(explicit_root) == explicit_root
        with manager.managed_toolchain_state_scope(nested_state):
            assert manager.toolchains_root() == nested_state / "toolchains" / "v1"
        assert manager.toolchains_root() == configured_state / "toolchains" / "v1"

    assert manager.toolchains_root() == environment_state / "toolchains" / "v1"
    monkeypatch.delenv("OPENSQUILLA_GATEWAY_STATE_DIR")
    assert manager.toolchains_root() == default_root / "toolchains" / "v1"


def test_managed_toolchain_state_scope_restores_after_exception(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    environment_state = tmp_path / "environment-state"
    configured_state = tmp_path / "configured-state"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_STATE_DIR", str(environment_state))

    with pytest.raises(RuntimeError, match="synthetic failure"):
        with manager.managed_toolchain_state_scope(configured_state):
            assert manager.toolchains_root() == configured_state / "toolchains" / "v1"
            raise RuntimeError("synthetic failure")

    assert manager.toolchains_root() == environment_state / "toolchains" / "v1"
