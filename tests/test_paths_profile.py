"""Tests for explicit OpenSquilla profile home resolution."""

from __future__ import annotations

import pytest

from opensquilla.paths import (
    default_opensquilla_home,
    default_profile_name,
    default_profiles_root,
    desktop_profile_lifecycle_active,
    is_valid_profile_name,
    profile_home,
    state_dir,
)

_VALID_NAMES = [
    "default",
    "agent-a",
    "agent_a",
    "a1",
    "0",
    "a" * 64,
    "abc-123_xyz",
]

_INVALID_NAMES = [
    "",
    "-leading-dash",
    "_leading-underscore",
    "UPPER",
    "MixedCase",
    "with spaces",
    "with/slash",
    "with\\backslash",
    "with..dot",
    "a" * 65,
    "../escape",
    "name?q=1",
    "中文",
    "name!",
    "name.with.dot",
    "name'quote",
    'name"quote',
]


@pytest.mark.parametrize(
    ("profile_kind", "desktop_env", "expected"),
    [
        (None, None, False),
        ("desktop-primary", None, True),
        ("desktop-recovery", "0", True),
        (" Desktop-Primary ", "false", True),
        ("cli", "1", False),
        ("unknown", "yes", False),
        (None, "1", True),
        (None, "true", True),
        (None, "yes", True),
        (None, "on", True),
        (" ", " TRUE ", True),
        (None, "0", False),
        (None, "false", False),
    ],
)
def test_desktop_profile_lifecycle_selection(
    monkeypatch, profile_kind, desktop_env, expected
) -> None:
    for key, value in (
        ("OPENSQUILLA_PROFILE_KIND", profile_kind),
        ("OPENSQUILLA_DESKTOP", desktop_env),
    ):
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)

    assert desktop_profile_lifecycle_active() is expected


@pytest.mark.parametrize("name", _VALID_NAMES)
def test_is_valid_profile_name_accepts(name: str) -> None:
    assert is_valid_profile_name(name)


@pytest.mark.parametrize("name", _INVALID_NAMES)
def test_is_valid_profile_name_rejects(name: str) -> None:
    assert not is_valid_profile_name(name)


def test_default_opensquilla_home_keeps_legacy_default(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("OPENSQUILLA_STATE_DIR", raising=False)
    monkeypatch.delenv("OPENSQUILLA_HOME", raising=False)
    monkeypatch.delenv("OPENSQUILLA_PROFILE", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    assert default_opensquilla_home() == tmp_path / ".opensquilla"


def test_default_profile_name_defaults_to_default(monkeypatch) -> None:
    monkeypatch.delenv("OPENSQUILLA_PROFILE", raising=False)
    assert default_profile_name() == "default"


def test_default_profile_name_trims_whitespace(monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_PROFILE", "  agent-a  ")
    assert default_profile_name() == "agent-a"


def test_default_profiles_root_uses_env(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("OPENSQUILLA_HOME", str(tmp_path / "profiles"))
    assert default_profiles_root() == tmp_path / "profiles"


def test_default_profiles_root_falls_back_under_legacy_home(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.delenv("OPENSQUILLA_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    assert default_profiles_root() == tmp_path / ".opensquilla" / "profiles"


def test_default_profiles_root_expands_tilde(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OPENSQUILLA_HOME", "~/my-profiles")

    assert default_profiles_root() == tmp_path / "my-profiles"


def test_profile_home_rejects_path_traversal(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("OPENSQUILLA_HOME", str(tmp_path / "profiles"))
    monkeypatch.setenv("OPENSQUILLA_PROFILE", "../escape")

    with pytest.raises(ValueError, match="Invalid OpenSquilla profile name"):
        profile_home()


def test_state_dir_override_wins_over_profile(monkeypatch, tmp_path) -> None:
    state_path = tmp_path / "pinned"
    profile_root = tmp_path / "profiles"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(state_path))
    monkeypatch.setenv("OPENSQUILLA_HOME", str(profile_root))
    monkeypatch.setenv("OPENSQUILLA_PROFILE", "agent-a")

    assert default_opensquilla_home() == state_path


def test_explicit_profile_uses_profiles_root(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("OPENSQUILLA_STATE_DIR", raising=False)
    profile_root = tmp_path / "profiles"
    monkeypatch.setenv("OPENSQUILLA_HOME", str(profile_root))
    monkeypatch.setenv("OPENSQUILLA_PROFILE", "agent-b")

    assert default_opensquilla_home() == profile_root / "agent-b"


def test_profile_without_home_uses_default_profiles_root(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("OPENSQUILLA_STATE_DIR", raising=False)
    monkeypatch.delenv("OPENSQUILLA_HOME", raising=False)
    monkeypatch.setenv("OPENSQUILLA_PROFILE", "coder")
    monkeypatch.setenv("HOME", str(tmp_path))

    assert default_opensquilla_home() == tmp_path / ".opensquilla" / "profiles" / "coder"


def test_profile_state_dirs_are_isolated(monkeypatch, tmp_path) -> None:
    profile_root = tmp_path / "profiles"
    monkeypatch.delenv("OPENSQUILLA_STATE_DIR", raising=False)
    monkeypatch.setenv("OPENSQUILLA_HOME", str(profile_root))

    monkeypatch.setenv("OPENSQUILLA_PROFILE", "agent-a")
    state_a = state_dir("agents", "main", "memory.db")

    monkeypatch.setenv("OPENSQUILLA_PROFILE", "agent-b")
    state_b = state_dir("agents", "main", "memory.db")

    assert state_a == profile_root / "agent-a" / "state" / "agents" / "main" / "memory.db"
    assert state_b == profile_root / "agent-b" / "state" / "agents" / "main" / "memory.db"
    assert state_a != state_b
