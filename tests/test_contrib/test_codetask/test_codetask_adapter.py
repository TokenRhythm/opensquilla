"""Unit tests for opensquilla.contrib.codetask.adapter (subprocess mocked)."""

import subprocess as sp
import sys

import pytest

from opensquilla.contrib.codetask import adapter
from opensquilla.contrib.codetask.adapter import LocalAdapter, _agent_command
from opensquilla.contrib.codetask.agent_config import AgentConfigBundle


class FakePopen:
    """Minimal Popen stand-in for the adapter's communicate()-based flow."""

    def __init__(self, cmd, stdout="", returncode=0, stderr="", timeout=False, **kwargs):
        self.args = cmd
        self.pid = 4242
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self._timeout = timeout
        self._calls = 0
        self.killed = False

    def communicate(self, timeout=None):
        self._calls += 1
        # Time out only on the first call (the bounded wait); the post-kill
        # drain call returns normally.
        if self._timeout and self._calls == 1:
            raise sp.TimeoutExpired(self.args, timeout or 1)
        return self._stdout, self._stderr

    def kill(self):
        self.killed = True


def _install_popen(monkeypatch, captured, **popen_kw):
    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["cwd"] = kwargs.get("cwd")
        captured["new_session"] = kwargs.get("start_new_session")
        captured["env"] = kwargs.get("env")
        captured["code"] = cmd[2]
        return FakePopen(cmd, **popen_kw)

    monkeypatch.setattr(adapter.subprocess, "Popen", fake_popen)


def _capture_agent_argv(monkeypatch, captured):
    def fake_agent_command(executable, argv, py_code):
        captured["argv"] = list(argv)
        return [sys.executable, "-c", py_code]

    monkeypatch.setattr(adapter, "_agent_command", fake_agent_command)


def test_sandbox_off_uses_full_host_access_without_workspace_containment(
    monkeypatch, tmp_path
):
    captured = {}
    _install_popen(monkeypatch, captured, stdout='{"status": "ok", "text": "done", "usage": {}}')
    _capture_agent_argv(monkeypatch, captured)
    _isolate_operator_config(monkeypatch, tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    out = LocalAdapter(model="m", timeout=10).run(
        "fix it", repo=repo, scratch_dir=tmp_path / "s", artifact_dir=tmp_path / "art"
    )
    assert out.success is True
    argv = captured["argv"]
    for flag in (
        "--workspace",
        "--no-workspace-strict",
        "--scratch-dir",
        "--stateless",
        "--permissions",
    ):
        assert flag in argv
    assert argv[argv.index("--permissions") + 1] == "full"
    assert "--workspace-strict" not in argv
    assert "--workspace-lockdown" not in argv
    assert captured["cwd"] == str(repo)


def test_legacy_trusted_mode_keeps_restricted_workspace_containment(monkeypatch, tmp_path):
    captured = {}
    _install_popen(monkeypatch, captured, stdout='{"status": "ok", "text": "done", "usage": {}}')
    _capture_agent_argv(monkeypatch, captured)
    repo = tmp_path / "repo"
    repo.mkdir()
    bundle = AgentConfigBundle(
        payload={
            "sandbox": {
                "sandbox": True,
                "security_grading": True,
                "run_mode": "trusted",
            }
        }
    )

    out = LocalAdapter(model="m", timeout=10, agent_config=bundle).run(
        "fix it", repo=repo, scratch_dir=tmp_path / "s", artifact_dir=tmp_path / "art"
    )

    assert out.success is True
    argv = captured["argv"]
    assert argv[argv.index("--permissions") + 1] == "restricted"
    assert "--workspace-strict" in argv
    assert "--workspace-lockdown" in argv
    assert "--no-workspace-strict" not in argv


def test_standard_sandbox_uses_restricted_permission_profile(monkeypatch, tmp_path):
    captured = {}
    _install_popen(monkeypatch, captured, stdout='{"status": "ok", "text": "done"}')
    _capture_agent_argv(monkeypatch, captured)
    repo = tmp_path / "repo"
    repo.mkdir()
    bundle = AgentConfigBundle(
        payload={
            "sandbox": {
                "sandbox": True,
                "security_grading": True,
                "run_mode": "standard",
            }
        }
    )

    out = LocalAdapter(agent_config=bundle).run(
        "fix it", repo=repo, scratch_dir=tmp_path / "s", artifact_dir=tmp_path / "art"
    )

    assert out.success is True
    argv = captured["argv"]
    assert argv[argv.index("--permissions") + 1] == "restricted"
    assert "--workspace-strict" in argv
    assert "--workspace-lockdown" in argv


@pytest.mark.skipif(sys.platform == "win32", reason="process-group signal differs on Windows")
def test_agent_process_starts_in_its_own_posix_session(monkeypatch, tmp_path):
    captured = {}
    _install_popen(monkeypatch, captured, stdout='{"status": "ok", "text": "done", "usage": {}}')
    repo = tmp_path / "repo"
    repo.mkdir()

    out = LocalAdapter(model="m", timeout=10).run(
        "fix it", repo=repo, scratch_dir=tmp_path / "s", artifact_dir=tmp_path / "art"
    )

    assert out.success is True
    # Descendant-killable process group (codex review #6).
    assert captured["new_session"] is True


def test_api_key_never_in_argv(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-secret-xyz")
    captured = {}
    _install_popen(monkeypatch, captured, stdout='{"status": "ok", "text": "done"}')
    repo = tmp_path / "repo"
    repo.mkdir()
    LocalAdapter().run("x", repo=repo, scratch_dir=tmp_path / "s", artifact_dir=tmp_path / "a")
    assert all("sk-or-secret-xyz" not in part for part in captured["cmd"])


def test_agent_command_uses_packaged_cli_directly_for_gateway_executable():
    cmd = _agent_command(
        "/Applications/OpenSquilla.app/Contents/Resources/runtime/gateway/opensquilla-gateway/opensquilla-gateway",
        ["opensquilla", "agent", "--message", "hello"],
        "print('unused')",
    )

    assert cmd == [
        "/Applications/OpenSquilla.app/Contents/Resources/runtime/gateway/opensquilla-gateway/opensquilla-gateway",
        "agent",
        "--message",
        "hello",
    ]
    assert "-c" not in cmd


def test_packaged_agent_run_uses_isolated_profile_env(monkeypatch, tmp_path):
    parent_home = tmp_path / "desktop-profile"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(parent_home))
    monkeypatch.setenv("OPENSQUILLA_PROFILE_KIND", "desktop-primary")
    monkeypatch.setenv("OPENSQUILLA_DESKTOP", "1")
    executable = (
        "/Applications/OpenSquilla.app/Contents/Resources/runtime/gateway/"
        "opensquilla-gateway/opensquilla-gateway"
    )
    monkeypatch.setattr(adapter, "agent_python", lambda: executable)
    captured = {}
    _install_popen(monkeypatch, captured, stdout='{"status": "ok", "text": "done"}')
    repo = tmp_path / "repo"
    repo.mkdir()

    out = LocalAdapter().run(
        "x", repo=repo, scratch_dir=tmp_path / "s", artifact_dir=tmp_path / "a"
    )

    assert out.success is True
    assert captured["cmd"][:2] == [executable, "agent"]
    assert captured["env"]["OPENSQUILLA_STATE_DIR"] == str(
        (tmp_path / "s").resolve() / "profile"
    )
    assert "OPENSQUILLA_PROFILE_KIND" not in captured["env"]
    assert "OPENSQUILLA_DESKTOP" not in captured["env"]


def test_timeout_kills_group_and_reports_timeout(monkeypatch, tmp_path):
    captured = {}
    _install_popen(monkeypatch, captured, timeout=True)
    killed = {"group": False}
    monkeypatch.setattr(
        adapter, "_kill_process_group", lambda proc: killed.__setitem__("group", True)
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    out = LocalAdapter(timeout=1).run(
        "x", repo=repo, scratch_dir=tmp_path / "s", artifact_dir=tmp_path / "a"
    )
    assert out.timeout is True
    assert out.finish_reason == "timeout"
    assert killed["group"] is True


@pytest.mark.skipif(sys.platform == "win32", reason="process-group signal differs on Windows")
def test_run_streams_logs_and_touches_observability_files(monkeypatch, tmp_path):
    script = (
        "import json, sys, time\n"
        "print('first line')\n"
        "sys.stdout.flush()\n"
        "time.sleep(0.1)\n"
        "print(json.dumps({'status': 'ok', 'text': 'done', 'usage': {'total_tokens': 7}}))\n"
        "sys.stdout.flush()\n"
    )
    monkeypatch.setattr(adapter, "agent_python", lambda: sys.executable)
    monkeypatch.setattr(
        adapter, "_agent_command", lambda executable, argv, py_code: [sys.executable, "-c", script]
    )
    monkeypatch.setattr(adapter, "POLL_INTERVAL_SECONDS", 0.05)
    statuses = []
    repo = tmp_path / "repo"
    repo.mkdir()

    out = LocalAdapter(timeout=5).run(
        "secret prompt",
        repo=repo,
        scratch_dir=tmp_path / "s",
        artifact_dir=tmp_path / "a",
        status_callback=statuses.append,
        quiet_timeout=2,
    )

    assert out.success is True
    assert out.usage["total_tokens"] == 7
    assert "first line" in (tmp_path / "a" / "agent_stdout.log").read_text("utf-8")
    assert (tmp_path / "a" / "agent_stderr.log").is_file()
    assert (tmp_path / "a" / "transcript.jsonl").is_file()
    assert (tmp_path / "a" / "usage.json").is_file()
    assert statuses
    assert statuses[0]["log_paths"]["stdout"].endswith("agent_stdout.log")
    assert "secret prompt" not in statuses[0]["current_command"]


@pytest.mark.skipif(sys.platform == "win32", reason="process-group signal differs on Windows")
def test_run_marks_silent_agent_as_stalled(monkeypatch, tmp_path):
    script = "import time\ntime.sleep(30)\n"
    monkeypatch.setattr(adapter, "agent_python", lambda: sys.executable)
    monkeypatch.setattr(
        adapter, "_agent_command", lambda executable, argv, py_code: [sys.executable, "-c", script]
    )
    monkeypatch.setattr(adapter, "POLL_INTERVAL_SECONDS", 0.05)
    repo = tmp_path / "repo"
    repo.mkdir()

    out = LocalAdapter(timeout=10).run(
        "x",
        repo=repo,
        scratch_dir=tmp_path / "s",
        artifact_dir=tmp_path / "a",
        quiet_timeout=1,
    )

    assert out.success is False
    assert out.finish_reason == "stalled"
    assert out.error and "no stdout/stderr/transcript/usage updates" in out.error


def test_run_clears_stale_scratch_manifest(monkeypatch, tmp_path):
    # A leftover verification.json from a prior run reusing this run_id must
    # be wiped before the agent runs, so the runner cannot read a stale
    # manifest (codex review).
    captured = {}
    _install_popen(monkeypatch, captured, stdout='{"status": "ok", "text": "done"}')
    repo = tmp_path / "repo"
    repo.mkdir()
    scratch = tmp_path / "s"
    scratch.mkdir()
    stale = scratch / "verification.json"
    stale.write_text('{"testable": true, "stale": true}')

    LocalAdapter().run("x", repo=repo, scratch_dir=scratch, artifact_dir=tmp_path / "a")

    # The stale manifest is gone (scratch was recreated clean).
    assert not stale.exists()
    assert scratch.is_dir()


def _isolate_operator_config(monkeypatch, tmp_path):
    """Point operator-config discovery at a missing file so tests exercise the
    pure template regardless of the developer's real config/env."""
    monkeypatch.setenv(
        "OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "no-operator-config.toml")
    )
    monkeypatch.delenv("OPENSQUILLA_CODETASK_AGENT_CONFIG", raising=False)


def test_run_points_agent_at_codetask_config(monkeypatch, tmp_path):
    # The agent subprocess must load code-task's own config (deny list etc.)
    # via OPENSQUILLA_GATEWAY_CONFIG_PATH. Runtime/provider env is inherited,
    # but all persistent paths belong to the per-attempt profile.
    from opensquilla.contrib.codetask.config import agent_config_path

    _isolate_operator_config(monkeypatch, tmp_path)
    profile_scoped = {
        "OPENSQUILLA_DESKTOP": "1",
        "OPENSQUILLA_DESKTOP_PROFILE_KIND": "desktop-primary",
        "OPENSQUILLA_PROFILE_KIND": "desktop-primary",
        "OPENSQUILLA_HOME": str(tmp_path / "profiles"),
        "OPENSQUILLA_PROFILE": "desktop",
        "OPENSQUILLA_STATE_DIR": str(tmp_path / "desktop-home"),
        "OPENSQUILLA_GATEWAY_STATE_DIR": str(tmp_path / "gateway-state"),
        "OPENSQUILLA_GATEWAY_WORKSPACE_DIR": str(tmp_path / "gateway-workspace"),
        "OPENSQUILLA_WORKSPACE_DIR": str(tmp_path / "workspace"),
        "OPENSQUILLA_MEMORY_DIR": str(tmp_path / "memory"),
        "OPENSQUILLA_MEMORY_DB": str(tmp_path / "memory.db"),
        "OPENSQUILLA_SESSION_ARCHIVE_DIR": str(tmp_path / "sessions"),
        "OPENSQUILLA_LOG_DIR": str(tmp_path / "logs"),
        "OPENSQUILLA_TURN_CALL_LOG_DIR": str(tmp_path / "turn-logs"),
        "OPENSQUILLA_LLM_TRACE_PATH": str(tmp_path / "llm-trace.jsonl"),
        "OPENSQUILLA_RUNTIME_EVENTS_PATH": str(tmp_path / "runtime-events.jsonl"),
        "OPENSQUILLA_PATCH_EVIDENCE_LEDGER_PATH": str(tmp_path / "patch-ledger.jsonl"),
        "OPENSQUILLA_SCHEDULER_DB": str(tmp_path / "scheduler.db"),
        "OPENSQUILLA_META_RUNS_DB": str(tmp_path / "meta-runs.db"),
        "OPENSQUILLA_ROUTER_DECISIONS_DB": str(tmp_path / "router.db"),
    }
    for name, value in profile_scoped.items():
        monkeypatch.setenv(name, value)
    inherited = {
        "OPENROUTER_API_KEY": "synthetic-provider-key",
        "HTTPS_PROXY": "http://127.0.0.1:19090",
        "OPENSQUILLA_NODE_BIN_DIR": str(tmp_path / "node-bin"),
        "OPENSQUILLA_MIGRATIONS_DIR": str(tmp_path / "migrations"),
    }
    for name, value in inherited.items():
        monkeypatch.setenv(name, value)
    captured = {}
    _install_popen(monkeypatch, captured, stdout='{"status": "ok", "text": "done"}')
    repo = tmp_path / "repo"
    repo.mkdir()
    LocalAdapter().run(
        "x", repo=repo, scratch_dir=tmp_path / "s", artifact_dir=tmp_path / "a"
    )
    env = captured["env"]
    assert env is not None
    # The agent now loads a PER-RUN config (derived from code-task's base config)
    # so its tool-result store is isolated to this run instead of the shared
    # global media root -- avoids the quadratic global-store rescan / spin.
    per_run_cfg = tmp_path / "a" / "agent-config.toml"
    assert env["OPENSQUILLA_GATEWAY_CONFIG_PATH"] == str(per_run_cfg)
    assert env["OPENSQUILLA_STATE_DIR"] == str((tmp_path / "s").resolve() / "profile")
    for name in profile_scoped:
        if name != "OPENSQUILLA_STATE_DIR":
            assert name not in env
    for name, value in inherited.items():
        assert env[name] == value
    import tomllib

    cfg_text = per_run_cfg.read_text(encoding="utf-8")
    parsed = tomllib.loads(cfg_text)
    base = tomllib.loads(agent_config_path().read_text(encoding="utf-8"))
    # every base section survives the merge (deny list + policy preserved)...
    for section in base:
        assert section in parsed, section
    # ...and media_root is pinned under THIS run's scratch (absolute).
    assert parsed["attachments"]["media_root"] == str(
        (tmp_path / "s").resolve() / "media"
    )
    from opensquilla.gateway.config import GatewayConfig
    from opensquilla.paths import media_root_from_config

    conf = GatewayConfig.load(str(per_run_cfg))
    assert media_root_from_config(conf) == (tmp_path / "s").resolve() / "media"
    assert "PATH" in env  # inherits parent env (provider keys pass through)


def test_desktop_writer_can_launch_codetask_agent_with_isolated_profile(
    monkeypatch, tmp_path
):
    """Regression for #656: a Desktop-held lock must not reject code-task."""

    desktop_home = tmp_path / "desktop-profile"
    workspace = desktop_home / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "SOUL.md").write_text("synthetic desktop profile\n", encoding="utf-8")
    (desktop_home / "state").mkdir()
    desktop_config = desktop_home / "config.toml"
    desktop_config.write_text(
        'state_dir = "state"\nworkspace_dir = "workspace"\n', encoding="utf-8"
    )
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(desktop_home))
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(desktop_config))
    monkeypatch.setenv("OPENSQUILLA_PROFILE_KIND", "desktop-primary")
    monkeypatch.setenv("OPENSQUILLA_DESKTOP", "1")
    monkeypatch.setenv("OPENSQUILLA_TEST_PROFILE_LOCK_ROOT", "1")
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "user-state"))

    child_script = (
        "import json\n"
        "from opensquilla.paths import default_opensquilla_home\n"
        "from opensquilla.recovery import guarded_desktop_profile\n"
        "with guarded_desktop_profile():\n"
        '    print(json.dumps({"status": "ok", "text": "locked", '
        '"usage": {"profile_home": str(default_opensquilla_home())}}))\n'
    )
    monkeypatch.setattr(adapter, "agent_python", lambda: sys.executable)
    monkeypatch.setattr(
        adapter,
        "_agent_command",
        lambda executable, argv, py_code: [sys.executable, "-c", child_script],
    )

    repo = tmp_path / "repo"
    repo.mkdir()
    scratch = tmp_path / "scratch"
    artifact = tmp_path / "artifacts"

    from opensquilla.recovery import guarded_desktop_profile

    with guarded_desktop_profile():
        desktop_files_before = {
            path.relative_to(desktop_home) for path in desktop_home.rglob("*")
        }
        out = LocalAdapter(timeout=10).run(
            "x",
            repo=repo,
            scratch_dir=scratch,
            artifact_dir=artifact,
            quiet_timeout=5,
        )
        desktop_files_after = {
            path.relative_to(desktop_home) for path in desktop_home.rglob("*")
        }

    assert out.success is True
    assert out.usage["profile_home"] == str(scratch.resolve() / "profile")
    assert desktop_files_after == desktop_files_before


def test_per_run_config_merges_existing_attachments(monkeypatch, tmp_path):
    """If the base agent config ever gains an [attachments] table, the per-run
    config must OVERRIDE media_root (merge), not append a duplicate table that
    breaks tomllib parsing."""
    import tomllib

    from opensquilla.contrib.codetask import adapter as adapter_mod
    from opensquilla.contrib.codetask import agent_config as agent_config_mod

    _isolate_operator_config(monkeypatch, tmp_path)
    base = tmp_path / "base.toml"
    base.write_text(
        '[attachments]\nmedia_root = "/old/global"\npersist_transcripts = true\n'
        "[tools]\ndeny = [\"x\"]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(agent_config_mod, "agent_config_path", lambda: base)

    captured = {}
    _install_popen(monkeypatch, captured, stdout='{"status": "ok", "text": "done"}')
    repo = tmp_path / "repo"
    repo.mkdir()
    adapter_mod.LocalAdapter().run(
        "x", repo=repo, scratch_dir=tmp_path / "s", artifact_dir=tmp_path / "a"
    )
    cfg = tomllib.loads((tmp_path / "a" / "agent-config.toml").read_text("utf-8"))
    # overridden, not duplicated; sibling key + other sections preserved
    assert cfg["attachments"]["media_root"] == str((tmp_path / "s").resolve() / "media")
    assert cfg["attachments"]["persist_transcripts"] is True
    assert cfg["tools"]["deny"] == ["x"]


def test_per_run_config_inherits_operator_provider(monkeypatch, tmp_path):
    """The operator's provider stack replaces the template's; credentials are
    kept out of the written file and travel via the child env instead
    (issue #541)."""
    import tomllib

    operator_cfg = tmp_path / "operator.toml"
    operator_cfg.write_text(
        "[llm]\n"
        'provider = "deepseek"\n'
        'model = "deepseek-chat"\n'
        'api_key = "sk-user-typed-secret"\n'
        "[squilla_router]\n"
        "enabled = true\n"
        "[llm_profiles.moonshot]\n"
        'api_key = "sk-profile-secret"\n'
        "[llm_ensemble]\n"
        "enabled = true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(operator_cfg))
    monkeypatch.delenv("OPENSQUILLA_CODETASK_AGENT_CONFIG", raising=False)

    captured = {}
    _install_popen(monkeypatch, captured, stdout='{"status": "ok", "text": "done"}')
    repo = tmp_path / "repo"
    repo.mkdir()
    LocalAdapter().run(
        "x", repo=repo, scratch_dir=tmp_path / "s", artifact_dir=tmp_path / "a"
    )

    written = (tmp_path / "a" / "agent-config.toml").read_text(encoding="utf-8")
    parsed = tomllib.loads(written)
    # Operator provider stack carried in...
    assert parsed["llm"]["provider"] == "deepseek"
    assert parsed["llm"]["model"] == "deepseek-chat"
    assert parsed["squilla_router"]["enabled"] is True
    # ...primary credential stripped from the on-disk file (snapshotted per
    # attempt) and transported via the child env instead...
    assert "api_key" not in parsed["llm"]
    assert "sk-user-typed-secret" not in written
    assert captured["env"]["OPENSQUILLA_LLM_API_KEY"] == "sk-user-typed-secret"
    # ...profile keys stay in the 0600 file (no env transport channel exists)...
    assert parsed["llm_profiles"]["moonshot"]["api_key"] == "sk-profile-secret"
    # ...[llm_ensemble] deliberately not carried (env-key auto-opt-in would
    # re-pin the subagent to a provider the operator moved away from)...
    assert "llm_ensemble" not in parsed
    # ...and the template's run policy stays authoritative.
    for section in ("tools", "sandbox", "meta_skill", "memory"):
        assert section in parsed, section
    assert parsed["meta_skill"]["enabled"] is False


def test_per_run_config_without_operator_config_keeps_default_behavior(
    monkeypatch, tmp_path
):
    """Upgrade parity: with no operator config anywhere, the subagent resolves
    the same built-in default provider as before the inheritance change."""
    import tomllib

    _isolate_operator_config(monkeypatch, tmp_path)
    monkeypatch.delenv("OPENSQUILLA_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("OPENSQUILLA_LLM_API_KEY", raising=False)
    captured = {}
    _install_popen(monkeypatch, captured, stdout='{"status": "ok", "text": "done"}')
    repo = tmp_path / "repo"
    repo.mkdir()
    LocalAdapter().run(
        "x", repo=repo, scratch_dir=tmp_path / "s", artifact_dir=tmp_path / "a"
    )
    per_run_cfg = tmp_path / "a" / "agent-config.toml"
    parsed = tomllib.loads(per_run_cfg.read_text(encoding="utf-8"))
    # No pinned [llm]: provider resolution falls to defaults + env, exactly
    # like the operator's own gateway with no config file.
    assert "llm" not in parsed
    from opensquilla.gateway.config import GatewayConfig

    # Whatever a bare gateway would default to (don't hard-code the vendor —
    # the built-in default provider changes over time), the subagent must
    # resolve the SAME thing.
    bare_default = GatewayConfig.load(str(tmp_path / "no-such-config.toml")).llm.provider
    conf = GatewayConfig.load(str(per_run_cfg))
    assert conf.llm.provider == bare_default
    # No key to transport, so no injection either.
    assert "OPENSQUILLA_LLM_API_KEY" not in captured["env"]


def test_agent_config_override_env_is_fully_authoritative(monkeypatch, tmp_path):
    """OPENSQUILLA_CODETASK_AGENT_CONFIG (the documented #541 escape hatch)
    disables provider inheritance: the custom file is used as-is, apart from
    the per-run media_root injection."""
    import tomllib

    override_cfg = tmp_path / "override.toml"
    override_cfg.write_text(
        '[llm]\nprovider = "deepseek"\nmodel = "deepseek-chat"\napi_key = "sk-in-override"\n',
        encoding="utf-8",
    )
    operator_cfg = tmp_path / "operator.toml"
    operator_cfg.write_text('[llm]\nprovider = "moonshot"\nmodel = "kimi-k2.6"\n', "utf-8")
    monkeypatch.setenv("OPENSQUILLA_CODETASK_AGENT_CONFIG", str(override_cfg))
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(operator_cfg))
    monkeypatch.delenv("OPENSQUILLA_LLM_API_KEY", raising=False)

    captured = {}
    _install_popen(monkeypatch, captured, stdout='{"status": "ok", "text": "done"}')
    repo = tmp_path / "repo"
    repo.mkdir()
    LocalAdapter().run(
        "x", repo=repo, scratch_dir=tmp_path / "s", artifact_dir=tmp_path / "a"
    )
    parsed = tomllib.loads((tmp_path / "a" / "agent-config.toml").read_text("utf-8"))
    assert parsed["llm"]["provider"] == "deepseek"  # override wins, no merge
    assert parsed["llm"]["api_key"] == "sk-in-override"  # untouched (full authority)
    assert parsed["attachments"]["media_root"] == str((tmp_path / "s").resolve() / "media")
    assert "OPENSQUILLA_LLM_API_KEY" not in captured["env"]  # no inheritance, no injection


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_per_run_config_is_owner_only(monkeypatch, tmp_path):
    # The per-run file can carry [llm_profiles] credentials.
    import stat

    operator_cfg = tmp_path / "operator.toml"
    operator_cfg.write_text(
        '[llm]\nprovider = "deepseek"\nmodel = "deepseek-chat"\n', encoding="utf-8"
    )
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(operator_cfg))
    monkeypatch.delenv("OPENSQUILLA_CODETASK_AGENT_CONFIG", raising=False)
    captured = {}
    _install_popen(monkeypatch, captured, stdout='{"status": "ok", "text": "done"}')
    repo = tmp_path / "repo"
    repo.mkdir()
    LocalAdapter().run(
        "x", repo=repo, scratch_dir=tmp_path / "s", artifact_dir=tmp_path / "a"
    )
    mode = stat.S_IMODE((tmp_path / "a" / "agent-config.toml").stat().st_mode)
    assert mode == 0o600
