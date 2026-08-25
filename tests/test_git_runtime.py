from __future__ import annotations

import ast
import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla import git_runtime
from opensquilla.git_runtime import (
    GitCapability,
    GitCapabilityState,
    GitRunResult,
    GitRunState,
)

_DIRECT_PROCESS_LAUNCHERS = frozenset(
    {
        "run",
        "Popen",
        "check_call",
        "check_output",
        "create_owned_subprocess_exec",
        "create_subprocess_exec",
    }
)


@pytest.fixture(autouse=True)
def _clear_capability_cache() -> None:
    git_runtime.clear_git_capability_cache()


def _executable(path: Path, content: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _platform(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setattr(git_runtime, "_platform_name", lambda: name)


@pytest.fixture
def real_git_environment(tmp_path: Path) -> dict[str, str]:
    capability = git_runtime.resolve_git_capability(force_refresh=True)
    if not capability.available:
        pytest.skip(f"Git capability is unavailable: {capability.reason}")
    global_config = tmp_path / "empty-gitconfig"
    global_config.write_text("", encoding="utf-8")
    environment = dict(os.environ)
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_CONFIG_GLOBAL"] = str(global_config)
    return environment


def _run_real_git(
    args: tuple[str, ...],
    *,
    cwd: Path,
    environment: dict[str, str],
) -> GitRunResult:
    result = git_runtime.run_git(
        args,
        cwd=cwd,
        timeout=10.0,
        environment=environment,
    )
    assert result.state is GitRunState.OK, result.stderr_text
    return result


def _init_real_repository(repository: Path, environment: dict[str, str]) -> None:
    repository.mkdir()
    _run_real_git(
        ("-c", "init.templateDir=", "init", "-q"),
        cwd=repository,
        environment=environment,
    )


def _commit_real_repository(repository: Path, environment: dict[str, str]) -> None:
    hooks = repository / ".empty-hooks"
    hooks.mkdir(exist_ok=True)
    _run_real_git(("add", "--all"), cwd=repository, environment=environment)
    _run_real_git(
        (
            "-c",
            "user.name=OpenSquilla Test",
            "-c",
            "user.email=opensquilla@example.test",
            "-c",
            "commit.gpgSign=false",
            "-c",
            f"core.hooksPath={hooks}",
            "commit",
            "-q",
            "-m",
            "base",
        ),
        cwd=repository,
        environment=environment,
    )


def _literal_git_argument(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return Path(node.value).name.casefold() in {"git", "git.exe"}
    if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
        return _literal_git_argument(node.elts[0])
    return False


def test_core_runtime_does_not_launch_literal_git_outside_git_runtime() -> None:
    """Keep background Git calls behind the Apple-shim-safe runtime boundary."""

    package_root = Path(__file__).parents[1] / "src" / "opensquilla"
    violations: list[str] = []
    for relative_root in ("engine", "tools", "cli"):
        for source_path in sorted((package_root / relative_root).rglob("*.py")):
            tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                function_name = (
                    node.func.attr
                    if isinstance(node.func, ast.Attribute)
                    else node.func.id
                    if isinstance(node.func, ast.Name)
                    else ""
                )
                if function_name not in _DIRECT_PROCESS_LAUNCHERS:
                    continue
                if _literal_git_argument(node.args[0]):
                    relative = source_path.relative_to(package_root)
                    violations.append(f"{relative}:{node.lineno}")
    assert violations == []


def test_result_properties_decode_bytes() -> None:
    capability = GitCapability(
        GitCapabilityState.AVAILABLE,
        Path("/example/git"),
        "host",
        None,
    )
    result = GitRunResult(
        GitRunState.OK,
        0,
        "完成".encode(),
        b"warning",
        capability,
    )

    assert capability.available is True
    assert result.ok is True
    assert result.stdout_text == "完成"
    assert result.stderr_text == "warning"


def test_linux_resolves_host_git_by_absolute_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _platform(monkeypatch, "linux")
    executable = _executable(tmp_path / "bin" / "git")

    capability = git_runtime.resolve_git_capability({"PATH": str(executable.parent)})

    assert capability.state is GitCapabilityState.AVAILABLE
    assert capability.executable == executable.absolute()
    assert capability.source == "host"


def test_linux_missing_git_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _platform(monkeypatch, "linux")

    capability = git_runtime.resolve_git_capability({"PATH": ""})

    assert capability.state is GitCapabilityState.UNAVAILABLE
    assert capability.available is False
    assert capability.reason == "git_not_found"


def test_darwin_apple_shim_resolves_selected_developer_git(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _platform(monkeypatch, "darwin")
    shim = _executable(tmp_path / "apple" / "git")
    xcode_select = _executable(tmp_path / "usr" / "bin" / "xcode-select")
    developer_root = tmp_path / "Developer"
    developer_git = _executable(developer_root / "usr" / "bin" / "git")
    monkeypatch.setattr(git_runtime, "_APPLE_GIT_SHIM", shim)
    monkeypatch.setattr(git_runtime, "_XCODE_SELECT", xcode_select)
    calls: list[tuple[list[str], float]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append((command, float(kwargs["timeout"])))
        return subprocess.CompletedProcess(command, 0, f"{developer_root}\n".encode(), b"")

    monkeypatch.setattr(git_runtime.subprocess, "run", fake_run)

    capability = git_runtime.resolve_git_capability({"PATH": str(shim.parent)})

    assert capability.executable == developer_git.absolute()
    assert capability.source == "apple_developer"
    assert calls == [([str(xcode_select), "--print-path"], 1.0)]


def test_darwin_invalid_apple_shim_continues_to_later_host_git(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _platform(monkeypatch, "darwin")
    shim = _executable(tmp_path / "apple" / "git")
    xcode_select = _executable(tmp_path / "usr" / "bin" / "xcode-select")
    homebrew_git = _executable(tmp_path / "homebrew" / "git")
    monkeypatch.setattr(git_runtime, "_APPLE_GIT_SHIM", shim)
    monkeypatch.setattr(git_runtime, "_XCODE_SELECT", xcode_select)
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 2, b"", b"not installed")

    monkeypatch.setattr(git_runtime.subprocess, "run", fake_run)

    capability = git_runtime.resolve_git_capability(
        {"PATH": os.pathsep.join((str(shim.parent), str(homebrew_git.parent)))}
    )

    assert capability.executable == homebrew_git.absolute()
    assert capability.source == "host"
    assert calls == [[str(xcode_select), "--print-path"]]


def test_darwin_symlink_to_apple_shim_is_not_treated_as_host_git(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _platform(monkeypatch, "darwin")
    shim = _executable(tmp_path / "apple" / "git")
    linked_git = tmp_path / "linked" / "git"
    linked_git.parent.mkdir(parents=True)
    linked_git.symlink_to(shim)
    xcode_select = _executable(tmp_path / "usr" / "bin" / "xcode-select")
    monkeypatch.setattr(git_runtime, "_APPLE_GIT_SHIM", shim)
    monkeypatch.setattr(git_runtime, "_XCODE_SELECT", xcode_select)
    monkeypatch.setattr(
        git_runtime.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 2, b"", b"missing"),
    )

    capability = git_runtime.resolve_git_capability({"PATH": str(linked_git.parent)})

    assert capability.state is GitCapabilityState.UNAVAILABLE
    assert capability.reason == "apple_developer_tools_unavailable"


def test_darwin_xcode_select_timeout_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _platform(monkeypatch, "darwin")
    shim = _executable(tmp_path / "apple" / "git")
    xcode_select = _executable(tmp_path / "usr" / "bin" / "xcode-select")
    monkeypatch.setattr(git_runtime, "_APPLE_GIT_SHIM", shim)
    monkeypatch.setattr(git_runtime, "_XCODE_SELECT", xcode_select)

    def time_out(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(command, 1.0)

    monkeypatch.setattr(git_runtime.subprocess, "run", time_out)

    capability = git_runtime.resolve_git_capability({"PATH": str(shim.parent)})

    assert capability.state is GitCapabilityState.UNAVAILABLE
    assert capability.reason == "xcode_select_timed_out"


def test_windows_safe_prefers_managed_and_full_prefers_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _platform(monkeypatch, "windows")
    managed_git = _executable(tmp_path / "managed" / "git.exe")
    host_git = _executable(tmp_path / "host" / "git.exe")
    monkeypatch.setattr(git_runtime, "_managed_git_path", lambda _platform: managed_git)
    environment = {"PATH": f"{host_git.parent};{tmp_path / 'other'}", "PATHEXT": ".exe;.cmd"}

    safe = git_runtime.resolve_git_capability(environment, run_mode="safe")
    full = git_runtime.resolve_git_capability(environment, run_mode="full")

    assert safe.executable == managed_git.absolute()
    assert safe.source == "managed"
    assert full.executable == host_git.absolute()
    assert full.source == "host"


def test_windows_turn_scope_sets_default_resolver_priority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _platform(monkeypatch, "windows")
    managed_git = _executable(tmp_path / "managed" / "git.exe")
    host_git = _executable(tmp_path / "host" / "git.exe")
    monkeypatch.setattr(git_runtime, "_managed_git_path", lambda _platform: managed_git)
    environment = {"PATH": str(host_git.parent), "PATHEXT": ".exe"}

    with git_runtime.git_run_mode_scope("full"):
        capability = git_runtime.resolve_git_capability(environment)

    assert capability.executable == host_git.absolute()
    assert capability.source == "host"


@pytest.mark.parametrize(
    ("runtime_enabled", "git_bash_enabled"),
    [(False, True), (True, False)],
)
def test_windows_managed_git_respects_disabled_runtime_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime_enabled: bool,
    git_bash_enabled: bool,
) -> None:
    _platform(monkeypatch, "windows")
    managed_git = _executable(tmp_path / "managed" / "git.exe")
    monkeypatch.setattr(
        "opensquilla.runtime_packs.resolve_component_binary",
        lambda *_args, **_kwargs: managed_git,
    )
    monkeypatch.setattr(
        "opensquilla.sandbox.integration.active_sandbox_policy",
        lambda: SimpleNamespace(
            runtimes=SimpleNamespace(
                enabled=runtime_enabled,
                git_bash=git_bash_enabled,
            )
        ),
    )

    capability = git_runtime.resolve_git_capability(
        {"PATH": "", "PATHEXT": ".EXE"},
        run_mode="safe",
        force_refresh=True,
    )

    assert capability.state is GitCapabilityState.UNAVAILABLE
    assert capability.reason == "git_not_found"


def test_force_refresh_bypasses_cached_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _platform(monkeypatch, "linux")
    executable = _executable(tmp_path / "bin" / "git")
    environment = {"PATH": str(executable.parent)}

    first = git_runtime.resolve_git_capability(environment)
    executable.unlink()
    cached = git_runtime.resolve_git_capability(environment)
    refreshed = git_runtime.resolve_git_capability(environment, force_refresh=True)

    assert first.available is True
    assert cached == first
    assert refreshed.state is GitCapabilityState.UNAVAILABLE


def test_run_git_uses_absolute_path_and_noninteractive_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _platform(monkeypatch, "linux")
    executable = _executable(tmp_path / "bin" / "git")
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, b"clean\n", b"")

    monkeypatch.setattr(git_runtime.subprocess, "run", fake_run)

    result = git_runtime.run_git(
        ("status", "--short"),
        cwd=tmp_path,
        environment={"PATH": str(executable.parent)},
    )

    command, kwargs = calls[0]
    child_environment = kwargs["env"]
    assert isinstance(child_environment, dict)
    assert command == [str(executable.absolute()), "status", "--short"]
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert child_environment["GIT_TERMINAL_PROMPT"] == "0"
    assert child_environment["GCM_INTERACTIVE"] == "Never"
    assert child_environment["LC_ALL"] == "C"
    assert result.state is GitRunState.OK
    assert result.stdout == b"clean\n"


def test_run_git_user_interaction_preserves_credentials_and_stdin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _platform(monkeypatch, "linux")
    executable = _executable(tmp_path / "bin" / "git")
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, b"cloned\n", b"")

    monkeypatch.setattr(git_runtime.subprocess, "run", fake_run)
    environment = {
        "PATH": str(executable.parent),
        "GIT_TERMINAL_PROMPT": "1",
        "GCM_INTERACTIVE": "Always",
        "GIT_ASKPASS": "/example/askpass",
        "SSH_ASKPASS": "/example/ssh-askpass",
        "LC_ALL": "zh_CN.UTF-8",
        "LANG": "zh_CN.UTF-8",
    }

    result = git_runtime.run_git(
        ("clone", "https://example.test/private.git"),
        environment=environment,
        allow_user_interaction=True,
    )

    command, kwargs = calls[0]
    child_environment = kwargs["env"]
    assert isinstance(child_environment, dict)
    assert command[0] == str(executable.absolute())
    assert "stdin" not in kwargs
    for key, value in environment.items():
        assert child_environment[key] == value
    assert result.state is GitRunState.OK


@pytest.mark.parametrize("allow_user_interaction", [False, True])
def test_run_git_never_launches_unavailable_apple_shim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    allow_user_interaction: bool,
) -> None:
    _platform(monkeypatch, "darwin")
    shim = _executable(tmp_path / "apple" / "git")
    xcode_select = _executable(tmp_path / "usr" / "bin" / "xcode-select")
    monkeypatch.setattr(git_runtime, "_APPLE_GIT_SHIM", shim)
    monkeypatch.setattr(git_runtime, "_XCODE_SELECT", xcode_select)
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(command)
        assert command[0] == str(xcode_select)
        return subprocess.CompletedProcess(command, 2, b"", b"not installed")

    monkeypatch.setattr(git_runtime.subprocess, "run", fake_run)

    result = git_runtime.run_git(
        ("status",),
        environment={"PATH": str(shim.parent)},
        allow_user_interaction=allow_user_interaction,
    )

    assert result.state is GitRunState.UNAVAILABLE
    assert calls == [[str(xcode_select), "--print-path"]]


def test_run_git_unavailable_does_not_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    _platform(monkeypatch, "linux")
    monkeypatch.setattr(
        git_runtime.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("Git must not be spawned when unavailable"),
    )

    result = git_runtime.run_git(("status",), environment={"PATH": ""})

    assert result.state is GitRunState.UNAVAILABLE
    assert result.returncode is None
    assert result.capability.available is False


def test_run_git_classifies_not_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _platform(monkeypatch, "linux")
    executable = _executable(tmp_path / "bin" / "git")
    monkeypatch.setattr(
        git_runtime.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            128,
            b"",
            b"fatal: not a git repository (or any of the parent directories): .git\n",
        ),
    )

    result = git_runtime.run_git(("status",), environment={"PATH": str(executable.parent)})

    assert result.state is GitRunState.NOT_REPOSITORY
    assert result.returncode == 128


def test_run_git_returns_timeout_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _platform(monkeypatch, "linux")
    executable = _executable(tmp_path / "bin" / "git")

    def time_out(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(command, 0.1, output=b"partial", stderr=b"slow")

    monkeypatch.setattr(git_runtime.subprocess, "run", time_out)

    result = git_runtime.run_git(
        ("status",),
        timeout=0.1,
        environment={"PATH": str(executable.parent)},
    )

    assert result.state is GitRunState.TIMED_OUT
    assert result.returncode is None
    assert result.stdout == b"partial"
    assert result.stderr == b"slow"


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (b"true\n", GitRunState.OK),
        (b"false\n", GitRunState.NOT_REPOSITORY),
    ],
)
def test_probe_git_repository_interprets_worktree_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    output: bytes,
    expected: GitRunState,
) -> None:
    capability = GitCapability(
        GitCapabilityState.AVAILABLE,
        tmp_path / "git",
        "host",
        None,
    )
    monkeypatch.setattr(
        git_runtime,
        "run_git",
        lambda *args, **kwargs: GitRunResult(
            GitRunState.OK,
            0,
            output,
            b"",
            capability,
        ),
    )

    assert git_runtime.probe_git_repository(tmp_path) is expected


def test_real_git_plain_directory_is_not_repository(
    tmp_path: Path,
    real_git_environment: dict[str, str],
) -> None:
    plain_directory = tmp_path / "plain"
    plain_directory.mkdir()

    state = git_runtime.probe_git_repository(
        plain_directory,
        timeout=10.0,
        environment=real_git_environment,
    )

    assert state is GitRunState.NOT_REPOSITORY


def test_real_git_unborn_repository_is_available(
    tmp_path: Path,
    real_git_environment: dict[str, str],
) -> None:
    repository = tmp_path / "unborn"
    _init_real_repository(repository, real_git_environment)

    state = git_runtime.probe_git_repository(
        repository,
        timeout=10.0,
        environment=real_git_environment,
    )

    assert state is GitRunState.OK


def test_real_git_repository_subdirectory_is_available(
    tmp_path: Path,
    real_git_environment: dict[str, str],
) -> None:
    repository = tmp_path / "repository"
    _init_real_repository(repository, real_git_environment)
    nested = repository / "src" / "nested"
    nested.mkdir(parents=True)

    state = git_runtime.probe_git_repository(
        nested,
        timeout=10.0,
        environment=real_git_environment,
    )

    assert state is GitRunState.OK


def test_real_git_linked_worktree_is_available(
    tmp_path: Path,
    real_git_environment: dict[str, str],
) -> None:
    repository = tmp_path / "repository"
    _init_real_repository(repository, real_git_environment)
    (repository / "tracked.txt").write_text("base\n", encoding="utf-8")
    _commit_real_repository(repository, real_git_environment)
    worktree = tmp_path / "linked-worktree"
    _run_real_git(
        ("worktree", "add", "-q", "-b", "linked-worktree", str(worktree)),
        cwd=repository,
        environment=real_git_environment,
    )

    state = git_runtime.probe_git_repository(
        worktree,
        timeout=10.0,
        environment=real_git_environment,
    )

    assert state is GitRunState.OK


def test_real_git_clean_and_dirty_status_are_both_successful(
    tmp_path: Path,
    real_git_environment: dict[str, str],
) -> None:
    repository = tmp_path / "repository"
    _init_real_repository(repository, real_git_environment)
    tracked = repository / "tracked.txt"
    tracked.write_text("base\n", encoding="utf-8")
    _commit_real_repository(repository, real_git_environment)

    clean = git_runtime.run_git(
        ("-c", "core.fsmonitor=false", "status", "--porcelain=v1"),
        cwd=repository,
        timeout=10.0,
        environment=real_git_environment,
    )
    tracked.write_text("changed\n", encoding="utf-8")
    dirty = git_runtime.run_git(
        ("-c", "core.fsmonitor=false", "status", "--porcelain=v1"),
        cwd=repository,
        timeout=10.0,
        environment=real_git_environment,
    )

    assert clean.state is GitRunState.OK
    assert clean.stdout == b""
    assert dirty.state is GitRunState.OK
    assert dirty.stdout != b""
    assert b"tracked.txt" in dirty.stdout
