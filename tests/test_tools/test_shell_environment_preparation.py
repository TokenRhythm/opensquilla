from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.sandbox.integration import active_sandbox_policy, sandbox_policy_scope
from opensquilla.sandbox.policy_models import SandboxPolicy as StoredSandboxPolicy
from opensquilla.tools.builtin import shell
from opensquilla.tools.types import ToolContext, current_tool_context


class EnvironmentReadyError(Exception):
    pass


@pytest.mark.parametrize("full", [False, True])
@pytest.mark.parametrize("background", [False, True])
async def test_environment_preparation_preserves_context_off_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, full: bool, background: bool,
) -> None:
    main_thread = threading.get_ident()
    calls: list[str] = []
    context = ToolContext(
        is_owner=True, workspace_dir=str(tmp_path), run_mode="full" if full else "safe",
    )
    policy = StoredSandboxPolicy()

    def runtime_environment(env, **kwargs):
        assert threading.get_ident() != main_thread
        assert current_tool_context.get() is context
        assert active_sandbox_policy() == policy
        calls.append("runtime")
        return {**env, "PREPARED": "runtime"}

    def skill_environment(env):
        assert threading.get_ident() != main_thread
        assert current_tool_context.get() is context
        assert env["OVERRIDE"] == "caller"
        calls.append("skills")
        return {**env, "PREPARED": "skills"}

    def ready(command, env, **kwargs):
        assert threading.get_ident() == main_thread
        assert env["PREPARED"] == "skills"
        raise EnvironmentReadyError

    monkeypatch.setattr(shell, "get_runtime", lambda: None)
    monkeypatch.setattr(shell, "full_host_access_active", lambda: full)
    monkeypatch.setattr(shell, "_runtime_shell_environment", runtime_environment)
    monkeypatch.setattr(shell, "_managed_skill_environment", skill_environment)
    monkeypatch.setattr(shell, "_append_windows_app_alias_path", lambda *args, **kwargs: None)
    monkeypatch.setattr(shell, "_runtime_unavailable_envelope", ready)
    monkeypatch.setattr(shell, "_shell_runtime_preflight", ready)
    token = current_tool_context.set(context)
    try:
        with sandbox_policy_scope(policy), pytest.raises(EnvironmentReadyError):
            tool = shell.background_process if background else shell.exec_command
            await tool("echo ok", env={"OVERRIDE": "caller"})
    finally:
        current_tool_context.reset(token)
    expected_runtime_calls = 1 if background else 2
    assert calls == [*(["runtime"] * expected_runtime_calls), "skills"]


@pytest.mark.parametrize("full", [False, True])
@pytest.mark.parametrize("background", [False, True])
async def test_cancelled_environment_preparation_never_reaches_launch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, full: bool, background: bool,
) -> None:
    loop = asyncio.get_running_loop()
    started, finished = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    reached_preflight = []

    def runtime_environment(env, **kwargs):
        loop.call_soon_threadsafe(started.set)
        try:
            assert release.wait(5)
            return env
        finally:
            loop.call_soon_threadsafe(finished.set)

    def preflight(*args, **kwargs):
        reached_preflight.append(True)
        raise AssertionError("cancelled preparation must not reach launch preflight")

    monkeypatch.setattr(shell, "get_runtime", lambda: None)
    monkeypatch.setattr(shell, "full_host_access_active", lambda: full)
    monkeypatch.setattr(shell, "_runtime_shell_environment", runtime_environment)
    monkeypatch.setattr(shell, "_managed_skill_environment", lambda env: env)
    monkeypatch.setattr(shell, "_runtime_unavailable_envelope", preflight)
    monkeypatch.setattr(shell, "_shell_runtime_preflight", preflight)
    token = current_tool_context.set(ToolContext(is_owner=True, workspace_dir=str(tmp_path)))
    task = None
    try:
        tool = shell.background_process if background else shell.exec_command
        task = asyncio.create_task(tool("echo ok", env={"SYNTHETIC": "1"}))
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, .5)
        assert not finished.is_set()
        release.set()
        await asyncio.wait_for(finished.wait(), 2)
        await asyncio.sleep(0)
        assert not reached_preflight
    finally:
        release.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        current_tool_context.reset(token)


async def test_preparation_slots_survive_caller_cancellation() -> None:
    loop = asyncio.get_running_loop()
    started = [asyncio.Event() for _ in range(4)]
    release = [threading.Event() for _ in range(4)]
    completed = [asyncio.Event() for _ in range(4)]
    active = 0
    maximum = 0
    lock = threading.Lock()

    def work(index):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        loop.call_soon_threadsafe(started[index].set)
        try:
            assert release[index].wait(5)
            return index
        finally:
            with lock:
                active -= 1
            loop.call_soon_threadsafe(completed[index].set)

    tasks = []
    try:
        for index in range(2):
            tasks.append(asyncio.create_task(shell._prepare_shell_runtime(lambda i=index: work(i))))
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started[:2])), 2)
        tasks[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[0]
        tasks.append(asyncio.create_task(shell._prepare_shell_runtime(lambda: work(2))))
        await asyncio.sleep(0)
        tasks[2].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[2]
        tasks.append(asyncio.create_task(shell._prepare_shell_runtime(lambda: work(3))))
        # A spare default-executor worker and the loop remain available.
        assert await asyncio.wait_for(asyncio.to_thread(lambda: "spare"), .5) == "spare"
        assert not started[2].is_set() and not started[3].is_set()
        release[0].set()
        await asyncio.wait_for(started[3].wait(), 2)
        assert completed[0].is_set()
        release[1].set()
        release[3].set()
        assert await tasks[1] == 1
        assert await tasks[3] == 3
        assert maximum == 2
        assert not started[2].is_set()
    finally:
        for event in release:
            event.set()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_preparation_error_releases_slot() -> None:
    def fail():
        raise ValueError("invalid runtime")

    with pytest.raises(ValueError, match="invalid runtime"):
        await shell._prepare_shell_runtime(fail)
    assert await shell._prepare_shell_runtime(lambda: "next") == "next"


async def test_guest_preparation_preserves_environment_and_skips_managed_skills(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = ToolContext(guest_safe=True, environment={"PATH": "guest", "GUEST": "1"})
    observed = []

    def runtime_environment(env, *, require_bundled):
        assert current_tool_context.get() is context
        assert require_bundled is True
        observed.append(dict(env))
        return {**env, "PATH": "verified-only"}

    def unexpected_skills(env):
        raise AssertionError("Guest must not inherit managed skill paths")

    monkeypatch.setenv("PRIVATE_HOST_ONLY", "not-for-guest")
    monkeypatch.setattr(shell, "_runtime_shell_environment", runtime_environment)
    monkeypatch.setattr(shell, "managed_skill_env", unexpected_skills)
    token = current_tool_context.set(context)
    try:
        result = await shell._prepare_shell_runtime(
            lambda: shell._managed_skill_environment(shell._base_shell_environment())
        )
    finally:
        current_tool_context.reset(token)
    assert observed == [{"PATH": "guest", "GUEST": "1"}]
    assert result == {"PATH": "verified-only", "GUEST": "1"}


@pytest.mark.parametrize("background", [False, True])
async def test_safe_managed_mount_preparation_runs_off_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, background: bool,
) -> None:
    main_thread = threading.get_ident()
    runtime = SimpleNamespace(
        effective=SimpleNamespace(sandbox_enabled=True), backend=SimpleNamespace(name="synthetic"),
    )
    policy = SimpleNamespace(network=None, env_allowlist=("PATH",))
    context = ToolContext(is_owner=True, workspace_dir=str(tmp_path), run_mode="safe")

    async def gate(**kwargs):
        request = SimpleNamespace(cwd=tmp_path, action_kind=kwargs["action_kind"], policy=policy)
        return object(), policy, request

    def mounts(value):
        assert value is policy
        assert threading.get_ident() != main_thread
        assert current_tool_context.get() is context
        raise EnvironmentReadyError

    monkeypatch.setattr(shell, "get_runtime", lambda: runtime)
    monkeypatch.setattr(shell, "full_host_access_active", lambda: False)
    monkeypatch.setattr(shell, "_host_execution_allowed", lambda: False)
    monkeypatch.setattr(shell, "_base_shell_environment", lambda: {"PATH": ""})
    monkeypatch.setattr(shell, "_runtime_shell_environment", lambda env, **kwargs: env)
    monkeypatch.setattr(shell, "_managed_skill_environment", lambda env: env)
    monkeypatch.setattr(shell, "_shell_runtime_preflight", lambda *args, **kwargs: None)
    monkeypatch.setattr(shell, "gate_action", gate)
    monkeypatch.setattr(shell, "consume_backend_denial_retry", lambda *args, **kwargs: None)
    monkeypatch.setattr(shell, "_policy_with_managed_toolchain_mounts", mounts)
    token = current_tool_context.set(context)
    try:
        with pytest.raises(EnvironmentReadyError):
            tool = shell.background_process if background else shell.exec_command
            await tool("echo ok", workdir=str(tmp_path))
    finally:
        current_tool_context.reset(token)
