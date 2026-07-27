from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from opensquilla.onboarding.config_store import load_config, persist_config
from opensquilla.paths import native_io_path

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows long-path regression")


def _long_home(tmp_path: Path) -> Path:
    segment = "onboarding-home-" + ("x" * 34)
    home = tmp_path.joinpath(segment, segment, segment, segment)
    assert len(os.fspath(home / "config.toml")) > 260
    native_io_path(home).mkdir(parents=True)
    return home


def test_long_config_load_persist_backup_roundtrip(tmp_path: Path) -> None:
    home = _long_home(tmp_path)
    config_path = home / "config.toml"
    original = "config_version = 1\nport = 4242\n"
    native_io_path(config_path).write_text(original, encoding="utf-8")

    try:
        config = load_config(config_path)
        assert config.port == 4242
        assert config.config_path == os.fspath(config_path)
        assert not config.config_path.startswith("\\\\?\\")

        config.port = 4243
        result = persist_config(
            config,
            path=config_path,
            backup=True,
            restart_required=True,
        )

        assert result.path == config_path
        assert not os.fspath(result.path).startswith("\\\\?\\")
        assert result.backup_path is not None
        assert result.backup_path.parent == config_path.parent
        assert not os.fspath(result.backup_path).startswith("\\\\?\\")
        assert result.restart_required is True
        assert native_io_path(result.backup_path).read_text(encoding="utf-8") == original
        assert "port = 4243" in native_io_path(config_path).read_text(encoding="utf-8")

        reloaded = load_config(config_path)
        assert reloaded.port == 4243
        assert reloaded.config_path == os.fspath(config_path)
    finally:
        shutil.rmtree(native_io_path(home), ignore_errors=True)


def test_long_missing_config_persist_creates_parent(tmp_path: Path) -> None:
    home = _long_home(tmp_path)
    config_path = home / "new" / "nested" / "config.toml"

    try:
        config = load_config(config_path)
        assert config.config_path == os.fspath(config_path)
        config.port = 4343

        result = persist_config(config, path=config_path, backup=True)

        assert result.path == config_path
        assert result.backup_path is None
        assert config.config_path == os.fspath(config_path)
        assert "port = 4343" in native_io_path(config_path).read_text(encoding="utf-8")
        assert load_config(config_path).port == 4343
    finally:
        shutil.rmtree(native_io_path(home), ignore_errors=True)
