from __future__ import annotations

import asyncio
import contextlib
import json
import os
import platform
import signal
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace
from urllib.error import URLError

import pytest
from typer.testing import CliRunner

from opensquilla.cli import gateway_cmd, gateway_lifecycle
from opensquilla.cli.gateway_cmd import gateway_startup_guidance
from opensquilla.cli.main import app
from opensquilla.paths import default_opensquilla_home

runner = CliRunner()
Manager = gateway_lifecycle.GatewayLifecycleManager


def _env_command(env_key: str) -> str:
    # Mirrors next_steps.set_env_command: recovery ``command`` fields are the
    # bare command on every platform (no "PowerShell:" label).
    if platform.system().lower().startswith("win"):
        return f'$env:{env_key} = "<your-key>"'
    return f'export {env_key}="<your-key>"'


def _payload(result):
    return json.loads(result.stdout)


def _write_pidfile(record: dict) -> None:
    path = gateway_lifecycle.gateway_pidfile_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")


def _record(pid: int = 1234, *, port: int = 18791) -> dict:
    return {
        "pid": pid,
        "host": "127.0.0.1",
        "port": port,
        "url": f"http://127.0.0.1:{port}",
        "healthUrl": f"http://127.0.0.1:{port}/health",
        "logPath": str(gateway_lifecycle.gateway_log_path()),
        "startedAt": "2026-05-04T00:00:00Z",
        "argv": [
            sys.executable,
            "-m",
            "opensquilla.cli.main",
            "gateway",
            "run",
            "--listen",
            "127.0.0.1",
            "--port",
            str(port),
        ],
    }


def _patch_health(monkeypatch, value: bool) -> None:
    monkeypatch.setattr(Manager, "_probe_health", lambda self: value)


def _patch_wait_for_health(monkeypatch, value: bool) -> None:
    monkeypatch.setattr(Manager, "_wait_for_health", lambda self: value)


def _patch_pid_running(monkeypatch, value: bool) -> None:
    monkeypatch.setattr(Manager, "_pid_running", lambda self, pid: value)


def _profile_tree_snapshot(home: Path) -> dict[str, tuple[str, bytes]]:
    snapshot: dict[str, tuple[str, bytes]] = {}
    for path in sorted(home.rglob("*")):
        relative = path.relative_to(home).as_posix()
        snapshot[relative] = ("directory", b"") if path.is_dir() else ("file", path.read_bytes())
    return snapshot


def _unsafe_desktop_profile(home: Path, *, port: int = 0) -> None:
    state = home / "state"
    lifecycle = state / "gateway"
    lifecycle.mkdir(parents=True)
    missing_workspace = home.parent / "missing-workspace"
    (home / "config.toml").write_text(
        f"state_dir = {json.dumps(str(state))}\n"
        f"workspace_dir = {json.dumps(str(missing_workspace))}\n",
        encoding="utf-8",
    )
    (lifecycle / "gateway.json").write_text(
        json.dumps(_record(pid=999_999, port=port)),
        encoding="utf-8",
    )
    logs = home / "logs"
    logs.mkdir()
    (logs / "gateway.log").write_bytes(b"synthetic-existing-log\n")


def _safe_desktop_profile(home: Path) -> None:
    state = home / "state"
    workspace = home / "workspace"
    state.mkdir(parents=True)
    workspace.mkdir()
    (workspace / "SOUL.md").write_text("synthetic-safe-profile\n", encoding="utf-8")
    (home / "config.toml").write_text(
        f"state_dir = {json.dumps(str(state))}\n"
        f"workspace_dir = {json.dumps(str(workspace))}\n",
        encoding="utf-8",
    )


class _FakeHealthResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None


def test_gateway_startup_guidance_shows_operator_next_steps() -> None:
    guidance = gateway_startup_guidance("127.0.0.1", 18791)

    assert "[bold]Web UI:[/bold] http://127.0.0.1:18791/control/" in guidance
    assert "[bold]API base:[/bold] http://127.0.0.1:18791" in guidance
    debug_log = default_opensquilla_home() / "logs" / "debug.log"
    assert f"[bold]Debug log:[/bold] {debug_log}" in guidance
    assert "[dim]Keep this terminal open. Press Ctrl+C to stop.[/dim]" in guidance


def test_gateway_run_turns_missing_onboarding_env_into_recovery_hint(
    tmp_path,
    monkeypatch,
) -> None:
    target = tmp_path / "custom.toml"
    target.write_text(
        '[llm]\n'
        'provider = "openrouter"\n'
        'model = "deepseek/deepseek-v4-flash"\n'
        'api_key = "sk-or"\n'
        '\n'
        '[memory.embedding]\n'
        'provider = "openai"\n'
        '\n'
        '[memory.embedding.remote]\n'
        'api_key_env = "OPENAI_EMBEDDINGS_API_KEY"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "home"))
    monkeypatch.delenv("OPENAI_EMBEDDINGS_API_KEY", raising=False)

    async def fail_start_gateway_server(**_kwargs):
        raise ValueError(
            "memory.embedding.provider='openai' requires "
            "memory.embedding.remote.api_key"
        )

    monkeypatch.setattr(gateway_cmd, "_gateway_bind_available", lambda *_args: True)
    monkeypatch.setattr(gateway_cmd, "start_gateway_server", fail_start_gateway_server)

    result = runner.invoke(app, ["gateway", "run", "--config", str(target)])

    assert result.exit_code == 1
    output = result.stdout + (result.stderr or "")
    compact = "".join(output.split())
    assert "Gateway could not start" in output
    assert (
        f"Set memory key: {_env_command('OPENAI_EMBEDDINGS_API_KEY')}".replace(" ", "")
        in compact
    )
    expected_config = str(target).replace("\\", "/")
    normalized = compact.replace("\\", "/")
    assert "opensquillaonboardstatus--config" in normalized
    assert expected_config in normalized
    assert normalized.index("opensquillaonboardstatus--config") < normalized.index(
        expected_config
    )
    assert "Traceback" not in output


def test_gateway_run_memory_recovery_command_is_bare_on_windows(
    tmp_path,
    monkeypatch,
) -> None:
    """The recovery ``command`` field is machine-shaped on every surface: the
    gateway-run fallback entry must carry the bare set-env command on Windows
    (no "PowerShell:" label), matching env_recovery_commands."""
    from opensquilla.onboarding import next_steps

    target = tmp_path / "custom.toml"
    target.write_text(
        '[llm]\n'
        'provider = "openrouter"\n'
        'model = "dummy/model"\n'
        'api_key = "sk-dummy"\n'
        '\n'
        '[memory.embedding.remote]\n'
        'api_key_env = "DUMMY_UNSET_EMBED_KEY"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "home"))
    monkeypatch.delenv("DUMMY_UNSET_EMBED_KEY", raising=False)
    monkeypatch.setattr(next_steps, "_is_windows", lambda: True)

    async def fail_start_gateway_server(**_kwargs):
        raise ValueError(
            "memory.embedding.provider='openai' requires "
            "memory.embedding.remote.api_key"
        )

    monkeypatch.setattr(gateway_cmd, "_gateway_bind_available", lambda *_args: True)
    monkeypatch.setattr(gateway_cmd, "start_gateway_server", fail_start_gateway_server)

    result = runner.invoke(app, ["gateway", "run", "--config", str(target)])

    assert result.exit_code == 1
    output = result.stdout + (result.stderr or "")
    compact = "".join(output.split())
    expected = 'Set memory key: $env:DUMMY_UNSET_EMBED_KEY = "<your-key>"'
    assert expected.replace(" ", "") in compact
    assert "PowerShell" not in output


def test_gateway_lifecycle_paths_use_state_root(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "home"))

    assert gateway_lifecycle.gateway_pidfile_path() == (
        tmp_path / "home" / "state" / "gateway" / "gateway.json"
    )
    assert gateway_lifecycle.gateway_log_path() == tmp_path / "home" / "logs" / "gateway.log"


def test_safe_desktop_gateway_start_uses_external_lifecycle_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "opensquilla"
    user_state = tmp_path / "user-state"
    _safe_desktop_profile(home)
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))
    monkeypatch.setenv("OPENSQUILLA_PROFILE_KIND", "desktop-primary")
    monkeypatch.setenv("OPENSQUILLA_TEST", "1")
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(user_state))
    calls = []

    def fake_popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(pid=4242)

    monkeypatch.setattr(gateway_lifecycle.subprocess, "Popen", fake_popen)
    _patch_health(monkeypatch, False)
    _patch_wait_for_health(monkeypatch, True)

    result = runner.invoke(app, ["gateway", "start", "--port", "0", "--json"])

    assert result.exit_code == 0, result.stdout
    payload = _payload(result)
    pidfile = Path(payload["pidfile"])
    log_path = Path(payload["logPath"])
    assert payload["state"] == "running"
    assert calls
    assert home not in pidfile.parents
    assert home not in log_path.parents
    assert user_state in pidfile.parents
    assert user_state in log_path.parents


@pytest.mark.parametrize("action", ["start", "restart"])
def test_unsafe_desktop_gateway_lifecycle_blocks_before_spawn_or_write(
    action: str,
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "opensquilla"
    user_state = tmp_path / "user-state"
    _unsafe_desktop_profile(home)
    before = _profile_tree_snapshot(home)
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))
    monkeypatch.setenv("OPENSQUILLA_PROFILE_KIND", "desktop-primary")
    monkeypatch.setenv("OPENSQUILLA_TEST", "1")
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(user_state))

    def fail_spawn(self, argv):
        raise AssertionError("unsafe Desktop profile must not spawn a gateway")

    monkeypatch.setattr(Manager, "_spawn_gateway", fail_spawn)

    result = getattr(Manager(port=0, health_timeout=0), action)()

    assert result.ok is False
    assert result.state == "recovery_required"
    assert result.code == "DESKTOP_PROFILE_RECOVERY_REQUIRED"
    assert result.details["stableCode"] == "effective_workspace_missing"
    assert _profile_tree_snapshot(home) == before
    assert not user_state.exists()


@pytest.mark.parametrize("action", ["start", "restart"])
def test_desktop_lifecycle_rejects_config_outside_profile_before_write(
    action: str,
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "opensquilla"
    user_state = tmp_path / "user-state"
    _safe_desktop_profile(home)
    outside = tmp_path / "outside.toml"
    outside.write_text("synthetic = true\n", encoding="utf-8")
    before = _profile_tree_snapshot(home)
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))
    monkeypatch.setenv("OPENSQUILLA_PROFILE_KIND", "desktop-primary")
    monkeypatch.setenv("OPENSQUILLA_TEST", "1")
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(user_state))

    result = getattr(
        Manager(config_path=str(outside), port=0, health_timeout=0),
        action,
    )()

    assert result.ok is False
    assert result.code == "DESKTOP_PROFILE_RECOVERY_REQUIRED"
    assert result.details["stableCode"] == "desktop_config_outside_profile"
    assert _profile_tree_snapshot(home) == before
    assert not user_state.exists()


def test_desktop_gateway_run_rejects_config_outside_profile_before_loading_it(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "opensquilla"
    _safe_desktop_profile(home)
    outside = tmp_path / "outside.toml"
    outside.write_text("synthetic = true\n", encoding="utf-8")
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(home))
    monkeypatch.setenv("OPENSQUILLA_PROFILE_KIND", "desktop-primary")

    def fail_load(*_args, **_kwargs):
        raise AssertionError("out-of-profile config must be rejected before load")

    monkeypatch.setattr(gateway_cmd.GatewayConfig, "load", fail_load)

    result = runner.invoke(app, ["gateway", "run", "--config", str(outside)])

    assert result.exit_code == 1
    assert "DESKTOP_CONFIG_OUTSIDE_PROFILE" in result.stdout


@pytest.mark.parametrize(
    "lock_error_name",
    ["ProfileLockBusyError", "LegacyGatewayRunningError"],
)
def test_gateway_run_emits_stable_profile_in_use_error_without_sensitive_path(
    tmp_path: Path,
    monkeypatch,
    lock_error_name: str,
) -> None:
    from opensquilla import recovery

    sensitive_profile = tmp_path / "customer-private-profile"
    lock_error = getattr(recovery, lock_error_name)

    @contextlib.contextmanager
    def busy_profile_guard():
        raise lock_error(
            f"profile is in use by another writer: {sensitive_profile}"
        )
        yield  # pragma: no cover - contextmanager shape only

    def fail_run_gateway(**_kwargs) -> None:
        raise AssertionError("gateway must not start without the profile lock")

    monkeypatch.setattr(recovery, "guarded_desktop_profile", busy_profile_guard)
    monkeypatch.setattr(gateway_cmd, "run_gateway", fail_run_gateway)

    result = runner.invoke(app, ["gateway", "run"])

    assert result.exit_code == 1
    output = result.stdout + (result.stderr or "")
    assert output.count("OPENSQUILLA_PROFILE_IN_USE") == 1
    assert "Another OpenSquilla process is still using this profile" in output
    assert "restart the computer" in output
    assert "Do not delete profile lock files" in output
    assert str(sensitive_profile) not in output
    assert lock_error_name not in output
    assert "Traceback" not in output


def test_gateway_help_lists_lifecycle_commands() -> None:
    result = runner.invoke(app, ["gateway", "--help"])

    assert result.exit_code == 0
    assert "run" in result.stdout
    assert "start" in result.stdout
    assert "status" in result.stdout
    assert "stop" in result.stdout
    assert "restart" in result.stdout


def test_gateway_subapp_disables_pretty_exceptions() -> None:
    from opensquilla.cli.main import gateway_app

    assert gateway_app.pretty_exceptions_enable is False


def test_gateway_start_help_explains_config_backed_target_defaults() -> None:
    result = runner.invoke(app, ["gateway", "start", "--help"])

    assert result.exit_code == 0
    assert "Port to bind (default: config port, usually 18791)" in result.stdout
    assert "Host to bind (default: config host, usually 127.0.0.1)" in result.stdout


def test_gateway_status_json_reports_not_started(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "home"))
    _patch_health(monkeypatch, False)

    result = runner.invoke(app, ["gateway", "status", "--json"])

    assert result.exit_code == 0
    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["state"] == "not_started"
    assert payload["managed"] is False


def test_gateway_status_gateway_url_probes_remote_https_health(monkeypatch) -> None:
    urls = []

    def fake_urlopen(request, timeout):
        urls.append(request.full_url)
        assert timeout == 0.5
        return _FakeHealthResponse()

    monkeypatch.setattr(gateway_lifecycle, "urlopen", fake_urlopen)

    result = runner.invoke(
        app,
        ["gateway", "status", "--gateway", "https://squilla.example.com", "--json"],
    )

    assert result.exit_code == 0, result.stdout
    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["remote"] is True
    assert payload["managed"] is False
    assert payload["state"] == "running"
    assert payload["gatewayUrl"] == "wss://squilla.example.com/ws"
    assert payload["url"] == "https://squilla.example.com"
    assert payload["healthUrl"] == "https://squilla.example.com/health"
    assert urls == ["https://squilla.example.com/health"]


def test_gateway_status_gateway_url_reports_remote_unavailable(monkeypatch) -> None:
    urls = []

    def fake_urlopen(request, timeout):
        urls.append(request.full_url)
        assert timeout == 0.5
        raise OSError("offline")

    monkeypatch.setattr(gateway_lifecycle, "urlopen", fake_urlopen)

    result = runner.invoke(
        app,
        ["gateway", "status", "--gateway", "wss://squilla.example.com/ws", "--json"],
    )

    assert result.exit_code == 1, result.stdout
    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["remote"] is True
    assert payload["managed"] is False
    assert payload["state"] == "unavailable"
    assert payload["code"] == "REMOTE_GATEWAY_UNAVAILABLE"
    assert payload["gatewayUrl"] == "wss://squilla.example.com/ws"
    assert payload["url"] == "https://squilla.example.com"
    assert payload["healthUrl"] == "https://squilla.example.com/health"
    assert urls == [
        "https://squilla.example.com/health",
        "https://squilla.example.com/healthz",
    ]
    assert [attempt["errorType"] for attempt in payload["details"]["attempts"]] == [
        "OSError",
        "OSError",
    ]


def test_gateway_status_reports_stale_without_mutating_pidfile(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "home"))
    _write_pidfile(_record(pid=9999))
    before = gateway_lifecycle.gateway_pidfile_path().read_text(encoding="utf-8")
    _patch_pid_running(monkeypatch, False)
    _patch_health(monkeypatch, False)

    result = runner.invoke(app, ["gateway", "status", "--json"])

    assert result.exit_code == 0
    assert _payload(result)["state"] == "stale"
    assert gateway_lifecycle.gateway_pidfile_path().read_text(encoding="utf-8") == before


def test_gateway_start_refuses_unmanaged_healthy_gateway(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "home"))
    _patch_health(monkeypatch, True)

    result = runner.invoke(app, ["gateway", "start", "--json"])

    assert result.exit_code == 3
    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["state"] == "unmanaged"
    assert payload["code"] == "UNMANAGED_GATEWAY_RUNNING"
    assert "http://127.0.0.1:18791" in payload["message"]
    assert "host=127.0.0.1" in payload["message"]
    assert "port=18791" in payload["message"]
    assert not gateway_lifecycle.gateway_pidfile_path().exists()


def test_gateway_start_uses_same_interpreter_cli_boundary(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "home"))
    calls = []

    def fake_popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(pid=4242)

    monkeypatch.setattr(gateway_lifecycle.subprocess, "Popen", fake_popen)
    _patch_health(monkeypatch, False)
    _patch_wait_for_health(monkeypatch, True)

    result = runner.invoke(
        app,
        ["gateway", "start", "--listen", "127.0.0.2", "--port", "18888", "--json"],
    )

    assert result.exit_code == 0, result.stdout
    payload = _payload(result)
    assert payload["state"] == "running"
    assert payload["pid"] == 4242
    argv, kwargs = calls[0]
    assert argv[:5] == [sys.executable, "-m", "opensquilla.cli.main", "gateway", "run"]
    assert "--listen" in argv
    assert argv[argv.index("--listen") + 1] == "127.0.0.2"
    assert kwargs["shell"] is False


def test_gateway_start_frozen_binary_invokes_subcommand_directly(tmp_path, monkeypatch) -> None:
    # In a PyInstaller-frozen desktop bundle sys.executable already is the CLI
    # entrypoint, so "-m opensquilla.cli.main" would be handed to Typer as
    # arguments and the child would exit on a usage error.
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "home"))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    calls = []

    def fake_popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(pid=4243)

    monkeypatch.setattr(gateway_lifecycle.subprocess, "Popen", fake_popen)
    _patch_health(monkeypatch, False)
    _patch_wait_for_health(monkeypatch, True)

    result = runner.invoke(
        app,
        ["gateway", "start", "--listen", "127.0.0.2", "--port", "18888", "--json"],
    )

    assert result.exit_code == 0, result.stdout
    argv, _ = calls[0]
    assert argv[:3] == [sys.executable, "gateway", "run"]
    assert "-m" not in argv
    assert "opensquilla.cli.main" not in argv
    assert argv[argv.index("--listen") + 1] == "127.0.0.2"


def test_gateway_start_uses_explicit_config_path(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "home"))
    default_config = tmp_path / "default.toml"
    custom_config = tmp_path / "custom.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(default_config))
    calls = []

    def fake_popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(pid=4245)

    monkeypatch.setattr(gateway_lifecycle.subprocess, "Popen", fake_popen)
    _patch_health(monkeypatch, False)
    _patch_wait_for_health(monkeypatch, True)

    result = runner.invoke(
        app,
        ["gateway", "start", "--config", str(custom_config), "--json"],
    )

    assert result.exit_code == 0, result.stdout
    argv, kwargs = calls[0]
    assert argv[argv.index("--config") + 1] == str(custom_config)
    assert kwargs["env"]["OPENSQUILLA_GATEWAY_CONFIG_PATH"] == str(custom_config)
    record = json.loads(gateway_lifecycle.gateway_pidfile_path().read_text(encoding="utf-8"))
    assert record["configPath"] == str(custom_config)


def test_gateway_start_uses_config_host_port_when_flags_are_omitted(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "home"))
    custom_config = tmp_path / "custom.toml"
    custom_config.write_text('host = "127.0.0.2"\nport = 19999\n', encoding="utf-8")
    calls = []

    def fake_popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(pid=4246)

    monkeypatch.setattr(gateway_lifecycle.subprocess, "Popen", fake_popen)
    _patch_health(monkeypatch, False)
    _patch_wait_for_health(monkeypatch, True)

    result = runner.invoke(
        app,
        ["gateway", "start", "--config", str(custom_config), "--json"],
    )

    assert result.exit_code == 0, result.stdout
    argv, _kwargs = calls[0]
    assert argv[argv.index("--listen") + 1] == "127.0.0.2"
    assert argv[argv.index("--port") + 1] == "19999"
    payload = _payload(result)
    assert payload["url"] == "http://127.0.0.2:19999"


def test_gateway_status_uses_config_host_port_when_flags_are_omitted(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "home"))
    custom_config = tmp_path / "custom.toml"
    custom_config.write_text('host = "127.0.0.2"\nport = 19999\n', encoding="utf-8")
    probes = []

    def fake_probe(self):
        probes.append((self.host, self.port))
        return False

    monkeypatch.setattr(Manager, "_probe_health", fake_probe)

    result = runner.invoke(
        app,
        ["gateway", "status", "--config", str(custom_config), "--json"],
    )

    assert result.exit_code == 0, result.stdout
    assert probes == [("127.0.0.2", 19999)]
    payload = _payload(result)
    assert payload["url"] == "http://127.0.0.2:19999"


def test_gateway_run_uses_config_host_port_when_flags_are_omitted(
    tmp_path, monkeypatch
) -> None:
    custom_config = tmp_path / "custom.toml"
    custom_config.write_text('host = "127.0.0.2"\nport = 19999\n', encoding="utf-8")
    captured = {}

    class FakeServer:
        def __init__(self, task):
            self._task = task

        async def close(self, _reason):
            return None

    async def fake_start_gateway_server(*, config, subscription_manager, run):
        captured["config"] = config

        async def done():
            return None

        import asyncio

        return FakeServer(asyncio.create_task(done()))

    monkeypatch.setattr(gateway_cmd, "_gateway_bind_available", lambda *_args: True)
    monkeypatch.setattr(gateway_cmd, "start_gateway_server", fake_start_gateway_server)

    gateway_cmd.run_gateway(
        port=None,
        bind=None,
        listen="",
        debug=False,
        config_path=str(custom_config),
    )

    assert captured["config"].host == "127.0.0.2"
    assert captured["config"].port == 19999


def test_gateway_run_records_cli_flags_as_runtime_overrides(
    tmp_path, monkeypatch
) -> None:
    """Boot-time --listen/--port/--debug are runtime state, not operator
    config edits: run_gateway must record runtime provenance for the fields
    it mutates so the sparse persister can keep them out of config.toml."""
    custom_config = tmp_path / "custom.toml"
    custom_config.write_text('host = "127.0.0.1"\nport = 18791\n', encoding="utf-8")
    captured = {}

    class FakeServer:
        def __init__(self, task):
            self._task = task

        async def close(self, _reason):
            return None

    async def fake_start_gateway_server(*, config, subscription_manager, run):
        captured["config"] = config

        async def done():
            return None

        return FakeServer(asyncio.ensure_future(done()))

    monkeypatch.setattr(gateway_cmd, "_gateway_bind_available", lambda *_args: True)
    monkeypatch.setattr(gateway_cmd, "start_gateway_server", fake_start_gateway_server)

    gateway_cmd.run_gateway(
        port=18888,
        bind=None,
        listen="0.0.0.0",
        debug=True,
        config_path=str(custom_config),
    )

    overrides = captured["config"].runtime_field_overrides()
    assert overrides["host"] == ("127.0.0.1", "0.0.0.0")
    assert overrides["port"] == (18791, 18888)
    assert overrides["debug"] == (False, True)


def test_gateway_run_flags_do_not_leak_into_config_via_unrelated_persist(
    tmp_path, monkeypatch
) -> None:
    """F4 regression: `gateway run --listen 0.0.0.0 --debug` for a one-off
    session followed by an onboarding-surface save of an unrelated section
    must not bake host/debug into config.toml permanently."""
    from opensquilla.onboarding.config_store import load_config, persist_config
    from opensquilla.onboarding.mutations import upsert_search_provider

    custom_config = tmp_path / "custom.toml"
    custom_config.write_text('host = "127.0.0.1"\nport = 18791\n', encoding="utf-8")
    captured = {}

    class FakeServer:
        def __init__(self, task):
            self._task = task

        async def close(self, _reason):
            return None

    async def fake_start_gateway_server(*, config, subscription_manager, run):
        captured["config"] = config

        async def done():
            return None

        return FakeServer(asyncio.ensure_future(done()))

    monkeypatch.setattr(gateway_cmd, "_gateway_bind_available", lambda *_args: True)
    monkeypatch.setattr(gateway_cmd, "start_gateway_server", fake_start_gateway_server)

    gateway_cmd.run_gateway(
        port=None,
        bind=None,
        listen="0.0.0.0",
        debug=True,
        config_path=str(custom_config),
    )

    boot_config = captured["config"]
    assert boot_config.host == "0.0.0.0"
    assert boot_config.debug is True

    # Web-UI save of an unrelated onboarding section: the mutation clone
    # inherits the boot config's provenance and is what the RPC layer
    # persists (rpc_onboarding._persist -> persist_config).
    res = upsert_search_provider(
        boot_config, provider_id="tavily", api_key="tvly-synthetic-run"
    )
    persist_config(res.config, path=custom_config)

    data = tomllib.loads(custom_config.read_text())
    assert data["search_api_key"] == "tvly-synthetic-run"
    assert data["host"] == "127.0.0.1"  # transient --listen never lands
    assert data.get("debug", False) is False  # transient --debug never lands
    assert data.get("port", 18791) == 18791

    # The file is still what the operator wrote plus the search save: a
    # fresh load must not come up publicly bound or in debug mode.
    reloaded = load_config(custom_config)
    assert reloaded.host == "127.0.0.1"
    assert reloaded.debug is False


def test_gateway_run_keeps_missing_explicit_config_path_for_setup(
    tmp_path,
    monkeypatch,
) -> None:
    custom_config = tmp_path / "first-run.toml"
    captured = {}

    class FakeServer:
        def __init__(self, task):
            self._task = task

        async def close(self, _reason):
            return None

    async def fake_start_gateway_server(*, config, subscription_manager, run):
        captured["config"] = config

        async def done():
            return None

        import asyncio

        return FakeServer(asyncio.create_task(done()))

    monkeypatch.setattr(gateway_cmd, "_gateway_bind_available", lambda *_args: True)
    monkeypatch.setattr(gateway_cmd, "start_gateway_server", fake_start_gateway_server)

    gateway_cmd.run_gateway(
        port=19876,
        bind=None,
        listen="",
        debug=False,
        config_path=str(custom_config),
    )

    assert captured["config"].config_path == str(custom_config)
    assert not custom_config.exists()


def test_gateway_run_preflights_occupied_port_before_building_services(
    tmp_path,
    monkeypatch,
) -> None:
    custom_config = tmp_path / "custom.toml"
    custom_config.write_text('host = "127.0.0.2"\nport = 19999\n', encoding="utf-8")

    async def fail_start_gateway_server(**_kwargs):
        raise AssertionError("gateway services should not build when bind preflight fails")

    monkeypatch.setattr(gateway_cmd, "start_gateway_server", fail_start_gateway_server)
    monkeypatch.setattr(gateway_cmd, "_gateway_bind_available", lambda host, port: False)

    result = runner.invoke(
        app,
        ["gateway", "run", "--config", str(custom_config)],
    )

    assert result.exit_code == 1
    assert "127.0.0.2:19999 is already in use" in result.stdout


def test_gateway_start_waits_for_readiness_after_liveness(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "home"))
    calls = []
    health_checks = 0
    ready_checks = []

    def fake_popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(pid=4244)

    def fake_health(self):
        nonlocal health_checks
        health_checks += 1
        return health_checks > 1

    def fake_ready(self):
        ready_checks.append(True)
        return len(ready_checks) > 1

    monkeypatch.setattr(gateway_lifecycle.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(Manager, "_probe_health", fake_health)
    monkeypatch.setattr(Manager, "_probe_ready", fake_ready, raising=False)
    monkeypatch.setattr(gateway_lifecycle.time, "sleep", lambda _seconds: None)

    result = runner.invoke(app, ["gateway", "start", "--json"])

    assert result.exit_code == 0, result.stdout
    assert _payload(result)["state"] == "running"
    assert calls
    assert len(ready_checks) == 2


def test_gateway_health_probe_uses_loopback_for_wildcard_bind(monkeypatch) -> None:
    urls = []

    def fake_urlopen(request, timeout):
        urls.append(request.full_url)
        assert timeout == 0.5
        return _FakeHealthResponse()

    monkeypatch.setattr(gateway_lifecycle, "urlopen", fake_urlopen)

    manager = Manager(host="0.0.0.0", port=18888)

    assert manager._probe_health() is True
    assert urls == ["http://127.0.0.1:18888/health"]


def test_gateway_start_with_wildcard_listen_keeps_bind_and_reports_probe_host(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "home"))
    calls = []

    def fake_popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(pid=4243)

    monkeypatch.setattr(gateway_lifecycle.subprocess, "Popen", fake_popen)
    _patch_health(monkeypatch, False)
    _patch_wait_for_health(monkeypatch, True)

    result = runner.invoke(
        app,
        ["gateway", "start", "--listen", "0.0.0.0", "--port", "18889", "--json"],
    )

    assert result.exit_code == 0, result.stdout
    payload = _payload(result)
    assert payload["host"] == "0.0.0.0"
    assert payload["probeHost"] == "127.0.0.1"
    assert payload["url"] == "http://0.0.0.0:18889"
    assert payload["healthUrl"] == "http://127.0.0.1:18889/health"

    record = json.loads(gateway_lifecycle.gateway_pidfile_path().read_text(encoding="utf-8"))
    assert record["host"] == "0.0.0.0"
    assert record["probeHost"] == "127.0.0.1"
    assert record["url"] == "http://0.0.0.0:18889"
    assert record["healthUrl"] == "http://127.0.0.1:18889/health"

    argv, kwargs = calls[0]
    assert argv[argv.index("--listen") + 1] == "0.0.0.0"
    assert kwargs["shell"] is False


def test_gateway_start_does_not_spawn_duplicate_recorded_gateway(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "home"))
    _write_pidfile(_record(pid=321))
    _patch_pid_running(monkeypatch, True)
    _patch_health(monkeypatch, True)

    def fail_popen(*args, **kwargs):
        raise AssertionError("duplicate gateway should not be spawned")

    monkeypatch.setattr(gateway_lifecycle.subprocess, "Popen", fail_popen)

    result = runner.invoke(app, ["gateway", "start", "--json"])

    assert result.exit_code == 0
    payload = _payload(result)
    assert payload["state"] == "running"
    assert payload["pid"] == 321


def test_gateway_start_refuses_live_pidfile_for_different_target(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "home"))
    _write_pidfile(_record(pid=321, port=18791))
    _patch_pid_running(monkeypatch, True)
    _patch_health(monkeypatch, False)

    def fail_popen(*args, **kwargs):
        raise AssertionError("target mismatch must not spawn a second gateway")

    monkeypatch.setattr(gateway_lifecycle.subprocess, "Popen", fail_popen)

    result = runner.invoke(app, ["gateway", "start", "--port", "18792", "--json"])

    assert result.exit_code == 3
    payload = _payload(result)
    assert payload["state"] == "target_mismatch"
    assert payload["code"] == "MANAGED_GATEWAY_TARGET_MISMATCH"
    assert "http://127.0.0.1:18791" in payload["message"]
    assert "http://127.0.0.1:18792" in payload["message"]
    assert gateway_lifecycle.gateway_pidfile_path().exists()


def test_gateway_status_reports_recorded_config_mismatch(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "home"))
    record = _record(pid=321, port=18791)
    record["configPath"] = str(tmp_path / "first.toml")
    _write_pidfile(record)
    _patch_pid_running(monkeypatch, True)
    _patch_health(monkeypatch, False)

    result = runner.invoke(
        app,
        [
            "gateway",
            "status",
            "--config",
            str(tmp_path / "second.toml"),
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = _payload(result)
    assert payload["state"] == "target_mismatch"
    assert payload["details"]["recordedConfigPath"] == str(tmp_path / "first.toml")
    assert payload["details"]["requestedConfigPath"] == str(tmp_path / "second.toml")


def test_gateway_stop_clears_stale_pidfile(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "home"))
    _write_pidfile(_record(pid=9999))
    _patch_pid_running(monkeypatch, False)
    _patch_health(monkeypatch, False)

    result = runner.invoke(app, ["gateway", "stop", "--json"])

    assert result.exit_code == 0
    assert _payload(result)["state"] == "cleared_stale"
    assert not gateway_lifecycle.gateway_pidfile_path().exists()


def test_gateway_stop_refuses_unmanaged_healthy_gateway(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "home"))
    _patch_health(monkeypatch, True)

    result = runner.invoke(app, ["gateway", "stop", "--json"])

    assert result.exit_code == 3
    payload = _payload(result)
    assert payload["code"] == "UNMANAGED_GATEWAY_RUNNING"
    assert payload["state"] == "unmanaged"


def test_gateway_stop_refuses_live_pidfile_for_different_target(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "home"))
    _write_pidfile(_record(pid=321, port=18791))
    _patch_pid_running(monkeypatch, True)
    _patch_health(monkeypatch, False)

    def fail_terminate(self, pid):
        raise AssertionError("target mismatch must not terminate another gateway")

    monkeypatch.setattr(Manager, "_terminate_pid", fail_terminate)

    result = runner.invoke(app, ["gateway", "stop", "--port", "18792", "--json"])

    assert result.exit_code == 3
    payload = _payload(result)
    assert payload["state"] == "target_mismatch"
    assert payload["code"] == "MANAGED_GATEWAY_TARGET_MISMATCH"
    assert gateway_lifecycle.gateway_pidfile_path().exists()


def test_gateway_restart_refuses_live_pidfile_for_different_target(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "home"))
    _write_pidfile(_record(pid=321, port=18791))
    _patch_pid_running(monkeypatch, True)
    _patch_health(monkeypatch, False)

    def fail_popen(*args, **kwargs):
        raise AssertionError("target mismatch must not restart over another gateway")

    monkeypatch.setattr(gateway_lifecycle.subprocess, "Popen", fail_popen)

    result = runner.invoke(app, ["gateway", "restart", "--port", "18792", "--json"])

    assert result.exit_code == 3
    payload = _payload(result)
    assert payload["state"] == "target_mismatch"
    assert payload["code"] == "MANAGED_GATEWAY_TARGET_MISMATCH"
    assert gateway_lifecycle.gateway_pidfile_path().exists()


def test_gateway_restart_stops_before_starting(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "home"))
    _write_pidfile(_record(pid=777))
    events = []

    def fake_popen(argv, **kwargs):
        events.append("start")
        return SimpleNamespace(pid=888)

    def fake_terminate(self, pid):
        events.append("stop")
        return True

    _patch_pid_running(monkeypatch, True)
    _patch_health(monkeypatch, False)
    monkeypatch.setattr(Manager, "_terminate_pid", fake_terminate)
    _patch_wait_for_health(monkeypatch, True)
    monkeypatch.setattr(gateway_lifecycle.subprocess, "Popen", fake_popen)

    result = runner.invoke(app, ["gateway", "restart", "--json"])

    assert result.exit_code == 0, result.stdout
    assert events == ["stop", "start"]
    assert _payload(result)["state"] == "running"


# ---------------------------------------------------------------------------
# Graceful shutdown signal handling (gateway run)
#
# Regression coverage for the SIGTERM drain bug: _run() must route SIGINT and
# SIGTERM through GatewayServer.close() (the only path that drains in-flight
# agent turns + background completions), not let uvicorn's default handler exit
# without the drain.
# ---------------------------------------------------------------------------


class _ShutdownProbeServer:
    """Fake GatewayServer whose serve task only ends when close() is called.

    ``fire`` is the shutdown reason to simulate once the loop is running; ``via``
    selects how it is delivered — "signal" through the captured signal handler,
    or "http" through ``app.state.request_shutdown`` (the HTTP endpoint path).
    """

    def __init__(self, *, fire: str | None = None, via: str = "signal") -> None:
        self.closed: list[str] = []
        self._fire = fire
        self._via = via
        self._on_signal = None
        self.app = SimpleNamespace(state=SimpleNamespace())
        self._task: asyncio.Task | None = None

    def spawn(self) -> None:
        # Must run inside the event loop, so the fake start coroutine calls it.
        self._task = asyncio.ensure_future(self._serve())

    async def _serve(self) -> None:
        if self._fire is not None:
            await asyncio.sleep(0.01)
            if self._via == "http":
                self.app.state.request_shutdown(self._fire)
            else:
                assert self._on_signal is not None, "shutdown handler not captured"
                self._on_signal(self._fire)
        await asyncio.Event().wait()

    async def close(self, reason: str) -> None:
        self.closed.append(reason)
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass


def _install_fake_start(server, holder, monkeypatch) -> None:
    async def fake_start(*, config, subscription_manager, run):
        server.spawn()
        holder["server"] = server
        return server

    monkeypatch.setattr(gateway_cmd, "start_gateway_server", fake_start)


def test_gateway_run_drains_via_close_on_shutdown_signal(tmp_path, monkeypatch) -> None:
    """A delivered SIGTERM must trigger server.close() (the graceful drain)."""
    config = tmp_path / "gw.toml"
    config.write_text('host = "127.0.0.1"\nport = 18791\n', encoding="utf-8")

    holder: dict = {}
    server = _ShutdownProbeServer(fire="sigterm", via="signal")
    # Capture the handler _run installs, and feed it back into the fake server so
    # it can simulate signal delivery deterministically (no real os.kill).
    real_install = gateway_cmd._install_shutdown_handlers

    def capturing_install(loop, on_signal):
        server._on_signal = on_signal
        return real_install(loop, on_signal)

    monkeypatch.setattr(gateway_cmd, "_install_shutdown_handlers", capturing_install)
    _install_fake_start(server, holder, monkeypatch)
    monkeypatch.setattr(gateway_cmd, "_gateway_bind_available", lambda *_args: True)

    gateway_cmd.run_gateway(
        port=None, bind=None, listen="", debug=False, config_path=str(config)
    )

    assert holder["server"].closed == ["sigterm"]


def test_gateway_run_drains_when_server_task_exits_on_its_own(
    tmp_path, monkeypatch
) -> None:
    """If the serve task ends without a signal, close() still runs the drain."""
    config = tmp_path / "gw.toml"
    config.write_text('host = "127.0.0.1"\nport = 18791\n', encoding="utf-8")

    class _SelfExitingServer:
        def __init__(self) -> None:
            self.closed: list[str] = []
            self._task: asyncio.Task | None = None

        def spawn(self) -> None:
            async def done() -> None:
                return None

            self._task = asyncio.ensure_future(done())

        async def close(self, reason: str) -> None:
            self.closed.append(reason)

    holder: dict = {}
    _install_fake_start(_SelfExitingServer(), holder, monkeypatch)
    monkeypatch.setattr(gateway_cmd, "_gateway_bind_available", lambda *_args: True)

    gateway_cmd.run_gateway(
        port=None, bind=None, listen="", debug=False, config_path=str(config)
    )

    assert holder["server"].closed == ["shutdown"]


@pytest.mark.skipif(
    os.name == "nt", reason="loop.add_signal_handler is unsupported on Windows"
)
def test_install_shutdown_handlers_traps_real_sigterm() -> None:
    """The installed asyncio handler must trap a real SIGTERM (not let it kill)."""

    async def main() -> list[str]:
        loop = asyncio.get_running_loop()
        got: list[str] = []
        installed = gateway_cmd._install_shutdown_handlers(loop, got.append)
        try:
            # Only deliver the signal once we've confirmed our handler owns it,
            # so a failed install can never fall through to the default (kill).
            assert signal.SIGTERM in installed
            os.kill(os.getpid(), signal.SIGTERM)
            for _ in range(100):
                if got:
                    break
                await asyncio.sleep(0.01)
        finally:
            gateway_cmd._remove_shutdown_handlers(loop, installed)
        return got

    assert asyncio.run(main()) == ["sigterm"]


def test_gateway_shutdown_deadline_exceeds_graceful_budget(monkeypatch) -> None:
    """The kill deadline must always exceed the (two-phase) graceful drain."""
    from opensquilla.gateway import boot

    monkeypatch.delenv(boot.GATEWAY_GRACEFUL_TIMEOUT_ENV, raising=False)
    assert boot.gateway_shutdown_deadline() > boot.gateway_graceful_timeout() * 2

    monkeypatch.setenv(boot.GATEWAY_GRACEFUL_TIMEOUT_ENV, "5")
    assert boot.gateway_graceful_timeout() == 5.0
    assert boot.gateway_shutdown_deadline() > 5.0 * 2

    # Bounded: absurd values clamp, junk falls back to the default.
    monkeypatch.setenv(boot.GATEWAY_GRACEFUL_TIMEOUT_ENV, "100000")
    assert boot.gateway_graceful_timeout() == 120.0
    monkeypatch.setenv(boot.GATEWAY_GRACEFUL_TIMEOUT_ENV, "not-a-number")
    assert boot.gateway_graceful_timeout() == 30.0


def test_gateway_run_drains_via_http_shutdown_trigger(tmp_path, monkeypatch) -> None:
    """_run must expose app.state.request_shutdown; calling it drains via close()."""
    config = tmp_path / "gw.toml"
    config.write_text('host = "127.0.0.1"\nport = 18791\n', encoding="utf-8")

    holder: dict = {}
    # via="http" makes the fake server fire through app.state.request_shutdown,
    # which _run wires to the same graceful-drain trigger as the signal handlers.
    server = _ShutdownProbeServer(fire="api_shutdown", via="http")
    _install_fake_start(server, holder, monkeypatch)
    monkeypatch.setattr(gateway_cmd, "_gateway_bind_available", lambda *_args: True)

    gateway_cmd.run_gateway(
        port=None, bind=None, listen="", debug=False, config_path=str(config)
    )

    assert holder["server"].closed == ["api_shutdown"]


# ---------------------------------------------------------------------------
# Lifecycle stop: graceful HTTP shutdown (Windows) + POSIX SIGTERM fallback
# ---------------------------------------------------------------------------


class _FakeShutdownResponse:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        return None


def test_request_graceful_shutdown_posts_with_token(monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_TOKEN", "secret-tok")
    captured: dict = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["method"] = request.method
        captured["auth"] = request.headers.get("Authorization")
        captured["timeout"] = timeout
        return _FakeShutdownResponse(202)

    monkeypatch.setattr(gateway_lifecycle, "urlopen", fake_urlopen)
    mgr = Manager(host="127.0.0.1", port=18791)

    assert mgr._request_graceful_shutdown() is True
    assert captured["url"] == "http://127.0.0.1:18791/api/system/shutdown"
    assert captured["method"] == "POST"
    assert captured["auth"] == "Bearer secret-tok"


def test_request_graceful_shutdown_returns_false_when_unreachable(monkeypatch) -> None:
    def fake_urlopen(request, timeout):
        raise URLError("connection refused")

    monkeypatch.setattr(gateway_lifecycle, "urlopen", fake_urlopen)
    monkeypatch.delenv("OPENSQUILLA_GATEWAY_TOKEN", raising=False)
    mgr = Manager(host="127.0.0.1", port=18791, config_path=None)

    assert mgr._request_graceful_shutdown() is False


def test_terminate_pid_windows_prefers_graceful_endpoint(monkeypatch) -> None:
    monkeypatch.setattr(gateway_lifecycle, "_running_on_windows", lambda: True)
    monkeypatch.setattr(Manager, "_pid_running", lambda self, pid: True)
    calls: list[str] = []
    monkeypatch.setattr(
        Manager,
        "_request_graceful_shutdown",
        lambda self: calls.append("graceful") or True,
    )
    monkeypatch.setattr(Manager, "_wait_for_exit", lambda self, pid, timeout: True)
    killed: list = []
    monkeypatch.setattr(gateway_lifecycle.os, "kill", lambda *a: killed.append(a))

    mgr = Manager(host="127.0.0.1", port=18791, shutdown_timeout=1.0)
    assert mgr._terminate_pid(4321) is True
    assert calls == ["graceful"]
    assert killed == []  # graceful exit means no hard kill


def test_terminate_pid_windows_falls_back_to_terminate(monkeypatch) -> None:
    monkeypatch.setattr(gateway_lifecycle, "_running_on_windows", lambda: True)
    monkeypatch.setattr(Manager, "_pid_running", lambda self, pid: True)
    monkeypatch.setattr(Manager, "_request_graceful_shutdown", lambda self: False)
    monkeypatch.setattr(Manager, "_wait_for_exit", lambda self, pid, timeout: True)
    killed: list = []
    monkeypatch.setattr(gateway_lifecycle.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    mgr = Manager(host="127.0.0.1", port=18791, shutdown_timeout=1.0)
    assert mgr._terminate_pid(4321) is True
    # Graceful rejected -> hard terminate via TerminateProcess (os.kill SIGTERM).
    assert killed == [(4321, signal.SIGTERM)]


@pytest.mark.skipif(os.name == "nt", reason="SIGKILL is POSIX-only")
def test_terminate_pid_posix_uses_sigterm_then_sigkill(monkeypatch) -> None:
    monkeypatch.setattr(gateway_lifecycle, "_running_on_windows", lambda: False)
    monkeypatch.setattr(Manager, "_pid_running", lambda self, pid: True)
    sent: list = []
    monkeypatch.setattr(gateway_lifecycle.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    # First wait (after SIGTERM) times out, forcing the SIGKILL hard-kill path.
    monkeypatch.setattr(Manager, "_wait_for_exit", lambda self, pid, timeout: False)
    # Never reach the HTTP path on POSIX.
    monkeypatch.setattr(
        Manager,
        "_request_graceful_shutdown",
        lambda self: (_ for _ in ()).throw(AssertionError("HTTP path used on POSIX")),
    )

    mgr = Manager(host="127.0.0.1", port=18791, shutdown_timeout=0.0)
    mgr._terminate_pid(4321)
    assert (4321, signal.SIGTERM) in sent
    assert any(sig == getattr(signal, "SIGKILL", None) for _pid, sig in sent)


def test_hard_kill_backstop_does_not_reuse_full_shutdown_timeout(monkeypatch) -> None:
    """Windows accepted-but-slow path must not wait the full graceful budget twice."""
    monkeypatch.setattr(gateway_lifecycle, "_running_on_windows", lambda: True)
    monkeypatch.setattr(Manager, "_pid_running", lambda self, pid: True)
    monkeypatch.setattr(Manager, "_request_graceful_shutdown", lambda self: True)
    waits: list[float] = []

    def fake_wait(self, pid, timeout):
        waits.append(timeout)
        return len(waits) > 1  # graceful wait times out; hard-kill wait succeeds

    monkeypatch.setattr(Manager, "_wait_for_exit", fake_wait)
    monkeypatch.setattr(gateway_lifecycle.os, "kill", lambda *a: None)

    mgr = Manager(host="127.0.0.1", port=18791, shutdown_timeout=75.0)
    assert mgr._terminate_pid(4321) is True
    # First wait uses the graceful budget; the hard-kill backstop is short, not 75s again.
    assert waits == [75.0, gateway_lifecycle._HARD_KILL_BACKSTOP_S]
