from __future__ import annotations

import asyncio
import types
from pathlib import Path

import pytest

from opensquilla import git_runtime
from opensquilla.engine.runtime import TurnRunner
from opensquilla.run_mode import RunMode
from opensquilla.sandbox.integration import active_sandbox_policy
from opensquilla.sandbox.policy_models import RuntimePolicySettings, SandboxPolicy
from opensquilla.tools.types import ToolContext


@pytest.mark.asyncio
async def test_runtime_assigns_fresh_tool_run_budget_key_per_turn() -> None:
    runner = TurnRunner(provider_selector=None, session_manager=None)
    source_context = ToolContext(session_key="agent:main:webchat:demo")
    captured: list[ToolContext] = []

    async def fake_run_turn(self, *args, **kwargs):
        captured.append(args[5])
        if False:
            yield None

    runner._run_turn = types.MethodType(fake_run_turn, runner)

    async for _ in runner.run(
        "first",
        session_key="agent:main:webchat:demo",
        tool_context=source_context,
    ):
        pass
    async for _ in runner.run(
        "second",
        session_key="agent:main:webchat:demo",
        tool_context=source_context,
    ):
        pass

    assert len(captured) == 2
    assert captured[0].session_key == "agent:main:webchat:demo"
    assert captured[1].session_key == "agent:main:webchat:demo"
    assert captured[0].tool_run_budget_key
    assert captured[1].tool_run_budget_key
    assert captured[0].tool_run_budget_key != captured[1].tool_run_budget_key
    assert source_context.tool_run_budget_key is None


@pytest.mark.asyncio
async def test_runtime_binds_tool_context_sandbox_policy_for_entire_turn() -> None:
    runner = TurnRunner(provider_selector=None, session_manager=None)
    source_context = ToolContext(
        session_key="agent:main:webchat:runtime-policy",
        sandbox_policy=SandboxPolicy(
            runtimes=RuntimePolicySettings(enabled=False, git_bash=False),
        ),
    )
    observed: list[tuple[bool, bool]] = []

    async def fake_run_turn(self, *args, **kwargs):
        del self, args, kwargs
        policy = active_sandbox_policy()
        observed.append((policy.runtimes.enabled, policy.runtimes.git_bash))
        if False:
            yield None

    runner._run_turn = types.MethodType(fake_run_turn, runner)

    async for _ in runner.run(
        "hello",
        session_key="agent:main:webchat:runtime-policy",
        tool_context=source_context,
    ):
        pass

    assert observed == [(False, False)]
    # The turn-local policy must not leak into subsequent work on this task.
    assert active_sandbox_policy().runtimes.enabled is True


@pytest.mark.asyncio
async def test_runtime_binds_effective_git_mode_per_concurrent_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git_runtime.clear_git_capability_cache()
    monkeypatch.setattr(git_runtime, "_platform_name", lambda: "windows")
    managed_git = tmp_path / "managed" / "git.exe"
    host_git = tmp_path / "host" / "git.exe"
    managed_git.parent.mkdir()
    host_git.parent.mkdir()
    managed_git.write_bytes(b"")
    host_git.write_bytes(b"")
    monkeypatch.setattr(git_runtime, "_managed_git_path", lambda _platform: managed_git)
    environment = {"PATH": str(host_git.parent), "PATHEXT": ".exe"}
    runner = TurnRunner(provider_selector=None, session_manager=None)
    observed: dict[str, tuple[Path | None, str | None]] = {}

    async def fake_run_turn(self, *args, **kwargs):
        del self, kwargs
        ctx = args[5]
        await asyncio.sleep(0)
        capability = git_runtime.resolve_git_capability(
            environment,
            force_refresh=True,
        )
        observed[str(ctx.session_key)] = (capability.executable, capability.source)
        if False:
            yield None

    runner._run_turn = types.MethodType(fake_run_turn, runner)
    full_context = ToolContext(
        session_key="agent:main:full",
        run_mode=None,
        sandbox_run_context=types.SimpleNamespace(run_mode=RunMode.FULL),
    )
    safe_context = ToolContext(
        session_key="agent:main:safe",
        run_mode=RunMode.SAFE.value,
    )

    async def consume(session_key: str, context: ToolContext) -> None:
        async for _ in runner.run("hello", session_key=session_key, tool_context=context):
            pass

    await asyncio.gather(
        consume("agent:main:full", full_context),
        consume("agent:main:safe", safe_context),
    )

    assert observed == {
        "agent:main:full": (host_git.absolute(), "host"),
        "agent:main:safe": (managed_git.absolute(), "managed"),
    }
    # The last turn scope must not leak into subsequent work on this task.
    outside = git_runtime.resolve_git_capability(environment, force_refresh=True)
    assert (outside.executable, outside.source) == (managed_git.absolute(), "managed")
