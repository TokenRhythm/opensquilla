from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import opensquilla.runtime_packs as runtime_packs
from opensquilla.runtime_packs.models import RuntimeAvailability
from opensquilla.sandbox.policy_models import RuntimePolicySettings, SandboxPolicy
from opensquilla.sandbox.types import (
    NetworkMode,
    ResourceLimits,
    SecurityLevel,
)
from opensquilla.sandbox.types import (
    SandboxPolicy as ExecutionSandboxPolicy,
)
from opensquilla.tools.builtin import shell
from opensquilla.tools.types import ToolContext, current_tool_context


@pytest.mark.parametrize(
    ("command", "windows", "expected"),
    [
        ("python --version", False, ("python", "python")),
        ("python3 -c 'print(1)'", False, ("python", "python3")),
        ("node --version", False, ("node", "node")),
        ("npm test", False, ("node", "npm")),
        ("npx vite", False, ("node", "npx")),
        ("git.exe --version", True, ("gitBash", "git.exe")),
        ("bash.exe --version", True, ("gitBash", "bash.exe")),
        ("git --version", False, None),
        ("/usr/bin/python --version", False, None),
        ("python --version | head -1", False, None),
        ("python --version && echo done", False, None),
        ("sh -lc 'python --version'", False, None),
    ],
)
def test_direct_runtime_command_is_deliberately_conservative(
    command: str,
    windows: bool,
    expected: tuple[str, str] | None,
) -> None:
    assert shell._direct_runtime_command(command, windows=windows) == expected


def _component_status(component_id: str, availability: RuntimeAvailability) -> object:
    return SimpleNamespace(component_id=component_id, availability=availability)


def test_strict_guest_gets_structured_runtime_unavailable_without_path_leak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = SimpleNamespace(
        components=(
            _component_status("python", RuntimeAvailability.MISSING),
            _component_status("node", RuntimeAvailability.READY),
            _component_status("gitBash", RuntimeAvailability.UNSUPPORTED),
        )
    )
    monkeypatch.setattr(runtime_packs, "status_snapshot", lambda: status)
    monkeypatch.setattr(shell, "active_sandbox_policy", SandboxPolicy)
    secret_path = tmp_path / "private-runtime-bin"
    token = current_tool_context.set(ToolContext(guest_safe=True, run_mode="safe"))
    try:
        result = shell._strict_runtime_unavailable_envelope(
            "python --version",
            {"PATH": str(secret_path)},
        )
    finally:
        current_tool_context.reset(token)

    assert result == {
        "status": "failed",
        "code": "RUNTIME_UNAVAILABLE",
        "componentId": "python",
        "retryable": False,
        "message": "The managed python runtime is unavailable for strict execution.",
    }
    assert str(secret_path) not in str(result)


def test_strict_runtime_preflight_skips_ready_or_compound_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = SimpleNamespace(
        components=(_component_status("node", RuntimeAvailability.READY),)
    )
    monkeypatch.setattr(runtime_packs, "status_snapshot", lambda: status)
    monkeypatch.setattr(shell, "active_sandbox_policy", SandboxPolicy)
    token = current_tool_context.set(ToolContext(guest_safe=True, run_mode="safe"))
    try:
        assert shell._strict_runtime_unavailable_envelope("node -v", {"PATH": ""}) is None
        assert (
            shell._strict_runtime_unavailable_envelope(
                "python -V | head -1",
                {"PATH": ""},
            )
            is None
        )
    finally:
        current_tool_context.reset(token)


def test_disabled_runtime_is_effectively_unavailable_to_strict_guest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = SimpleNamespace(
        components=(_component_status("node", RuntimeAvailability.READY),)
    )
    monkeypatch.setattr(runtime_packs, "status_snapshot", lambda: status)
    monkeypatch.setattr(
        shell,
        "active_sandbox_policy",
        lambda: SandboxPolicy(runtimes=RuntimePolicySettings(node=False)),
    )
    token = current_tool_context.set(ToolContext(guest_safe=True, run_mode="safe"))
    try:
        result = shell._strict_runtime_unavailable_envelope("npm test", {"PATH": ""})
    finally:
        current_tool_context.reset(token)

    assert result is not None and result["code"] == "RUNTIME_UNAVAILABLE"


def test_runtime_environment_reapplication_remains_strict_for_guest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[bool] = []

    def apply(
        environment: dict[str, str],
        *,
        mode: object,
        policy: object,
        require_bundled: bool,
    ) -> dict[str, str]:
        del mode, policy
        observed.append(require_bundled)
        return environment

    monkeypatch.setattr(shell, "apply_bundled_runtime_path", apply)
    token = current_tool_context.set(ToolContext(guest_safe=True, run_mode="safe"))
    try:
        shell._runtime_shell_environment(
            {"PATH": "/untrusted/host"},
            require_bundled=shell._guest_requires_managed_runtime(),
        )
    finally:
        current_tool_context.reset(token)

    assert observed == [True]


def test_strict_guest_does_not_inherit_managed_skill_toolchain_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        shell,
        "managed_skill_env",
        lambda environment: {**environment, "PATH": "/skill-toolchain/bin"},
    )
    token = current_tool_context.set(ToolContext(guest_safe=True, run_mode="safe"))
    try:
        result = shell._managed_skill_environment({"PATH": "/runtime-pack/bin"})
    finally:
        current_tool_context.reset(token)

    assert result["PATH"] == "/runtime-pack/bin"


def test_strict_guest_never_appends_host_windowsapps_aliases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    windows_apps = tmp_path / "Microsoft" / "WindowsApps"
    windows_apps.mkdir(parents=True)
    (windows_apps / "winget.exe").write_bytes(b"")
    (windows_apps / "python.exe").write_bytes(b"")
    monkeypatch.setattr(shell.os, "name", "nt")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    environment = {"PATH": str(tmp_path / "runtime-pack" / "bin")}
    token = current_tool_context.set(ToolContext(guest_safe=True, run_mode="safe"))
    try:
        shell._append_windows_app_alias_path(environment)
    finally:
        current_tool_context.reset(token)

    assert environment["PATH"] == str(tmp_path / "runtime-pack" / "bin")


def test_strict_guest_mounts_runtime_packs_but_not_skill_toolchains(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill_root = tmp_path / "skill-toolchain"
    runtime_root = tmp_path / "runtime-pack"
    skill_root.mkdir()
    runtime_root.mkdir()
    monkeypatch.setattr(
        shell,
        "managed_toolchain_readonly_paths",
        lambda: (skill_root,),
    )
    monkeypatch.setattr(runtime_packs, "runtime_roots", lambda _policy: (runtime_root,))
    monkeypatch.setattr(shell, "active_sandbox_policy", SandboxPolicy)
    policy = ExecutionSandboxPolicy(
        level=SecurityLevel.STANDARD,
        network=NetworkMode.NONE,
        mounts=(),
        workspace_rw=True,
        tmp_writable=True,
        limits=ResourceLimits(),
        env_allowlist=("PATH",),
        require_approval=False,
    )
    token = current_tool_context.set(ToolContext(guest_safe=True, run_mode="safe"))
    try:
        result = shell._policy_with_managed_toolchain_mounts(policy)
    finally:
        current_tool_context.reset(token)

    assert {(mount.host_path, mount.mode) for mount in result.mounts} == {
        (runtime_root, "ro")
    }


@pytest.mark.asyncio
async def test_guest_exec_env_override_cannot_restore_host_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[bool, str]] = []

    def apply(
        environment: dict[str, str],
        *,
        require_bundled: bool = False,
    ) -> dict[str, str]:
        result = dict(environment)
        observed.append((require_bundled, result.get("PATH", "")))
        if require_bundled:
            result["PATH"] = ""
        return result

    async def must_not_execute(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("missing strict runtime must not reach host execution")

    monkeypatch.setattr(shell, "get_runtime", lambda: None)
    monkeypatch.setattr(shell, "_runtime_shell_environment", apply)
    monkeypatch.setattr(shell, "managed_skill_env", lambda environment: environment)
    monkeypatch.setattr(shell, "_run_host_shell_command", must_not_execute)
    monkeypatch.setattr(
        shell,
        "_strict_runtime_unavailable_envelope",
        lambda _command, environment: (
            {
                "status": "failed",
                "code": "RUNTIME_UNAVAILABLE",
                "componentId": "python",
                "retryable": False,
                "message": "The managed python runtime is unavailable for strict execution.",
            }
            if environment.get("PATH") == ""
            else None
        ),
    )
    token = current_tool_context.set(
        ToolContext(
            guest_safe=True,
            run_mode="safe",
            workspace_dir=str(tmp_path),
            environment={"PATH": ""},
            sandbox_policy=SandboxPolicy(),
        )
    )
    try:
        payload = await shell.exec_command(
            "python --version",
            workdir=str(tmp_path),
            env={"PATH": str(tmp_path / "host-bin")},
        )
    finally:
        current_tool_context.reset(token)

    assert any(require and path.endswith("host-bin") for require, path in observed)
    assert json.loads(payload)["code"] == "RUNTIME_UNAVAILABLE"
