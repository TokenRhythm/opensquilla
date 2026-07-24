from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from opensquilla.engine.types import DoneEvent
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.gateway.task_runtime import TaskRuntime
from opensquilla.sandbox.run_context import RUN_CONTEXT_ORIGIN_KEY
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage

OWNER = Principal(
    role="operator",
    scopes=frozenset({"operator.admin"}),
    is_owner=True,
    authenticated=True,
)
REMOTE = Principal(
    role="operator",
    scopes=frozenset({"operator.read", "operator.write"}),
    is_owner=False,
    authenticated=True,
)


@dataclass
class WorkspaceStack:
    storage: SessionStorage
    manager: SessionManager
    runtime: TaskRuntime
    context: RpcContext
    runs: list[Any]
    started: asyncio.Event
    release: asyncio.Event


@asynccontextmanager
async def open_stack(db_path: Path) -> AsyncIterator[WorkspaceStack]:
    storage = await SessionStorage.open(str(db_path))
    manager = SessionManager(storage, inject_time_prefix=False)
    runs: list[Any] = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def handle(run: Any) -> None:
        runs.append(run)
        started.set()
        await release.wait()

    runtime = TaskRuntime(
        storage=storage,
        turn_handler=handle,
        max_concurrency=1,
        running_heartbeat_interval_s=None,
    )
    context = RpcContext(
        conn_id="project-workspace",
        principal=OWNER,
        config=GatewayConfig(
            workspace_dir=str(db_path.parent / "default-workspace"),
            memory={"flush_enabled": False},
            naming={"enabled": False},
        ),
        session_manager=manager,
        task_runtime=runtime,
    )
    try:
        yield WorkspaceStack(
            storage=storage,
            manager=manager,
            runtime=runtime,
            context=context,
            runs=runs,
            started=started,
            release=release,
        )
    finally:
        release.set()
        for reservations in list(runtime._reservations_by_session.values()):
            for reservation in list(reservations):
                await runtime.abort_reservation(reservation)
        await runtime.shutdown(cancel=True, timeout=2.0)
        await storage.close()


async def add_project(stack: WorkspaceStack, path: Path):
    path.mkdir()
    result = await get_dispatcher().dispatch(
        "open-project",
        "workspaces.open",
        {"path": str(path), "trusted": True},
        stack.context,
    )
    assert result.ok is True
    return await stack.storage.get_project_workspace(result.payload["workspace"]["id"])


@pytest.mark.asyncio
async def test_new_chat_binds_project_and_run_context_atomically(tmp_path: Path) -> None:
    async with open_stack(tmp_path / "sessions.db") as stack:
        project = await add_project(stack, tmp_path / "project")
        assert project is not None
        key = "agent:main:webchat:project-first-send"

        response = await get_dispatcher().dispatch(
            "project-first-send",
            "chat.send",
            {
                "sessionKey": key,
                "message": "pwd",
                "workspaceId": project.workspace_id,
                "clientRequestId": "project-request-1",
            },
            stack.context,
        )
        await asyncio.wait_for(stack.started.wait(), timeout=2.0)

        assert response.ok is True
        session = await stack.storage.get_session(key)
        assert session is not None
        assert session.workspace_id == project.workspace_id
        assert session.origin is not None
        saved_context = session.origin[RUN_CONTEXT_ORIGIN_KEY]
        assert saved_context["workspace"] == project.path
        assert saved_context["run_mode"] == "full"


@pytest.mark.asyncio
async def test_turn_tool_context_uses_project_directory(tmp_path: Path) -> None:
    async with open_stack(tmp_path / "sessions.db") as stack:
        project = await add_project(stack, tmp_path / "project")
        assert project is not None
        captured: dict[str, Any] = {}
        ran = asyncio.Event()

        class Runner:
            async def run(self, message: str, session_key: str, **kwargs: Any):
                captured.update(kwargs)
                ran.set()
                yield DoneEvent()

        stack.context.task_runtime = None
        stack.context.turn_runner = Runner()
        response = await get_dispatcher().dispatch(
            "project-tool-context",
            "sessions.send",
            {
                "key": "agent:main:webchat:project-tool-context",
                "message": "pwd",
                "intent": "new_chat",
                "workspaceId": project.workspace_id,
            },
            stack.context,
        )
        await asyncio.wait_for(ran.wait(), timeout=2.0)

        assert response.ok is True
        assert captured["tool_context"].workspace_dir == project.path


@pytest.mark.asyncio
async def test_workspace_id_rejected_for_continue_and_non_owner(
    tmp_path: Path,
) -> None:
    async with open_stack(tmp_path / "sessions.db") as stack:
        project = await add_project(stack, tmp_path / "project")
        assert project is not None
        key = "agent:main:webchat:existing"
        await stack.manager.create(key)

        continued = await get_dispatcher().dispatch(
            "continue-project",
            "sessions.send",
            {
                "key": key,
                "message": "no",
                "intent": "continue",
                "workspaceId": project.workspace_id,
            },
            stack.context,
        )
        assert continued.ok is False
        assert continued.error.code == "INVALID_REQUEST"

        remote_ctx = RpcContext(
            conn_id="remote-project",
            principal=REMOTE,
            config=stack.context.config,
            session_manager=stack.manager,
            task_runtime=stack.runtime,
        )
        remote = await get_dispatcher().dispatch(
            "remote-project-send",
            "chat.send",
            {
                "sessionKey": "agent:main:webchat:remote",
                "message": "no",
                "workspaceId": project.workspace_id,
            },
            remote_ctx,
        )
        assert remote.ok is False
        assert remote.error.code == "OWNER_REQUIRED"


@pytest.mark.asyncio
async def test_removed_or_missing_project_rejects_without_creating_session(
    tmp_path: Path,
) -> None:
    async with open_stack(tmp_path / "sessions.db") as stack:
        project_path = tmp_path / "project"
        project = await add_project(stack, project_path)
        assert project is not None
        await stack.storage.remove_project_workspace(project.workspace_id)

        removed_key = "agent:main:webchat:removed-project"
        removed = await get_dispatcher().dispatch(
            "removed-project",
            "chat.send",
            {
                "sessionKey": removed_key,
                "message": "no",
                "workspaceId": project.workspace_id,
            },
            stack.context,
        )
        assert removed.ok is False
        assert await stack.storage.get_session(removed_key) is None

        restored = await stack.storage.create_or_restore_project_workspace(
            path=project.path,
            path_key=project.path_key,
            display_name=project.display_name,
            trusted_at=project.trusted_at,
        )
        project_path.rmdir()
        missing_key = "agent:main:webchat:missing-project"
        missing = await get_dispatcher().dispatch(
            "missing-project",
            "chat.send",
            {
                "sessionKey": missing_key,
                "message": "no",
                "workspaceId": restored.workspace_id,
            },
            stack.context,
        )
        assert missing.ok is False
        assert await stack.storage.get_session(missing_key) is None


@pytest.mark.asyncio
async def test_workspace_changes_participate_in_idempotency_fingerprint(
    tmp_path: Path,
) -> None:
    async with open_stack(tmp_path / "sessions.db") as stack:
        first = await add_project(stack, tmp_path / "first")
        second = await add_project(stack, tmp_path / "second")
        assert first is not None and second is not None
        key = "agent:main:webchat:fingerprint-project"
        params = {
            "sessionKey": key,
            "message": "same",
            "workspaceId": first.workspace_id,
            "clientRequestId": "same-request-id",
        }
        accepted = await get_dispatcher().dispatch(
            "fingerprint-first", "chat.send", params, stack.context
        )
        await asyncio.wait_for(stack.started.wait(), timeout=2.0)
        conflict = await get_dispatcher().dispatch(
            "fingerprint-second",
            "chat.send",
            {**params, "workspaceId": second.workspace_id},
            stack.context,
        )

        assert accepted.ok is True
        assert conflict.ok is False
        assert conflict.error.code == "IDEMPOTENCY_CONFLICT"


@pytest.mark.asyncio
async def test_bootstrap_and_fork_preserve_project_workspace(tmp_path: Path) -> None:
    async with open_stack(tmp_path / "sessions.db") as stack:
        project = await add_project(stack, tmp_path / "project")
        assert project is not None
        parent = await stack.manager.create(
            "agent:main:webchat:project-parent",
            workspace_id=project.workspace_id,
            origin={
                RUN_CONTEXT_ORIGIN_KEY: {
                    "run_mode": "standard",
                    "workspace": project.path,
                }
            },
        )

        bootstrap = await get_dispatcher().dispatch(
            "project-bootstrap",
            "sessions.bootstrap",
            {"key": parent.session_key},
            stack.context,
        )
        child = await stack.manager.branch(
            parent.session_key,
            "agent:main:webchat:project-child",
        )

        assert bootstrap.ok is True
        assert bootstrap.payload["session"]["workspace"] == project.path
        assert child.workspace_id == project.workspace_id
        assert child.origin == parent.origin
