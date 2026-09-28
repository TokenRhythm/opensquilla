"""Retired sandbox settings must not prevent old profiles from loading."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from opensquilla.gateway.config import GatewayConfig
from opensquilla.onboarding import config_store

_AUTO_SETUP_ENV = "OPENSQUILLA_GATEWAY_SANDBOX__AUTO_SETUP"
_RUN_MODE_ENV = "OPENSQUILLA_GATEWAY_SANDBOX__RUN_MODE"
_CPU_SECONDS_ENV = "OPENSQUILLA_GATEWAY_SANDBOX__CPU_SECONDS"


@pytest.fixture(autouse=True)
def isolate_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key in tuple(os.environ):
        if key.upper().startswith(("OPENSQUILLA_GATEWAY_", "OPENSQUILLA_SANDBOX_")):
            monkeypatch.delenv(key)
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "profile"))
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "user-state"))
    monkeypatch.chdir(tmp_path)


def _assert_settings(config: GatewayConfig, run_mode: str) -> None:
    assert config.sandbox.run_mode == run_mode
    assert config.sandbox.sandbox is (run_mode == "safe")
    assert config.sandbox.security_grading is (run_mode == "safe")
    assert config.sandbox.cpu_seconds == 31
    assert "auto_setup" not in config.sandbox.model_fields_set
    assert "auto_setup" not in config.sandbox.model_dump()


def _env_lines(auto_setup: bool, run_mode: str) -> str:
    return (
        f"{_AUTO_SETUP_ENV}={str(auto_setup).lower()}\n"
        f"{_RUN_MODE_ENV}={run_mode}\n"
        f"{_CPU_SECONDS_ENV}=31\n"
    )


@pytest.mark.parametrize("auto_setup", [True, False])
@pytest.mark.parametrize("run_mode", ["safe", "full"])
def test_gateway_payload_ignores_retired_auto_setup(auto_setup: bool, run_mode: str) -> None:
    config = GatewayConfig(
        sandbox={"auto_setup": auto_setup, "run_mode": run_mode, "cpu_seconds": 31},
    )

    _assert_settings(config, run_mode)


@pytest.mark.parametrize("source", ["nested-env", "json-env"])
@pytest.mark.parametrize("auto_setup", [True, False])
@pytest.mark.parametrize("run_mode", ["safe", "full"])
def test_gateway_environment_loaders_ignore_retired_auto_setup(
    source: str,
    auto_setup: bool,
    run_mode: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    if source == "nested-env":
        monkeypatch.setenv(_AUTO_SETUP_ENV, str(auto_setup).lower())
        monkeypatch.setenv(_RUN_MODE_ENV, run_mode)
        monkeypatch.setenv(_CPU_SECONDS_ENV, "31")
    else:
        monkeypatch.setenv(
            "OPENSQUILLA_GATEWAY_SANDBOX",
            json.dumps({"auto_setup": auto_setup, "run_mode": run_mode, "cpu_seconds": 31}),
        )
    missing = tmp_path / "missing.toml"
    existing = tmp_path / "config.toml"
    existing.write_text("port = 18791\n", encoding="utf-8")

    for config in (
        GatewayConfig(),
        GatewayConfig.load(missing),
        config_store.load_config(missing),
        GatewayConfig.load(existing),
        config_store.load_config(existing),
    ):
        _assert_settings(config, run_mode)
    assert not missing.exists()


@pytest.mark.parametrize("auto_setup", [True, False])
@pytest.mark.parametrize("run_mode", ["safe", "full"])
def test_pydantic_dotenv_ignores_retired_auto_setup(
    auto_setup: bool, run_mode: str, tmp_path: Path
) -> None:
    env_file = tmp_path / "settings.env"
    original = _env_lines(auto_setup, run_mode)
    env_file.write_text(original, encoding="utf-8")

    _assert_settings(GatewayConfig(_env_file=env_file), run_mode)
    assert env_file.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("auto_setup", [True, False])
@pytest.mark.parametrize("run_mode", ["safe", "full"])
def test_profile_dotenv_loads_in_a_fresh_process(
    auto_setup: bool, run_mode: str, tmp_path: Path
) -> None:
    profile = tmp_path / "profile"
    profile.mkdir()
    original = _env_lines(auto_setup, run_mode)
    (profile / ".env").write_text(original, encoding="utf-8")
    (profile / "config.toml").write_text("port = 18791\n", encoding="utf-8")
    cwd = tmp_path / "empty-cwd"
    cwd.mkdir()
    source = Path(__file__).resolve().parents[2] / "src"
    environment = {
        key: value for key, value in os.environ.items()
        if not key.upper().startswith("OPENSQUILLA_")
    }
    environment.update({
        "HOME": str(profile),
        "USERPROFILE": str(profile),
        "APPDATA": str(tmp_path / "appdata"),
        "LOCALAPPDATA": str(tmp_path / "localappdata"),
        "OPENSQUILLA_STATE_DIR": str(profile),
        "OPENSQUILLA_USER_STATE_DIR": str(tmp_path / "user-state"),
        "PYTHONPATH": str(source),
    })
    script = textwrap.dedent("""\
        import os
        import sys
        from pathlib import Path
        from opensquilla.env import load_env

        profile = Path(sys.argv[1])
        run_mode = sys.argv[2]
        assert load_env(cwd=Path.cwd(), home=profile) == 3
        assert os.environ["OPENSQUILLA_GATEWAY_SANDBOX__AUTO_SETUP"] == sys.argv[3]

        from opensquilla.gateway.config import GatewayConfig

        config = GatewayConfig.load(profile / "config.toml")
        assert config.sandbox.run_mode == run_mode
        assert config.sandbox.sandbox is (run_mode == "safe")
        assert config.sandbox.security_grading is (run_mode == "safe")
        assert config.sandbox.cpu_seconds == 31
        assert "auto_setup" not in config.sandbox.model_fields_set
        assert "auto_setup" not in config.sandbox.model_dump()
        print("retired-profile-setting-loaded")
        """)

    result = subprocess.run(
        [sys.executable, "-c", script, str(profile), run_mode, str(auto_setup).lower()],
        cwd=cwd, env=environment, text=True, capture_output=True, timeout=30, check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().endswith("retired-profile-setting-loaded")
    assert (profile / ".env").read_text(encoding="utf-8") == original


@pytest.mark.parametrize("auto_setup", [True, False])
@pytest.mark.parametrize("run_mode", ["safe", "full"])
def test_toml_migration_removes_retired_auto_setup_once(
    auto_setup: bool, run_mode: str, tmp_path: Path
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[sandbox]\n"
        f"auto_setup = {str(auto_setup).lower()}\n"
        f'run_mode = "{run_mode}"\n'
        "cpu_seconds = 31\n",
        encoding="utf-8",
    )

    _assert_settings(GatewayConfig.load(config_path), run_mode)
    migrated = config_path.read_bytes()
    assert "auto_setup" not in tomllib.loads(migrated.decode("utf-8"))["sandbox"]
    backups = set(tmp_path.glob("config.toml.backup.*"))
    assert len(backups) == 1
    _assert_settings(GatewayConfig.load(config_path), run_mode)
    _assert_settings(config_store.load_config(config_path), run_mode)
    assert config_path.read_bytes() == migrated
    assert set(tmp_path.glob("config.toml.backup.*")) == backups


@pytest.mark.parametrize("source", ["payload", "nested-env", "json-env", "dotenv"])
def test_unknown_sandbox_fields_remain_strict(
    source: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sandbox = {"auto_setup": False, "unexpected_setting": True}
    kwargs = {}
    if source == "payload":
        kwargs["sandbox"] = sandbox
    elif source == "json-env":
        monkeypatch.setenv("OPENSQUILLA_GATEWAY_SANDBOX", json.dumps(sandbox))
    else:
        lines = (
            f"{_AUTO_SETUP_ENV}=false\n"
            "OPENSQUILLA_GATEWAY_SANDBOX__UNEXPECTED_SETTING=true\n"
        )
        if source == "nested-env":
            for line in lines.splitlines():
                key, value = line.split("=", 1)
                monkeypatch.setenv(key, value)
        else:
            env_file = tmp_path / "settings.env"
            env_file.write_text(lines, encoding="utf-8")
            kwargs["_env_file"] = env_file

    with pytest.raises(ValidationError) as error:
        GatewayConfig(**kwargs)

    assert [(item["loc"], item["type"]) for item in error.value.errors()] == [
        (("sandbox", "unexpected_setting"), "extra_forbidden"),
    ]
