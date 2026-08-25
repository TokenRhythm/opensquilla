"""Build-mode verification: the fixed checklist decides the task state."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from opensquilla.contrib.codetask import build_verify
from opensquilla.contrib.codetask.types import TaskState


def _make_repo(tmp_path: Path, *, pkg: bool = True, lock: bool = True) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    if pkg:
        (tmp_path / "package.json").write_text("{}")
    if lock:
        (tmp_path / "package-lock.json").write_text("{}")
    return tmp_path


def _ok(out: str = "ok") -> build_verify._CheckRun:
    return build_verify._CheckRun(ran=True, exit_code=0, timed_out=False, output_tail=out)


def _fail(code: int = 1, out: str = "") -> build_verify._CheckRun:
    return build_verify._CheckRun(ran=True, exit_code=code, timed_out=False, output_tail=out)


def test_missing_lockfile_is_environment_blocked(tmp_path):
    repo = _make_repo(tmp_path, lock=False)
    out = build_verify.verify_build(repo)
    assert out.state is TaskState.ENVIRONMENT_BLOCKED
    assert "package-lock.json" in out.detail
    assert out.build.all_passed is False
    assert out.build.checks == []


def test_all_checks_pass_is_verified(tmp_path, monkeypatch):
    # Pin a deterministic host (linux) and provide its installer, exercising the
    # "all checks pass + installer present -> VERIFIED" path. Every platform now
    # requires its own installer; per-platform cases are covered below.
    monkeypatch.setattr(build_verify.sys, "platform", "linux")
    repo = _make_repo(tmp_path)
    (repo / "dist").mkdir()
    (repo / "dist" / "app-1.0.0.AppImage").write_text("y")
    monkeypatch.setattr(build_verify, "_run_build_check", lambda *a, **k: _ok())
    out = build_verify.verify_build(repo)
    assert out.state is TaskState.VERIFIED
    assert out.build.all_passed is True
    assert [c.name for c in out.build.checks] == [
        "npm_ci",
        "build",
        "package",
        "runtime_deps",
    ]
    assert all(c.ok and c.ran for c in out.build.checks)
    assert out.build.installer_path.endswith("app-1.0.0.AppImage")


def test_npm_ci_failure_is_environment_blocked_and_stops_early(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    monkeypatch.setattr(build_verify, "_run_build_check", lambda *a, **k: _fail(1, "npm err"))
    out = build_verify.verify_build(repo)
    assert out.state is TaskState.ENVIRONMENT_BLOCKED
    # stops at the first failing check
    assert [c.name for c in out.build.checks] == ["npm_ci"]
    assert out.build.checks[0].ok is False
    assert out.build.all_passed is False


def test_build_failure_is_failed(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    monkeypatch.setattr(build_verify, "_resolve_cli", lambda name: name)

    def fake_run(argv, **k):
        if argv[:2] == ["npm", "run"]:  # the build step
            return _fail(1, "tsc error")
        return _ok()

    monkeypatch.setattr(build_verify, "_run_build_check", fake_run)
    out = build_verify.verify_build(repo)
    assert out.state is TaskState.FAILED
    assert [c.name for c in out.build.checks] == ["npm_ci", "build"]
    assert out.build.checks[-1].ok is False
    assert "build" in out.detail


def test_package_failure_is_failed(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    monkeypatch.setattr(build_verify, "_resolve_cli", lambda name: name)

    def fake_run(argv, **k):
        if argv[0] == "npx":  # electron-builder
            return _fail(1, "packaging error")
        return _ok()

    monkeypatch.setattr(build_verify, "_run_build_check", fake_run)
    out = build_verify.verify_build(repo)
    assert out.state is TaskState.FAILED
    assert [c.name for c in out.build.checks] == ["npm_ci", "build", "package"]
    assert out.build.checks[-1].ok is False


def test_command_not_found_is_recorded(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)

    def fake_run(*a, **k):
        return build_verify._CheckRun(
            ran=False,
            exit_code=None,
            timed_out=False,
            output_tail="",
            error="command not found: npm",
        )

    monkeypatch.setattr(build_verify, "_run_build_check", fake_run)
    out = build_verify.verify_build(repo)
    assert out.state is TaskState.ENVIRONMENT_BLOCKED  # npm_ci is the first check
    assert out.build.checks[0].ran is False
    assert "not found" in out.build.checks[0].raw_tail


def test_timeout_is_typed_and_reported(tmp_path, monkeypatch):
    # A hung check must be distinguishable from a build that ran and failed.
    repo = _make_repo(tmp_path)

    def fake_run(argv, **k):
        if argv[:2] == ["npm", "run"]:
            return build_verify._CheckRun(
                ran=True, exit_code=None, timed_out=True, output_tail="TIMEOUT after 1s"
            )
        return _ok()

    monkeypatch.setattr(build_verify, "_resolve_cli", lambda name: name)
    monkeypatch.setattr(build_verify, "_run_build_check", fake_run)
    out = build_verify.verify_build(repo)
    assert out.state is TaskState.FAILED
    build_step = out.build.checks[-1]
    assert build_step.name == "build"
    assert build_step.timed_out is True
    assert build_step.ok is False
    assert "timed out" in out.detail


def test_resolve_cli_uses_desktop_node_bin_env(tmp_path, monkeypatch):
    node_bin = tmp_path / "node-bin"
    node_bin.mkdir()
    npm = node_bin / ("npm.cmd" if sys.platform == "win32" else "npm")
    npm.write_text("", encoding="utf-8")
    npm.chmod(0o755)
    monkeypatch.setenv("OPENSQUILLA_NODE_BIN_DIR", str(node_bin))
    monkeypatch.setattr(build_verify.shutil, "which", lambda *a, **k: None)

    assert build_verify._resolve_cli("npm") == str(npm)


def test_resolve_cli_safe_mode_prefers_runtime_pack(monkeypatch):
    monkeypatch.setattr(
        build_verify,
        "_runtime_pack_node_binary",
        lambda _name: "/managed/node/npm",
    )
    monkeypatch.setattr(build_verify, "_runtime_pack_precedes_host", lambda: True)
    monkeypatch.setattr(build_verify.shutil, "which", lambda _name: "/host/npm")

    assert build_verify._resolve_cli("npm") == "/managed/node/npm"


def test_resolve_cli_full_mode_prefers_host_with_managed_fallback(monkeypatch):
    monkeypatch.setattr(
        build_verify,
        "_runtime_pack_node_binary",
        lambda _name: "/managed/node/npm",
    )
    monkeypatch.setattr(build_verify, "_runtime_pack_precedes_host", lambda: False)
    monkeypatch.setattr(build_verify.shutil, "which", lambda _name: "/host/npm")

    assert build_verify._resolve_cli("npm") == "/host/npm"

    monkeypatch.setattr(build_verify.shutil, "which", lambda _name: None)
    assert build_verify._resolve_cli("npm") == "/managed/node/npm"


@pytest.mark.parametrize(
    ("mode", "expected"),
    (("safe", True), ("full", False), (None, False)),
)
def test_runtime_pack_precedence_normalizes_tool_run_mode(monkeypatch, mode, expected):
    from opensquilla.tools import run_mode as tool_run_mode

    monkeypatch.setattr(tool_run_mode, "current_run_mode", lambda: mode)

    assert build_verify._runtime_pack_precedes_host() is expected


def test_verify_build_prepends_desktop_node_bin_to_child_path(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path / "repo")
    node_bin = tmp_path / "node-bin"
    node_bin.mkdir()
    monkeypatch.setenv("OPENSQUILLA_NODE_BIN_DIR", str(node_bin))
    monkeypatch.setattr(build_verify, "_resolve_cli", lambda name: name)

    seen_paths: list[str] = []

    def fake_run(argv, *, cwd, env, timeout):
        seen_paths.append(env["PATH"])
        return _fail(1, "stop after first check")

    monkeypatch.setattr(build_verify, "_run_build_check", fake_run)
    build_verify.verify_build(repo)

    assert seen_paths
    assert seen_paths[0].split(build_verify.os.pathsep)[0] == str(node_bin)


def test_verify_build_env_is_non_interactive(tmp_path, monkeypatch):
    # Every verification child must run non-interactively: a prompting build
    # tool should see CI/npm auto-confirm rather than block on the caller's TTY.
    repo = _make_repo(tmp_path)
    monkeypatch.setattr(build_verify, "_resolve_cli", lambda name: name)

    seen_env: list[dict[str, str]] = []

    def fake_run(argv, *, cwd, env, timeout):
        seen_env.append(env)
        return _fail(1)

    monkeypatch.setattr(build_verify, "_run_build_check", fake_run)
    build_verify.verify_build(repo)

    assert seen_env
    env = seen_env[0]
    assert env["CI"] == "1"
    assert env["npm_config_yes"] == "true"
    assert env["CSC_IDENTITY_AUTO_DISCOVERY"] == "false"


def test_run_build_check_closes_stdin_and_is_bounded(tmp_path):
    # Real child: a process that tries to read stdin must get EOF (DEVNULL) and
    # exit promptly rather than blocking; a genuinely hung child must be killed
    # at the deadline and reported as timed_out.
    repo = _make_repo(tmp_path)

    reads_stdin = [
        sys.executable,
        "-c",
        "import sys; data = sys.stdin.read(); print('read', len(data)); sys.exit(0)",
    ]
    run = build_verify._run_build_check(
        reads_stdin, cwd=repo, env=dict(build_verify.os.environ), timeout=30
    )
    assert run.ran is True
    assert run.timed_out is False
    assert run.exit_code == 0

    hangs = [sys.executable, "-c", "import time; time.sleep(30)"]
    run = build_verify._run_build_check(
        hangs, cwd=repo, env=dict(build_verify.os.environ), timeout=1
    )
    assert run.timed_out is True
    assert run.exit_code is None
    assert "TIMEOUT" in run.output_tail


def test_package_step_is_mac_dmg_on_darwin(monkeypatch):
    monkeypatch.setattr(build_verify.sys, "platform", "darwin")
    name, argv = build_verify._package_step()
    assert name == "package" and "--mac" in argv and "dmg" in argv


def test_package_step_is_linux_appimage_on_linux(monkeypatch):
    # Linux builds a real, tooling-free installer (AppImage), not just --dir.
    monkeypatch.setattr(build_verify.sys, "platform", "linux")
    name, argv = build_verify._package_step()
    assert "--linux" in argv and "AppImage" in argv and "--dir" not in argv


def test_package_step_is_win_nsis_on_win32(monkeypatch):
    monkeypatch.setattr(build_verify.sys, "platform", "win32")
    monkeypatch.setattr(build_verify, "_resolve_cli", lambda name: name)
    name, argv = build_verify._package_step()
    assert name == "package" and "--win" in argv and "nsis" in argv


def test_find_installers_finds_appimage_and_deb_on_linux(tmp_path, monkeypatch):
    monkeypatch.setattr(build_verify.sys, "platform", "linux")
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "app-1.0.0.AppImage").write_text("y")
    (tmp_path / "dist" / "app_1.0.0_amd64.deb").write_text("y")
    (tmp_path / "dist" / "app-1.0.0.dmg").write_text("y")  # wrong platform: ignore
    got = build_verify._find_installers(tmp_path)
    assert len(got) == 2
    assert any(p.endswith(".AppImage") for p in got)
    assert any(p.endswith(".deb") for p in got)
    assert not any(p.endswith(".dmg") for p in got)


def test_find_installers_finds_exe_and_skips_unpacked_on_win(tmp_path, monkeypatch):
    monkeypatch.setattr(build_verify.sys, "platform", "win32")
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "App Setup 1.0.0.exe").write_text("y")  # the installer
    unpacked = tmp_path / "dist" / "win-unpacked"
    unpacked.mkdir()
    (unpacked / "App.exe").write_text("y")  # raw app exe — must NOT be picked up
    got = build_verify._find_installers(tmp_path)
    assert got == [str(tmp_path / "dist" / "App Setup 1.0.0.exe")]


def test_find_installers_lists_all_dmgs_on_darwin(tmp_path, monkeypatch):
    monkeypatch.setattr(build_verify.sys, "platform", "darwin")
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "app-arm64.dmg").write_text("y")
    (tmp_path / "dist" / "app-x64.dmg").write_text("y")
    got = build_verify._find_installers(tmp_path)
    assert len(got) == 2 and all(p.endswith(".dmg") for p in got)


@pytest.mark.skipif(sys.platform == "win32", reason="code-task Windows support is WIP")
def test_find_installers_finds_dmg_in_custom_output_dir_on_darwin(tmp_path, monkeypatch):
    # electron-builder `directories.output: release` (not the default dist/).
    monkeypatch.setattr(build_verify.sys, "platform", "darwin")
    (tmp_path / "release").mkdir()
    (tmp_path / "release" / "app-1.0.0-arm64.dmg").write_text("y")
    got = build_verify._find_installers(tmp_path)
    assert len(got) == 1 and got[0].endswith("release/app-1.0.0-arm64.dmg")


def test_find_installers_ignores_node_modules_dmgs(tmp_path, monkeypatch):
    monkeypatch.setattr(build_verify.sys, "platform", "darwin")
    nm = tmp_path / "node_modules" / "some-pkg"
    nm.mkdir(parents=True)
    (nm / "vendored.dmg").write_text("y")  # must NOT be picked up
    (tmp_path / "release").mkdir()
    (tmp_path / "release" / "real.dmg").write_text("y")
    got = build_verify._find_installers(tmp_path)
    assert got == [str(tmp_path / "release" / "real.dmg")]


def test_clean_package_without_installer_is_failed(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    monkeypatch.setattr(build_verify.sys, "platform", "darwin")
    monkeypatch.setattr(build_verify, "_run_build_check", lambda *a, **k: _ok())
    out = build_verify.verify_build(repo)  # all steps exit 0 but no installer
    assert out.state is TaskState.FAILED
    assert "no installer" in out.detail
    assert out.build.all_passed is False


def test_darwin_package_with_dmg_is_verified(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    monkeypatch.setattr(build_verify.sys, "platform", "darwin")
    monkeypatch.setattr(build_verify, "_run_build_check", lambda *a, **k: _ok())
    (repo / "dist").mkdir()
    (repo / "dist" / "app-1.0.0.dmg").write_text("y")
    out = build_verify.verify_build(repo)
    assert out.state is TaskState.VERIFIED
    assert out.build.installer_path.endswith("app-1.0.0.dmg")
    assert out.build.installer_paths == [out.build.installer_path]


def test_win_package_with_exe_is_verified(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    monkeypatch.setattr(build_verify.sys, "platform", "win32")
    monkeypatch.setattr(build_verify, "_resolve_cli", lambda name: name)
    monkeypatch.setattr(build_verify, "_run_build_check", lambda *a, **k: _ok())
    (repo / "dist").mkdir()
    (repo / "dist" / "App Setup 1.0.0.exe").write_text("y")
    out = build_verify.verify_build(repo)
    assert out.state is TaskState.VERIFIED
    assert out.build.installer_path.endswith(".exe")


def test_linux_package_with_appimage_is_verified(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    monkeypatch.setattr(build_verify.sys, "platform", "linux")
    monkeypatch.setattr(build_verify, "_run_build_check", lambda *a, **k: _ok())
    (repo / "dist").mkdir()
    (repo / "dist" / "app-1.0.0.AppImage").write_text("y")
    out = build_verify.verify_build(repo)
    assert out.state is TaskState.VERIFIED
    assert out.build.installer_path.endswith(".AppImage")
