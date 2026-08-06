from __future__ import annotations

import json
import multiprocessing
import os
import sys
from pathlib import Path
from typing import Any

import pytest
import typer
from typer.testing import CliRunner


def _hold_runtime_writer(
    home: str,
    user_state: str,
    profile_kind: str,
    ready: Any,
    release: Any,
) -> None:
    """Hold the same universal writer lease used by gateway/agent runtimes."""

    os.environ["OPENSQUILLA_STATE_DIR"] = home
    os.environ["OPENSQUILLA_USER_STATE_DIR"] = user_state
    os.environ["OPENSQUILLA_TEST_PROFILE_LOCK_ROOT"] = "1"
    if profile_kind:
        os.environ["OPENSQUILLA_PROFILE_KIND"] = profile_kind
        os.environ["OPENSQUILLA_DESKTOP"] = "1"
    else:
        os.environ.pop("OPENSQUILLA_PROFILE_KIND", None)
        os.environ.pop("OPENSQUILLA_DESKTOP", None)

    from opensquilla.recovery import guarded_desktop_profile

    try:
        with guarded_desktop_profile(Path(home)):
            ready.put("locked")
            if not release.wait(timeout=15):
                raise TimeoutError("test did not release the runtime writer")
    except BaseException as exc:
        ready.put(f"error:{type(exc).__name__}:{exc}")
        raise


def _profile(home: Path) -> None:
    workspace = home / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "SOUL.md").write_text("synthetic runtime profile\n", encoding="utf-8")
    (home / "state").mkdir()
    (home / "config.toml").write_text(
        'state_dir = "state"\nworkspace_dir = "workspace"\n',
        encoding="utf-8",
    )


def test_future_desktop_config_blocks_agent_before_profile_seed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "profile"
    home.mkdir(parents=True)
    (home / "config.toml").write_text("config_version = 999\n", encoding="utf-8")
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "user-state"))
    monkeypatch.setenv("OPENSQUILLA_TEST_PROFILE_LOCK_ROOT", "1")
    monkeypatch.setenv("OPENSQUILLA_PROFILE_KIND", "desktop-primary")
    monkeypatch.setenv("OPENSQUILLA_DESKTOP", "1")

    from opensquilla.cli import main as cli_main
    from opensquilla.recovery import RecoveryRequiredError

    agent_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        cli_main,
        "run_agent_command",
        lambda **kwargs: agent_calls.append(dict(kwargs)),
    )

    result = CliRunner().invoke(
        cli_main.app,
        ["agent", "--message", "must not reach a provider", "--json"],
    )

    assert result.exit_code == 1
    assert isinstance(result.exception, RecoveryRequiredError)
    assert result.exception.report.stable_code == "config_schema_too_new"
    assert agent_calls == []
    assert not (home / "workspace").exists()
    assert not (home / "state").exists()


def test_unknown_desktop_layout_warns_without_seeding_or_blocking(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """unknown_layout is a warning now: the agent starts and nothing is seeded."""

    home = tmp_path / "profile"
    unknown = home / "unknown-layout"
    unknown.mkdir(parents=True)
    identity = unknown / "USER.md"
    identity.write_text("synthetic preserved identity\n", encoding="utf-8")
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "user-state"))
    monkeypatch.setenv("OPENSQUILLA_TEST_PROFILE_LOCK_ROOT", "1")
    monkeypatch.setenv("OPENSQUILLA_PROFILE_KIND", "desktop-primary")
    monkeypatch.setenv("OPENSQUILLA_DESKTOP", "1")

    from opensquilla.cli import main as cli_main

    agent_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        cli_main,
        "run_agent_command",
        lambda **kwargs: agent_calls.append(dict(kwargs)),
    )

    result = CliRunner().invoke(
        cli_main.app,
        ["agent", "--message", "reaches the runtime", "--json"],
    )

    assert result.exit_code == 0
    assert len(agent_calls) == 1
    assert identity.read_text(encoding="utf-8") == "synthetic preserved identity\n"
    assert not (home / "workspace").exists()


@pytest.mark.parametrize(
    "profile_kind",
    ["desktop-primary", ""],
    ids=["desktop-primary", "ordinary-cli"],
)
def test_runtime_writer_lock_keeps_read_only_cli_available_and_rejects_agent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    profile_kind: str,
) -> None:
    """A live writer excludes writers, never read-only gateway clients."""

    home = tmp_path / "profile"
    user_state = tmp_path / "user-state"
    _profile(home)
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(user_state))
    monkeypatch.setenv("OPENSQUILLA_TEST_PROFILE_LOCK_ROOT", "1")
    if profile_kind:
        monkeypatch.setenv("OPENSQUILLA_PROFILE_KIND", profile_kind)
        monkeypatch.setenv("OPENSQUILLA_DESKTOP", "1")
    else:
        monkeypatch.delenv("OPENSQUILLA_PROFILE_KIND", raising=False)
        monkeypatch.delenv("OPENSQUILLA_DESKTOP", raising=False)

    context = multiprocessing.get_context("spawn" if sys.platform == "win32" else "fork")
    ready = context.Queue()
    release = context.Event()
    writer = context.Process(
        target=_hold_runtime_writer,
        args=(str(home), str(user_state), profile_kind, ready, release),
    )
    writer.start()
    assert ready.get(timeout=10) == "locked"

    try:
        from opensquilla.cli import gateway_cmd, models_cmd
        from opensquilla.cli import main as cli_main
        from opensquilla.recovery import ProfileLockBusyError

        status_calls: list[dict[str, object]] = []

        def fake_status_gateway(**kwargs: object) -> None:
            status_calls.append(dict(kwargs))
            typer.echo(json.dumps({"status": "synthetic-running"}))

        model_calls: list[dict[str, object]] = []

        def fake_run_gateway_sync(_callback: object, **kwargs: object) -> dict[str, object]:
            model_calls.append(dict(kwargs))
            return {
                "models": [
                    {
                        "id": "synthetic/model",
                        "provider": "synthetic",
                        "capabilities": ["text"],
                    }
                ],
                "errors": [],
            }

        agent_calls: list[dict[str, object]] = []

        def fake_run_agent_command(**kwargs: object) -> None:
            agent_calls.append(dict(kwargs))

        monkeypatch.setattr(gateway_cmd, "status_gateway", fake_status_gateway)
        monkeypatch.setattr(models_cmd, "run_gateway_sync", fake_run_gateway_sync)
        monkeypatch.setattr(cli_main, "run_agent_command", fake_run_agent_command)
        runner = CliRunner()

        status = runner.invoke(cli_main.app, ["gateway", "status", "--json"])
        assert status.exit_code == 0, status.stdout
        assert json.loads(status.stdout) == {"status": "synthetic-running"}
        assert len(status_calls) == 1

        models = runner.invoke(cli_main.app, ["models", "list", "--json"])
        assert models.exit_code == 0, models.stdout
        assert json.loads(models.stdout) == [
            {
                "id": "synthetic/model",
                "provider": "synthetic",
                "capabilities": ["text"],
            }
        ]
        assert len(model_calls) == 1

        competing_agent = runner.invoke(
            cli_main.app,
            ["agent", "--message", "must not reach a provider", "--json"],
        )
        assert competing_agent.exit_code == 1
        assert isinstance(competing_agent.exception, ProfileLockBusyError)
        assert agent_calls == []
    finally:
        release.set()
        writer.join(timeout=10)
        if writer.is_alive():
            writer.terminate()
            writer.join(timeout=5)

    assert writer.exitcode == 0
