from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from opensquilla.env import load_env
from opensquilla.gateway.config import GatewayConfig
from opensquilla.paths import native_io_path

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows long-path regression")


def _long_home(tmp_path: Path) -> Path:
    segment = "config-home-" + ("x" * 38)
    home = tmp_path.joinpath(segment, segment, segment, segment)
    assert len(os.fspath(home)) > 260
    native_io_path(home).mkdir(parents=True)
    return home


def _clear_path_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "OPENSQUILLA_GATEWAY_STATE_DIR",
        "OPENSQUILLA_GATEWAY_WORKSPACE_DIR",
        "OPENSQUILLA_WORKSPACE_DIR",
    ):
        monkeypatch.delenv(name, raising=False)


def test_long_home_loads_config_and_injects_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _long_home(tmp_path)
    config_path = home / "config.toml"
    probe_name = "OPENSQUILLA_LONG_HOME_ENV_PROBE"
    _clear_path_overrides(monkeypatch)
    monkeypatch.delenv(probe_name, raising=False)
    native_io_path(config_path).write_text(
        "\n".join(
            (
                "config_version = 1",
                "port = 4242",
                'workspace_dir = "workspace"',
                'state_dir = "state"',
                "",
            )
        ),
        encoding="utf-8",
    )
    native_io_path(home / ".env").write_text(
        f"{probe_name}=loaded-from-long-home\n",
        encoding="utf-8",
    )
    empty_cwd = tmp_path / "empty-cwd"
    empty_cwd.mkdir()

    try:
        loaded = GatewayConfig.load(config_path)
        loaded_direct = GatewayConfig.load_from_toml(config_path)
        expected_workspace = str((home / "workspace").absolute())
        expected_state = str((home / "state").absolute())

        assert loaded.port == 4242
        assert loaded.workspace_dir == expected_workspace
        assert loaded.state_dir == expected_state
        assert loaded.config_path == os.fspath(config_path)
        assert not loaded.config_path.startswith("\\\\?\\")
        assert loaded_direct.port == 4242
        assert loaded_direct.workspace_dir == expected_workspace
        assert loaded_direct.state_dir == expected_state
        assert load_env(cwd=empty_cwd, home=home) == 1
        assert os.environ[probe_name] == "loaded-from-long-home"
    finally:
        shutil.rmtree(native_io_path(home), ignore_errors=True)


def test_long_home_config_migration_rewrites_and_backs_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _long_home(tmp_path)
    config_path = home / "config.toml"
    _clear_path_overrides(monkeypatch)
    native_io_path(config_path).write_text(
        "\n".join(
            (
                "[llm_ensemble]",
                "proposer_timeout_seconds = 300.0",
                "aggregator_timeout_seconds = 300.0",
                "",
            )
        ),
        encoding="utf-8",
    )

    try:
        loaded = GatewayConfig.load(config_path)
        rewritten = native_io_path(config_path).read_text(encoding="utf-8")
        backups = list(native_io_path(home).glob("config.toml.backup.*"))

        assert loaded.llm_ensemble.proposer_timeout_seconds == 3600.0
        assert loaded.llm_ensemble.aggregator_timeout_seconds == 3600.0
        assert loaded.config_path == os.fspath(config_path)
        assert not loaded.config_path.startswith("\\\\?\\")
        assert "config_version = 1" in rewritten
        assert "proposer_timeout_seconds = 3600.0" in rewritten
        assert len(backups) == 1
        assert "proposer_timeout_seconds = 300.0" in backups[0].read_text(
            encoding="utf-8"
        )
    finally:
        shutil.rmtree(native_io_path(home), ignore_errors=True)
