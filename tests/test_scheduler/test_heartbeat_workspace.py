"""Heartbeat execution follows the session root, not its Agent instruction root."""

from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from opensquilla.agents.scope import resolve_agent_workspace_dir
from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.types import DoneEvent
from opensquilla.execution_workspaces import configured_execution_workspace
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.execution_workspaces import build_execution_workspace_factory
from opensquilla.gateway.project_workspace_runtime import prepare_heartbeat_tool_context
from opensquilla.gateway.session_services import SessionServiceUnavailableError
from opensquilla.project_workspaces import ProjectWorkspaceStateError
from opensquilla.run_mode import RunMode
from opensquilla.sandbox.run_context import RUN_CONTEXT_ORIGIN_KEY, MountGrant, RunContext
from opensquilla.scheduler.heartbeat_loop import HeartbeatLoop
from opensquilla.scheduler.heartbeat_service import HeartbeatService
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage

KEY = "agent:main:main"


class _FileReadingRunner:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.contents: list[str] = []

    async def run(self, **kwargs: Any):
        self.calls.append(kwargs)
        root = Path(kwargs["tool_context"].workspace_dir)
        self.contents.append((root / "marker.txt").read_text())
        yield DoneEvent()


@pytest.fixture
def workspace_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "profile"))
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "user-state"))
    monkeypatch.setenv("OPENSQUILLA_TEST_PROFILE_LOCK_ROOT", "1")
    config = GatewayConfig(
        sandbox={"run_mode": "full"},
        heartbeat={"enabled": True, "target": "none", "prompt": None},
    )
    agent_root = resolve_agent_workspace_dir("main", config)
    agent_root.mkdir(parents=True)
    (agent_root / "HEARTBEAT.md").write_text("Check the task's marker file.")
    return config, agent_root


def _service(config, storage, manager, runner):
    return HeartbeatService(
        turn_runner=runner, session_storage=storage, channel_manager_ref=lambda: None,
        prepare_tool_context=partial(
            prepare_heartbeat_tool_context, config=config, storage=storage,
            session_manager=manager,
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["managed", "configured", "legacy", "legacy-mode-only"])
@pytest.mark.parametrize("trigger", ["periodic", "now"])
async def test_heartbeat_reads_session_root_without_changing_legacy_binding(
    tmp_path: Path, workspace_config, kind: str, trigger: str,
) -> None:
    config, agent_root = workspace_config
    async with SessionStorage(tmp_path / "sessions.sqlite") as storage:
        factory = build_execution_workspace_factory(config, profile_home=tmp_path / "profile")
        manager = SessionManager(storage, execution_workspace_factory=(
            factory if kind == "managed" else None
        ))
        session = await manager.create(KEY)
        if kind == "configured":
            root = tmp_path / "selected"
            root.mkdir()
            session.execution_workspace = configured_execution_workspace(root)
            await storage.upsert_session(session)
        elif kind == "legacy-mode-only":
            session.origin = {RUN_CONTEXT_ORIGIN_KEY: {"run_mode": "safe"}}
            await storage.upsert_session(session)
        binding = session.execution_workspace
        root = Path(binding["root"]) if binding else agent_root
        (agent_root / "marker.txt").write_text("old-agent-root")
        (root / "marker.txt").write_text("current-task")
        runner = _FileReadingRunner()
        loop = HeartbeatLoop(
            config=config, heartbeat_service=_service(config, storage, manager, runner),
        )
        for expected in ("current-task", "updated-task"):
            (root / "marker.txt").write_text(expected)
            if trigger == "periodic":
                await loop._tick()
            else:
                await loop.run_once_now(reason="test", agent_id="main", session_key=KEY)
            assert runner.contents[-1] == expected
        assert (await storage.get_session(KEY)).execution_workspace == binding
        assert loop._tool_context.workspace_dir == str(agent_root)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["removed", "symlink", "corrupt"])
async def test_heartbeat_revalidates_binding_before_every_run(
    tmp_path: Path, workspace_config, change: str,
) -> None:
    config, agent_root = workspace_config
    async with SessionStorage(tmp_path / "sessions.sqlite") as storage:
        manager = SessionManager(storage, execution_workspace_factory=(
            build_execution_workspace_factory(config, profile_home=tmp_path / "profile")
        ))
        session = await manager.create(KEY)
        root = Path(session.execution_workspace["root"])
        (root / "marker.txt").write_text("current-task")
        (agent_root / "marker.txt").write_text("must-not-read")
        runner = _FileReadingRunner()
        service = _service(config, storage, manager, runner)
        loop = HeartbeatLoop(config=config, heartbeat_service=service)
        await loop.run_once_now(reason="first", agent_id="main", session_key=KEY)
        if change == "corrupt":
            session.execution_workspace = {}
            await storage.upsert_session(session)
        else:
            root.rename(tmp_path / "preserved-source")
            if change == "symlink":
                root.symlink_to(tmp_path / "preserved-source", target_is_directory=True)
        with pytest.raises(ProjectWorkspaceStateError):
            await loop.run_once_now(reason="second", agent_id="main", session_key=KEY)
        assert runner.contents == ["current-task"]
        # The periodic wrapper logs the same failure instead of starting a turn.
        await loop._tick()
        assert len(runner.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("storage_kind", ["missing", "manager"])
async def test_heartbeat_rejects_unavailable_storage_before_execution(
    tmp_path: Path, workspace_config, storage_kind: str,
) -> None:
    config, _ = workspace_config
    async with SessionStorage(tmp_path / "sessions.sqlite") as storage:
        manager = SessionManager(storage, execution_workspace_factory=(
            build_execution_workspace_factory(config, profile_home=tmp_path / "profile")
        ))
        session = await manager.create(KEY)
        runner = _FileReadingRunner()
        invalid_storage = None if storage_kind == "missing" else manager
        loop = HeartbeatLoop(
            config=config,
            heartbeat_service=_service(config, invalid_storage, manager, runner),
        )
        with pytest.raises(SessionServiceUnavailableError, match="requires session storage"):
            await loop.run_once_now(reason="test", agent_id="main", session_key=KEY)
        assert runner.calls == []
        assert (await storage.get_session(KEY)).model_dump() == session.model_dump()


@pytest.mark.asyncio
async def test_heartbeat_missing_session_is_not_created(tmp_path: Path, workspace_config) -> None:
    config, _ = workspace_config
    async with SessionStorage(tmp_path / "sessions.sqlite") as storage:
        manager = SessionManager(storage, execution_workspace_factory=(
            build_execution_workspace_factory(config, profile_home=tmp_path / "profile")
        ))
        runner = _FileReadingRunner()
        loop = HeartbeatLoop(
            config=config, heartbeat_service=_service(config, storage, manager, runner),
        )
        with pytest.raises(KeyError, match="Session not found"):
            await loop.run_once_now(reason="test", agent_id="main", session_key=KEY)
        assert await storage.get_session(KEY) is None
        assert not (tmp_path / "profile" / "tasks").exists()
        assert runner.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("with_sandbox_context", [False, True])
async def test_heartbeat_refresh_preserves_caller_authority_and_agent_instruction_source(
    tmp_path: Path, workspace_config, with_sandbox_context: bool,
) -> None:
    config, agent_root = workspace_config
    async with SessionStorage(tmp_path / "sessions.sqlite") as storage:
        manager = SessionManager(storage, execution_workspace_factory=(
            build_execution_workspace_factory(config, profile_home=tmp_path / "profile")
        ))
        session = await manager.create(KEY)
        root = Path(session.execution_workspace["root"])
        (root / "marker.txt").write_text("task")
        (root / "HEARTBEAT.md").write_text("Wrong instruction source")
        runner = _FileReadingRunner()
        loop = HeartbeatLoop(
            config=config, heartbeat_service=_service(config, storage, manager, runner),
        )
        sandbox = RunContext(
            run_mode=RunMode.SAFE, workspace=str(agent_root),
            mounts=(MountGrant(path=str(tmp_path / "approved"), access="ro", scope="chat"),),
        ) if with_sandbox_context else None
        incoming = replace(
            loop._tool_context, run_mode="safe", sandbox_run_context=sandbox,
            sandbox_mounts=[{"path": str(tmp_path / "approved"), "access": "ro"}],
            sender_id="scheduled-sender", channel_id="scheduled-channel",
        )
        await loop.run_once_now(
            reason="test", agent_id="main", session_key=KEY, tool_context=incoming,
        )
        received = runner.calls[0]["tool_context"]
        assert received is not incoming
        assert received.workspace_dir == str(root)
        assert received.run_mode == "safe"  # Configured owner default is Full.
        assert not received.is_owner
        for field in (
            "elevated", "allowed_tools", "denied_tools", "caller_kind", "interaction_mode",
            "sender_id", "channel_id", "sandbox_mounts",
        ):
            assert getattr(received, field) == getattr(incoming, field)
        if sandbox is not None:
            assert received.sandbox_run_context == replace(sandbox, workspace=str(root))
            assert sandbox.workspace == str(agent_root)
        else:
            assert received.sandbox_run_context is None
        assert incoming.workspace_dir == str(agent_root)
        assert loop._heartbeat_md_path() == agent_root / "HEARTBEAT.md"
        assert "Agent workspace context" in runner.calls[0]["message"]


@pytest.mark.parametrize("light", [False, True])
def test_heartbeat_bootstrap_stays_agent_scoped(tmp_path: Path, workspace_config, light: bool):
    config, agent_root = workspace_config
    task_root = tmp_path / "task"
    task_root.mkdir()
    (agent_root / "HEARTBEAT.md").write_text("agent-instruction-marker")
    (task_root / "HEARTBEAT.md").write_text("wrong-task-instruction-marker")
    runner = TurnRunner(provider_selector=None, config=config)
    prompt = runner._assemble_prompt(
        "main", [], session_key=KEY, workspace_dir=str(task_root),
        bootstrap_context_mode="heartbeat_light" if light else None,
    )
    text = "\n".join(prompt) if isinstance(prompt, tuple) else prompt
    assert "agent-instruction-marker" in text
    assert "wrong-task-instruction-marker" not in text
