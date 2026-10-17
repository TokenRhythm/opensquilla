"""Safety primitives: containment + protected-root guards."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from opensquilla.uninstall import safety


def test_is_within_true_for_child(tmp_path: Path) -> None:
    root = tmp_path / "home"
    child = root / "state" / "sessions.db"
    child.parent.mkdir(parents=True)
    child.write_text("x")
    assert safety.is_within(child, root)
    assert safety.is_within(root, root)


def test_is_within_false_for_sibling(tmp_path: Path) -> None:
    root = tmp_path / "home"
    root.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    assert not safety.is_within(other, root)


def test_is_within_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "home"
    root.mkdir()
    outside = tmp_path / "secret"
    outside.mkdir()
    link = root / "link"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows symbolic-link privilege is unavailable")
        raise
    # The link lives under root, but it resolves outside it → not contained.
    assert not safety.is_within(link, root)


def test_protected_root_flags_home_and_shallow(monkeypatch, tmp_path: Path) -> None:
    fake_home = tmp_path / "user"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    assert safety.protected_root_reason(fake_home) is not None  # the home itself
    assert safety.protected_root_reason(fake_home.parent) is not None  # ancestor of home
    assert safety.protected_root_reason(Path("/")) is not None  # filesystem root
    assert safety.protected_root_reason(fake_home / "Documents") is not None  # personal dir


def test_protected_root_allows_normal_opensquilla_home(monkeypatch, tmp_path: Path) -> None:
    fake_home = tmp_path / "user"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    target = fake_home / ".opensquilla"
    target.mkdir()
    assert safety.protected_root_reason(target) is None
    assert not safety.is_protected_root(target)


def test_protected_root_flags_mount_point(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    (tmp_path / "user").mkdir()
    mountish = tmp_path / "vol"
    mountish.mkdir()
    # Simulate the path being a mount root (e.g. OPENSQUILLA_STATE_DIR=/Volumes/X).
    monkeypatch.setattr(safety.os.path, "ismount", lambda p: str(p) == str(mountish.resolve()))
    assert safety.protected_root_reason(mountish) is not None


def test_protected_root_flags_mount_container_children() -> None:
    # /Volumes/Drive, /mnt/nas, /srv/x, /media/x — directly under a mount container.
    for path in (Path("/Volumes/Drive"), Path("/mnt/nas"), Path("/srv/opensquilla")):
        assert safety.protected_root_reason(path) is not None, path


def test_protected_root_flags_top_level_home_dirs(monkeypatch, tmp_path: Path) -> None:
    fake_home = tmp_path / "user"
    (fake_home / "Dropbox").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    # A relocated home at ~/Dropbox (sync root) must be refused for blanket rmtree.
    assert safety.protected_root_reason(fake_home / "Dropbox") is not None
    # ...but the canonical ~/.opensquilla is allowed.
    (fake_home / ".opensquilla").mkdir()
    assert safety.protected_root_reason(fake_home / ".opensquilla") is None


def test_looks_like_opensquilla_home(tmp_path: Path) -> None:
    canonical = tmp_path / ".opensquilla"
    canonical.mkdir()
    assert safety.looks_like_opensquilla_home(canonical)  # by canonical name

    shaped = tmp_path / "weirdname"
    shaped.mkdir()
    assert not safety.looks_like_opensquilla_home(shaped)
    # Generic files are NOT sufficient — an arbitrary relocated dir could have them.
    (shaped / "config.toml").write_text("x")
    (shaped / "state").mkdir()
    assert not safety.looks_like_opensquilla_home(shaped)
    # The OpenSquilla-specific receipt IS an unambiguous signal.
    (shaped / "install-receipt.json").write_text("{}")
    assert safety.looks_like_opensquilla_home(shaped)
