from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import tomli_w
from typer.testing import CliRunner

from opensquilla.cli.main import app

runner = CliRunner()

_EMPTY_MIGRATION_ENV_KEYS = (
    "OPENSQUILLA_PROFILE",
    "OPENSQUILLA_HOME",
    "OPENSQUILLA_GATEWAY_CONFIG_PATH",
    "OPENSQUILLA_GATEWAY_STATE_DIR",
    "OPENSQUILLA_GATEWAY_WORKSPACE_DIR",
    "OPENSQUILLA_WORKSPACE_DIR",
    "OPENSQUILLA_PROFILE_KIND",
    "OPENSQUILLA_DESKTOP_PROFILE_KIND",
    "OPENSQUILLA_DESKTOP",
)


@pytest.fixture(autouse=True)
def _isolate_migration_process_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep CLI migration discovery, writes, and locks inside this test's tree."""

    for key in _EMPTY_MIGRATION_ENV_KEYS:
        # Empty values block dotenv rehydration while remaining falsey to path selectors.
        monkeypatch.setenv(key, "")
    user_home = tmp_path / "user-home"
    user_home.mkdir()
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.setenv("USERPROFILE", str(user_home))
    runtime_temp = tmp_path / "runtime-temp"
    runtime_temp.mkdir()
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-appdata"))
    monkeypatch.setenv("TEMP", str(runtime_temp))
    monkeypatch.setenv("TMP", str(runtime_temp))
    monkeypatch.setenv("TMPDIR", str(runtime_temp))
    monkeypatch.setenv("OPENSQUILLA_TEST", "1")
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "opensquilla-home"))
    monkeypatch.setenv("OPENSQUILLA_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "user-state"))
    monkeypatch.chdir(tmp_path)


def _set_fake_home(monkeypatch, home: Path) -> None:
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))


def _make_source(root: Path) -> Path:
    source = root / ".openclaw"
    workspace = source / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "SOUL.md").write_text("soul\n", encoding="utf-8")
    (workspace / "MEMORY.md").write_text("memory\n", encoding="utf-8")
    (source / "openclaw.json").write_text(
        json.dumps({"agents": {"defaults": {"model": "deepseek-chat"}}}),
        encoding="utf-8",
    )
    return source


def _tree_snapshot(root: Path) -> dict[str, bytes | None]:
    return {
        path.relative_to(root).as_posix(): None if path.is_dir() else path.read_bytes()
        for path in sorted(root.rglob("*"))
    }


def test_migrate_pytest_isolates_inherited_process_paths(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    hostile_root = tmp_path / "hostile-parent-environment"
    hostile_cwd = hostile_root / "cwd"
    hostile_workspace = hostile_root / "workspace"
    hostile_state = hostile_root / "state"
    hostile_logs = hostile_root / "logs"
    hostile_user_state = hostile_root / "user-state"
    hostile_home = hostile_root / "home"
    runtime_temp = hostile_root / "runtime-temp"
    for directory in (
        hostile_cwd,
        hostile_workspace,
        hostile_state,
        hostile_logs,
        hostile_user_state,
        hostile_home,
        runtime_temp,
    ):
        directory.mkdir(parents=True)

    config_path = hostile_cwd / "opensquilla.toml"
    config_bytes = (
        b"# synthetic operator config must remain byte-for-byte unchanged\n"
        b"[llm]\n"
        b'provider = "openai"\n'
        b'model = "gpt-5"\n'
        b'api_key_env = "SYNTHETIC_OPENAI_API_KEY"\n'
    )
    config_path.write_bytes(config_bytes)
    protected_roots = (
        hostile_cwd,
        hostile_workspace,
        hostile_state,
        hostile_logs,
        hostile_user_state,
    )
    for root in protected_roots[1:]:
        (root / "keep.txt").write_text("keep\n", encoding="utf-8")
    before = {root: _tree_snapshot(root) for root in protected_roots}

    passthrough_keys = {
        "COMSPEC",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
    }
    child_env = {key: value for key, value in os.environ.items() if key.upper() in passthrough_keys}
    child_env.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": os.pathsep.join((str(repo_root / "src"), str(repo_root))),
            "HOME": str(hostile_home),
            "USERPROFILE": str(hostile_home),
            "TMPDIR": str(runtime_temp),
            "TMP": str(runtime_temp),
            "TEMP": str(runtime_temp),
            "APPDATA": str(hostile_root / "appdata"),
            "LOCALAPPDATA": str(hostile_root / "local-appdata"),
            "OPENSQUILLA_TEST": "1",
            "OPENSQUILLA_PROFILE": "operator",
            "OPENSQUILLA_HOME": str(hostile_root / "profiles"),
            "OPENSQUILLA_STATE_DIR": str(hostile_state),
            "OPENSQUILLA_LOG_DIR": str(hostile_logs),
            "OPENSQUILLA_USER_STATE_DIR": str(hostile_user_state),
            "OPENSQUILLA_GATEWAY_CONFIG_PATH": str(config_path),
            "OPENSQUILLA_GATEWAY_STATE_DIR": str(hostile_state),
            "OPENSQUILLA_GATEWAY_WORKSPACE_DIR": str(hostile_workspace),
            "OPENSQUILLA_WORKSPACE_DIR": str(hostile_workspace),
            "OPENSQUILLA_PROFILE_KIND": "desktop-primary",
            "OPENSQUILLA_DESKTOP_PROFILE_KIND": "desktop-primary",
            "OPENSQUILLA_DESKTOP": "1",
        }
    )
    test_file = Path(__file__).resolve()
    nodes = (
        f"{test_file}::test_migrate_auto_detect_single_source_auto_picks",
        f"{test_file}::test_migrate_openclaw_apply_writes_config_and_workspace",
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "--rootdir",
            str(repo_root),
            *nodes,
        ],
        cwd=hostile_cwd,
        env=child_env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert config_path.read_bytes() == config_bytes
    assert not list(hostile_cwd.glob("opensquilla.toml.backup.*"))
    assert {root: _tree_snapshot(root) for root in protected_roots} == before


def test_migrate_openclaw_json_dry_run(tmp_path: Path, monkeypatch) -> None:
    source = _make_source(tmp_path)
    home = tmp_path / "opensquilla-home"
    target = tmp_path / "config.toml"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))

    result = runner.invoke(
        app,
        [
            "migrate",
            "openclaw",
            "--source",
            str(source),
            "--config",
            str(target),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["apply"] is False
    assert not target.exists()
    assert any(item["status"] == "planned" for item in payload["items"])


def test_migrate_openclaw_apply_writes_config_and_workspace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _make_source(tmp_path)
    home = tmp_path / "opensquilla-home"
    target = tmp_path / "config.toml"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))

    result = runner.invoke(
        app,
        [
            "migrate",
            "openclaw",
            "--source",
            str(source),
            "--config",
            str(target),
            "--apply",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert "OpenClaw migration complete" in result.stdout
    assert (home / "workspace" / "SOUL.md").read_text(encoding="utf-8") == "soul\n"
    config = tomllib.loads(target.read_text(encoding="utf-8"))
    assert config["llm"]["model"] == "deepseek-chat"


def test_migrate_openclaw_missing_source_exits_nonzero(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "migrate",
            "openclaw",
            "--source",
            str(tmp_path / "missing"),
            "--json",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["items"][0]["status"] == "error"


def test_migrate_openclaw_exclude_skips_workspace_item(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _make_source(tmp_path)
    home = tmp_path / "opensquilla-home"
    target = tmp_path / "config.toml"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))

    result = runner.invoke(
        app,
        [
            "migrate",
            "openclaw",
            "--source",
            str(source),
            "--config",
            str(target),
            "--apply",
            "--exclude",
            "soul",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert not (home / "workspace" / "SOUL.md").exists()
    config = tomllib.loads(target.read_text(encoding="utf-8"))
    assert config["llm"]["model"] == "deepseek-chat"


def test_migrate_openclaw_rejects_unknown_include(tmp_path: Path) -> None:
    source = _make_source(tmp_path)

    result = runner.invoke(
        app,
        [
            "migrate",
            "openclaw",
            "--source",
            str(source),
            "--include",
            "not-a-real-option",
        ],
    )

    assert result.exit_code != 0
    assert "Unknown migration option" in result.stdout


def test_migrate_openclaw_rejects_unknown_preset(tmp_path: Path) -> None:
    source = _make_source(tmp_path)

    result = runner.invoke(
        app,
        [
            "migrate",
            "openclaw",
            "--source",
            str(source),
            "--preset",
            "everything",
        ],
    )

    assert result.exit_code != 0
    assert "Unknown migration preset" in result.stdout


def test_migrate_openclaw_rejects_unknown_skill_conflict(tmp_path: Path) -> None:
    source = _make_source(tmp_path)

    result = runner.invoke(
        app,
        [
            "migrate",
            "openclaw",
            "--source",
            str(source),
            "--skill-conflict",
            "merge",
        ],
    )

    assert result.exit_code != 0
    assert "Unknown skill conflict behavior" in result.stdout


# ---------------------------------------------------------------------------
# OpenSquilla self-migration CLI contract
# ---------------------------------------------------------------------------


def test_migrate_opensquilla_plain_error_reports_failure(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["migrate", "opensquilla", "--source", str(tmp_path / "missing")],
    )

    assert result.exit_code == 1
    assert "OpenSquilla self-migration failed" in result.stdout
    assert "error: 1" in result.stdout
    assert "complete" not in result.stdout.lower()


def test_migrate_opensquilla_json_error_is_parseable_and_exits_nonzero(
    tmp_path: Path,
) -> None:
    result = runner.invoke(
        app,
        [
            "migrate",
            "opensquilla",
            "--source",
            str(tmp_path / "missing"),
            "--json",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["items"][0]["status"] == "error"


def test_migrate_opensquilla_rejects_config_with_target_home_guidance(
    tmp_path: Path,
) -> None:
    result = runner.invoke(
        app,
        [
            "migrate",
            "opensquilla",
            "--source",
            str(tmp_path / "source"),
            "--config",
            str(tmp_path / "config.toml"),
        ],
    )

    assert result.exit_code == 2
    assert "--config is not supported for OpenSquilla self-migration" in result.stdout
    assert "OPENSQUILLA_STATE_DIR" in result.stdout
    assert "target home" in result.stdout


def test_migrate_opensquilla_keeps_its_internal_multi_profile_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla import recovery
    from opensquilla.cli import migrate_cmd

    source = tmp_path / "source-profile"
    (source / "workspace").mkdir(parents=True)
    (source / "state").mkdir()
    (source / "config.toml").write_text("port = 18790\n", encoding="utf-8")
    (source / "workspace" / "SOUL.md").write_text("source soul\n", encoding="utf-8")
    target = tmp_path / "desktop-primary"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(target))
    monkeypatch.setenv("OPENSQUILLA_PROFILE_KIND", "desktop-primary")
    monkeypatch.delattr(os, "fchmod", raising=False)

    def fail_foreign_guard() -> None:
        pytest.fail("OpenSquilla self-import must not take the foreign migration lifecycle")

    acquired: list[tuple[Path, ...]] = []
    original_acquire = recovery.acquire_profile_locks

    def track_internal_locks(*homes: str | Path, **kwargs: object) -> object:
        acquired.append(tuple(Path(home) for home in homes))
        return original_acquire(*homes, **kwargs)

    monkeypatch.setattr(migrate_cmd, "_guard_foreign_migration_target", fail_foreign_guard)
    monkeypatch.setattr(recovery, "acquire_profile_locks", track_internal_locks)

    result = runner.invoke(
        app,
        [
            "migrate",
            "opensquilla",
            "--source",
            str(source),
            "--apply",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert acquired == [(source, target, target.parent)]
    assert (target / "workspace" / "SOUL.md").read_text(encoding="utf-8") == (
        "source soul\n"
    )


# ---------------------------------------------------------------------------
# Auto-detect entry point: ``opensquilla migrate`` (no subcommand)
# ---------------------------------------------------------------------------


def _seed_openclaw(home: Path) -> Path:
    source = home / ".openclaw"
    workspace = source / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "SOUL.md").write_text("openclaw soul\n", encoding="utf-8")
    (workspace / "MEMORY.md").write_text("openclaw memory\n", encoding="utf-8")
    (source / "openclaw.json").write_text("{}", encoding="utf-8")
    return source


def _seed_opensquilla(home: Path) -> Path:
    source = home / ".opensquilla"
    source.mkdir(parents=True)
    (source / "config.toml").write_text("port = 18790\n", encoding="utf-8")
    return source


def _seed_portable(base: Path) -> Path:
    source = base / "OpenSquilla" / "portable" / "dummy-release"
    source.mkdir(parents=True)
    (source / "config.toml").write_text("port = 18790\n", encoding="utf-8")
    return source


def test_migrate_batch_rejects_config_when_opensquilla_is_selected(
    tmp_path: Path, monkeypatch
) -> None:
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    _seed_opensquilla(fake_home)
    _set_fake_home(monkeypatch, fake_home)
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "desktop-home"))

    result = runner.invoke(
        app,
        [
            "migrate",
            "--source",
            "opensquilla",
            "--config",
            str(tmp_path / "custom-config.toml"),
        ],
    )

    assert result.exit_code == 2, result.stdout
    assert "--config is not supported for OpenSquilla self-migration" in result.stdout


def _seed_hermes(home: Path) -> Path:
    source = home / ".hermes"
    source.mkdir(parents=True)
    (source / "config.yaml").write_text(
        "model:\n  provider: openrouter\n", encoding="utf-8"
    )
    (source / "SOUL.md").write_text("hermes soul\n", encoding="utf-8")
    return source


@pytest.mark.parametrize(
    ("source_name", "entrypoint"),
    [
        ("openclaw", "subcommand"),
        ("hermes", "subcommand"),
        ("openclaw", "auto-detect"),
        ("hermes", "auto-detect"),
    ],
)
def test_foreign_migration_blocks_unsafe_desktop_before_any_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_name: str,
    entrypoint: str,
) -> None:
    from opensquilla.recovery import RecoveryRequiredError

    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    source_path = (
        _seed_openclaw(fake_home)
        if source_name == "openclaw"
        else _seed_hermes(fake_home)
    )
    target = tmp_path / "desktop-primary"
    target.mkdir()
    missing_workspace = tmp_path / "missing-workspace"
    config = target / "config.toml"
    # config_version = 999 is the remaining hard startup gate.
    config_bytes = (
        "config_version = 999\n"
        f"workspace_dir = {json.dumps(str(missing_workspace))}\n"
    ).encode()
    config.write_bytes(config_bytes)
    _set_fake_home(monkeypatch, fake_home)
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(target))
    monkeypatch.setenv("OPENSQUILLA_PROFILE_KIND", "desktop-primary")

    if entrypoint == "subcommand":
        args = [
            "migrate",
            source_name,
            "--source",
            str(source_path),
            "--apply",
            "--json",
        ]
    else:
        args = ["migrate", "--source", source_name, "--apply", "--json"]
    result = runner.invoke(app, args)

    assert result.exit_code != 0
    assert isinstance(result.exception, RecoveryRequiredError)
    assert result.exception.report.stable_code == "config_schema_too_new"
    assert config.read_bytes() == config_bytes
    assert not missing_workspace.exists()
    assert not (target / "workspace").exists()
    assert not (target / "migration").exists()
    assert not (target / "state" / "gateway.pid.lock").exists()
    assert sorted(path.relative_to(target).as_posix() for path in target.rglob("*")) == [
        "config.toml"
    ]


def test_foreign_migration_guard_is_noop_for_ordinary_cli_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _make_source(tmp_path / "source-root")
    target = tmp_path / "cli-home"
    target.mkdir()
    workspace = tmp_path / "explicit-cli-workspace"
    (target / "config.toml").write_text(
        tomli_w.dumps({"workspace_dir": str(workspace)}),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(target))
    monkeypatch.delenv("OPENSQUILLA_PROFILE_KIND", raising=False)
    monkeypatch.delenv("OPENSQUILLA_DESKTOP", raising=False)

    result = runner.invoke(
        app,
        [
            "migrate",
            "openclaw",
            "--source",
            str(source),
            "--apply",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert (workspace / "SOUL.md").read_text(encoding="utf-8") == "soul\n"
    assert (target / "migration" / "openclaw").is_dir()
    assert not (target / "state" / "gateway.pid.lock").exists()


def test_migrate_auto_detect_no_source_reports_nothing(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "fake_home"
    home.mkdir()
    _set_fake_home(monkeypatch, home)
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "opensquilla"))

    result = runner.invoke(app, ["migrate", "--json"])

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["detected"] == []
    assert "No migration source detected" in payload["message"]
    assert "CLI, desktop, and portable locations" in payload["message"]


def test_migrate_auto_detect_single_source_auto_picks(
    tmp_path: Path, monkeypatch
) -> None:
    # Only hermes present: don't prompt, just run it.
    home = tmp_path / "fake_home"
    home.mkdir()
    _seed_hermes(home)
    _set_fake_home(monkeypatch, home)
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "opensquilla"))

    result = runner.invoke(app, ["migrate", "--apply", "--json"])

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["selected"] == ["hermes"]
    assert "hermes" in payload["reports"]


def test_migrate_auto_detect_single_cli_home_still_requires_confirmation(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "fake_home"
    home.mkdir()
    source = _seed_opensquilla(home)
    _set_fake_home(monkeypatch, home)
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "desktop-home"))

    result = runner.invoke(app, ["migrate", "--json"])

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["detected"] == [
        {
            "name": "opensquilla",
            "kind": "cli-home",
            "path": str(source),
            "version": None,
            "estimated_activity_at": payload["detected"][0]["estimated_activity_at"],
            "session_count": 0,
            "size_bytes": payload["detected"][0]["size_bytes"],
            "previously_imported": False,
        }
    ]
    assert payload["detected"][0]["estimated_activity_at"] is not None
    assert payload["detected"][0]["size_bytes"] > 0
    assert "explicit" in payload["message"].lower()
    assert "--source opensquilla" in payload["message"]


def test_migrate_auto_detect_single_portable_requires_explicit_confirmation(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "fake_home"
    home.mkdir()
    portable_base = tmp_path / "local-app-data"
    portable = _seed_portable(portable_base)
    _set_fake_home(monkeypatch, home)
    monkeypatch.setenv("LOCALAPPDATA", str(portable_base))
    monkeypatch.delenv("TEMP", raising=False)
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "opensquilla"))

    result = runner.invoke(app, ["migrate", "--json"])

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    candidate = payload["detected"][0]
    assert candidate["name"] == "opensquilla"
    assert candidate["kind"] == "windows-portable"
    assert candidate["path"] == str(portable)
    assert candidate["session_count"] == 0
    assert candidate["size_bytes"] > 0
    assert "estimated_activity_at" in candidate
    assert candidate["previously_imported"] is False
    assert "Re-run with" in payload["message"]


def test_migrate_opensquilla_single_portable_requires_home_flag(
    tmp_path: Path, monkeypatch
) -> None:
    portable_base = tmp_path / "local-app-data"
    portable = _seed_portable(portable_base)
    monkeypatch.setenv("LOCALAPPDATA", str(portable_base))
    monkeypatch.delenv("TEMP", raising=False)
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "target-home"))

    refused = runner.invoke(
        app, ["migrate", "opensquilla", "--kind", "windows-portable", "--json"]
    )
    accepted = runner.invoke(
        app,
        [
            "migrate",
            "opensquilla",
            "--kind",
            "windows-portable",
            "--home",
            str(portable),
            "--json",
        ],
    )

    assert refused.exit_code == 2
    refused_payload = json.loads(refused.stdout)
    assert refused_payload["requires_selection"] is True
    assert refused_payload["candidates"][0]["path"] == str(portable)
    assert refused_payload["candidates"][0]["session_count"] == 0
    assert "estimated_activity_at" in refused_payload["candidates"][0]
    assert accepted.exit_code == 0, accepted.stdout
    assert json.loads(accepted.stdout)["source"] == str(portable)


@pytest.mark.parametrize("kind", ["cli-home", "desktop-home"])
def test_migrate_opensquilla_direct_same_product_source_requires_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    import opensquilla.cli.migrate_cmd as migrate_cmd

    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    source = (
        _seed_opensquilla(fake_home)
        if kind == "cli-home"
        else tmp_path / "detected-desktop" / "opensquilla"
    )
    if kind == "desktop-home":
        source.mkdir(parents=True)
        (source / "config.toml").write_text("port = 18790\n", encoding="utf-8")
        monkeypatch.setattr(migrate_cmd, "detect_desktop_home", lambda: source)
    _set_fake_home(monkeypatch, fake_home)
    target = tmp_path / "target-home"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(target))

    refused = runner.invoke(
        app,
        ["migrate", "opensquilla", "--kind", kind, "--json"],
    )
    accepted = runner.invoke(
        app,
        [
            "migrate",
            "opensquilla",
            "--kind",
            kind,
            "--source",
            str(source),
            "--json",
        ],
    )

    assert refused.exit_code == 2
    payload = json.loads(refused.stdout)
    assert payload["requires_selection"] is True
    assert payload["candidates"][0]["path"] == str(source)
    assert not target.exists()
    assert accepted.exit_code == 0, accepted.stdout
    assert json.loads(accepted.stdout)["source"] == str(source)


def test_migrate_help_treats_cli_and_desktop_as_supported_profiles() -> None:
    group_help = runner.invoke(app, ["migrate", "--help"])
    command_help = runner.invoke(app, ["migrate", "opensquilla", "--help"])

    assert group_help.exit_code == 0
    assert command_help.exit_code == 0
    combined = f"{group_help.stdout}\n{command_help.stdout}"
    assert "legacy OpenSquilla home" not in combined
    assert "supported OpenSquilla CLI or Desktop profile" in combined
    assert "historical Windows Portable" in combined


def test_portable_text_chooser_labels_activity_as_estimated_and_shows_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:
    portable_base = tmp_path / "local-app-data"
    portable = _seed_portable(portable_base)
    (portable / "install-receipt.json").write_text(
        json.dumps({"version": "0.5.0rc3"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("LOCALAPPDATA", str(portable_base))
    monkeypatch.delenv("TEMP", raising=False)
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "target-home"))

    result = runner.invoke(
        app,
        ["migrate", "opensquilla", "--kind", "windows-portable"],
    )

    assert result.exit_code == 2
    assert str(portable) in result.stdout
    assert "version 0.5.0rc3" in result.stdout
    assert "estimated recent activity" in result.stdout
    assert "0 sessions" in result.stdout
    assert "bytes" in result.stdout


def test_migrate_opensquilla_inspect_candidate_json_is_metadata_only(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _seed_portable(tmp_path / "portable-base")
    (source / "install-receipt.json").write_text(
        json.dumps({"version": "0.5.0rc3"}),
        encoding="utf-8",
    )
    state = source / "state"
    state.mkdir()
    connection = sqlite3.connect(state / "sessions.db")
    try:
        connection.execute("CREATE TABLE sessions (session_key TEXT PRIMARY KEY, title TEXT)")
        connection.execute("INSERT INTO sessions VALUES (?, ?)", ("secret-id", "secret-title"))
        connection.commit()
    finally:
        connection.close()
    target = tmp_path / "target"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(target))

    result = runner.invoke(
        app,
        [
            "migrate",
            "opensquilla",
            "--source",
            str(source),
            "--kind",
            "windows-portable",
            "--inspect-candidate",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["candidate"]["version"] == "0.5.0rc3"
    assert payload["candidate"]["session_count"] == 1
    assert payload["candidate"]["path"] == str(source)
    assert "secret-id" not in result.stdout
    assert "secret-title" not in result.stdout
    assert not target.exists()


def test_migrate_opensquilla_replacement_flags_require_exact_target(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "legacy-home"
    source.mkdir()
    (source / "config.toml").write_text("port = 18791\n", encoding="utf-8")
    (source / "workspace").mkdir()
    (source / "state").mkdir()
    target = tmp_path / "target-home"
    target.mkdir()
    (target / "existing.txt").write_text("preserve", encoding="utf-8")
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(target))

    refused = runner.invoke(
        app,
        [
            "migrate",
            "opensquilla",
            "--source",
            str(source),
            "--apply",
            "--overwrite",
            "--json",
        ],
    )
    accepted = runner.invoke(
        app,
        [
            "migrate",
            "opensquilla",
            "--source",
            str(source),
            "--apply",
            "--replace-target",
            "--confirm-replace-target",
            str(target.resolve()),
            "--json",
        ],
    )

    assert refused.exit_code == 1
    assert "exact confirmation" in refused.stdout
    assert accepted.exit_code == 0, accepted.stdout
    assert not (target / "existing.txt").exists()
    assert (target / "config.toml").is_file()


def test_migrate_auto_detect_multiple_sources_non_tty_lists_and_exits(
    tmp_path: Path, monkeypatch
) -> None:
    # Both sources present and no --source filter: in non-TTY (CliRunner)
    # the user must opt in explicitly. We print the discovered sources
    # and exit 0 so CI doesn't silently migrate things.
    home = tmp_path / "fake_home"
    home.mkdir()
    _seed_openclaw(home)
    _seed_hermes(home)
    _set_fake_home(monkeypatch, home)
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "opensquilla"))

    result = runner.invoke(app, ["migrate", "--json"])

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    detected_names = [entry["name"] for entry in payload["detected"]]
    assert detected_names == ["openclaw", "hermes"]
    assert "Re-run with" in payload["message"]


def test_migrate_auto_detect_source_filter_runs_only_selected(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "fake_home"
    home.mkdir()
    _seed_openclaw(home)
    _seed_hermes(home)
    _set_fake_home(monkeypatch, home)
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "opensquilla"))

    result = runner.invoke(
        app, ["migrate", "--source", "hermes", "--apply", "--json"]
    )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["selected"] == ["hermes"]
    assert "openclaw" not in payload["reports"]


def test_migrate_auto_detect_source_filter_runs_both_in_order(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "fake_home"
    home.mkdir()
    _seed_openclaw(home)
    _seed_hermes(home)
    _set_fake_home(monkeypatch, home)
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "opensquilla"))

    result = runner.invoke(
        app,
        ["migrate", "--source", "hermes,openclaw", "--apply", "--json"],
    )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    # Order is canonical (openclaw first, then hermes) regardless of how
    # the user wrote the --source flag, so the second migrator sees
    # whatever the first one wrote.
    assert payload["selected"] == ["openclaw", "hermes"]
    assert set(payload["reports"]) == {"openclaw", "hermes"}


def test_migrate_auto_detect_rejects_unknown_source_name(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "fake_home"
    home.mkdir()
    _seed_openclaw(home)
    _set_fake_home(monkeypatch, home)
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "opensquilla"))

    result = runner.invoke(app, ["migrate", "--source", "bogus", "--json"])

    assert result.exit_code == 2, result.stdout
    assert "Unknown migration source" in result.stdout


def test_migrate_auto_detect_rejects_requested_but_undetected_source(
    tmp_path: Path, monkeypatch
) -> None:
    # ``--source hermes`` when hermes is not on disk should fail loudly
    # rather than silently no-op.
    home = tmp_path / "fake_home"
    home.mkdir()
    _seed_openclaw(home)
    _set_fake_home(monkeypatch, home)
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "opensquilla"))

    result = runner.invoke(app, ["migrate", "--source", "hermes", "--json"])

    assert result.exit_code == 2, result.stdout
    assert "not detected" in result.stdout


def test_migrate_auto_detect_tty_prompt_path_is_invoked(
    tmp_path: Path, monkeypatch
) -> None:
    # When both sources are present and stdin is a TTY (real interactive
    # use), we should reach the questionary prompt instead of the
    # non-TTY exit branch. Patch the prompt helper so the test doesn't
    # need a real terminal. ``--json`` short-circuits to the non-TTY
    # branch on purpose (scripting context), so this test uses plain
    # text output and checks that the migration actually ran via the
    # files it wrote.
    home = tmp_path / "fake_home"
    home.mkdir()
    _seed_openclaw(home)
    _seed_hermes(home)
    state = tmp_path / "opensquilla"
    _set_fake_home(monkeypatch, home)
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(state))

    from opensquilla.cli import migrate_cmd

    monkeypatch.setattr("opensquilla.cli.migrate_cmd._stdin_is_tty", lambda: True)
    captured: list[list[object]] = []

    def fake_prompt(detected):
        captured.append(list(detected))
        # User picks only hermes.
        return ["hermes"]

    monkeypatch.setattr(migrate_cmd, "_prompt_source_selection", fake_prompt)

    result = runner.invoke(app, ["migrate", "--apply"])

    assert result.exit_code == 0, result.stdout
    assert "hermes migration complete" in result.stdout
    # openclaw was offered but the fake prompt only picked hermes, so
    # the openclaw migrator must NOT have run.
    assert "openclaw migration complete" not in result.stdout
    assert len(captured) == 1
    assert {source.name for source in captured[0]} == {"openclaw", "hermes"}


def test_migrate_auto_detect_validates_all_selected_before_running_any(
    tmp_path: Path, monkeypatch
) -> None:
    # Pre-validate so an invalid flag for the second migrator never
    # half-applies the first one. ``persona_conflict`` is the openclaw-only
    # flag, so a bogus value for it must error out even though hermes
    # would happily ignore it.
    home = tmp_path / "fake_home"
    home.mkdir()
    _seed_openclaw(home)
    _seed_hermes(home)
    state = tmp_path / "opensquilla"
    _set_fake_home(monkeypatch, home)
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(state))

    result = runner.invoke(
        app,
        [
            "migrate",
            "--source",
            "openclaw,hermes",
            "--persona-conflict",
            "absolutely-bogus",
            "--apply",
        ],
    )

    assert result.exit_code == 2, result.stdout
    assert "Unknown persona conflict behavior" in result.stdout
    # Neither migrator should have left state behind: the workspace dir
    # is created by the first migrator's apply, so its absence is proof
    # we bailed before running anything.
    assert not (state / "workspace").exists()


def test_migrate_auto_detect_tty_prompt_cancellation_exits_cleanly(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "fake_home"
    home.mkdir()
    _seed_openclaw(home)
    _seed_hermes(home)
    _set_fake_home(monkeypatch, home)
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "opensquilla"))

    from opensquilla.cli import migrate_cmd

    monkeypatch.setattr("opensquilla.cli.migrate_cmd._stdin_is_tty", lambda: True)
    monkeypatch.setattr(migrate_cmd, "_prompt_source_selection", lambda _detected: [])

    result = runner.invoke(app, ["migrate"])

    assert result.exit_code == 0, result.stdout
    assert "No source selected" in result.stdout
