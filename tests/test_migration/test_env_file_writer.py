"""Behavior pins for the shared migration secret ``.env`` writer.

Complements ``test_secret_file_perms.py`` (which pins mode/atomicity/error
propagation through the full migrators): these tests cover the two
compatibility guarantees the consolidated writer adds — symlinked ``.env``
files are written through (the link survives), and a writable ``.env``
inside a non-writable directory falls back to an in-place rewrite instead
of failing the migration partway through.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from opensquilla.migration.env_file import write_secret_env_file


def test_write_secret_env_file_writes_through_symlink(tmp_path: Path) -> None:
    real = tmp_path / "dotfiles" / "env-store"
    real.parent.mkdir()
    real.write_text("EXISTING_KEY=keep-me\n", encoding="utf-8")
    link = tmp_path / ".env"
    try:
        link.symlink_to(real)
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows symbolic-link privilege is unavailable")
        raise

    write_secret_env_file(link, ["EXISTING_KEY=keep-me", "NEW_KEY=synthetic"])

    # The dotfiles-managed link must survive and the target must receive
    # the merged content — replacing the link with a regular file silently
    # disconnects the user's env store.
    assert link.is_symlink()
    content = real.read_text(encoding="utf-8")
    assert "EXISTING_KEY=keep-me" in content
    assert "NEW_KEY=synthetic" in content
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(real).st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are meaningless on Windows")
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root bypasses file modes"
)
def test_write_secret_env_file_falls_back_when_directory_not_writable(
    tmp_path: Path,
) -> None:
    home = tmp_path / "opensquilla-home"
    home.mkdir()
    env_path = home / ".env"
    env_path.write_text("OLD_KEY=old\n", encoding="utf-8")
    os.chmod(home, 0o555)  # directory read/execute only; file stays writable
    try:
        write_secret_env_file(env_path, ["OLD_KEY=old", "NEW_KEY=synthetic"])
    finally:
        os.chmod(home, 0o755)

    content = env_path.read_text(encoding="utf-8")
    assert "NEW_KEY=synthetic" in content
    assert "OLD_KEY=old" in content


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are meaningless on Windows")
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root bypasses file modes"
)
def test_write_secret_env_file_unwritable_dir_without_file_still_raises(
    tmp_path: Path,
) -> None:
    home = tmp_path / "opensquilla-home"
    home.mkdir()
    os.chmod(home, 0o555)
    try:
        with pytest.raises(PermissionError):
            write_secret_env_file(home / ".env", ["KEY=synthetic"])
    finally:
        os.chmod(home, 0o755)


def test_openclaw_migration_preserves_symlinked_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: a dotfiles-managed ~/.opensquilla/.env symlink survives a
    secrets migration, with the merged content written through the link."""
    from opensquilla.migration.openclaw import MigrationOptions, OpenClawMigrator

    source = tmp_path / ".openclaw"
    source.mkdir()
    (source / "openclaw.json").write_text(
        json.dumps({"agents": {"defaults": {}}}), encoding="utf-8"
    )
    (source / ".env").write_text("OPENAI_API_KEY=sk-dummy-openclaw\n", encoding="utf-8")

    home = tmp_path / "opensquilla-home"
    home.mkdir()
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))
    monkeypatch.delenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", raising=False)

    real = tmp_path / "dotfiles-env"
    real.write_text("EXISTING_KEY=keep-me\n", encoding="utf-8")
    try:
        (home / ".env").symlink_to(real)
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows symbolic-link privilege is unavailable")
        raise

    OpenClawMigrator(
        MigrationOptions(
            source=source,
            config_path=tmp_path / "config.toml",
            apply=True,
            migrate_secrets=True,
        )
    ).migrate()

    assert (home / ".env").is_symlink()
    content = real.read_text(encoding="utf-8")
    assert "EXISTING_KEY=keep-me" in content
    assert "OPENAI_API_KEY=sk-dummy-openclaw" in content
