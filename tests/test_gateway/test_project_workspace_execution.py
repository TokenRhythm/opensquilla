from __future__ import annotations

import asyncio
import os
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
from opensquilla.sandbox.run_context import (
    RUN_CONTEXT_ORIGIN_KEY,
    run_context_from_origin_payload,
)
from opensquilla.sandbox.run_mode import RunMode
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
async def test_new_project_uses_standard_with_project_default_provenance(
    tmp_path: Path,
) -> None:
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
        assert saved_context["run_mode"] == "standard"
        assert saved_context["run_mode_source"] == "project_default"


@pytest.mark.asyncio
async def test_explicit_full_project_uses_operator_default_provenance(
    tmp_path: Path,
) -> None:
    async with open_stack(tmp_path / "sessions.db") as stack:
        stack.context.config.sandbox.run_mode = "full"
        project = await add_project(stack, tmp_path / "project")
        assert project is not None
        key = "agent:main:webchat:project-explicit-full"

        response = await get_dispatcher().dispatch(
            "project-explicit-full",
            "chat.send",
            {
                "sessionKey": key,
                "message": "pwd",
                "workspaceId": project.workspace_id,
                "clientRequestId": "project-explicit-full-request",
            },
            stack.context,
        )
        await asyncio.wait_for(stack.started.wait(), timeout=2.0)

        assert response.ok is True
        session = await stack.storage.get_session(key)
        assert session is not None and session.origin is not None
        restored = run_context_from_origin_payload(
            session.origin[RUN_CONTEXT_ORIGIN_KEY]
        )
        assert restored is not None
        assert restored.run_mode is RunMode.FULL
        assert restored.run_mode_source == "operator_default"


@pytest.mark.parametrize("mode", [RunMode.STANDARD, RunMode.TRUSTED])
@pytest.mark.asyncio
async def test_explicit_standard_and_trusted_project_modes_round_trip(
    tmp_path: Path,
    mode: RunMode,
) -> None:
    async with open_stack(tmp_path / f"{mode.value}-sessions.db") as stack:
        stack.context.config.sandbox.run_mode = mode.value
        project = await add_project(stack, tmp_path / f"{mode.value}-project")
        assert project is not None
        key = f"agent:main:webchat:project-explicit-{mode.value}"

        response = await get_dispatcher().dispatch(
            f"project-explicit-{mode.value}",
            "chat.send",
            {
                "sessionKey": key,
                "message": "pwd",
                "workspaceId": project.workspace_id,
                "clientRequestId": f"project-explicit-{mode.value}-request",
            },
            stack.context,
        )
        await asyncio.wait_for(stack.started.wait(), timeout=2.0)

        assert response.ok is True
        session = await stack.storage.get_session(key)
        assert session is not None and session.origin is not None
        restored = run_context_from_origin_payload(
            session.origin[RUN_CONTEXT_ORIGIN_KEY]
        )
        assert restored is not None
        assert restored.run_mode is mode
        assert restored.run_mode_source == "operator_default"


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


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX symlinks")
@pytest.mark.asyncio
async def test_retargeted_project_rejects_without_creating_session(
    tmp_path: Path,
) -> None:
    async with open_stack(tmp_path / "sessions.db") as stack:
        project_path = tmp_path / "project"
        replacement = tmp_path / "replacement"
        project = await add_project(stack, project_path)
        assert project is not None
        replacement.mkdir()
        project_path.rename(tmp_path / "project-old")
        project_path.symlink_to(replacement, target_is_directory=True)
        key = "agent:main:webchat:retargeted-project"

        response = await get_dispatcher().dispatch(
            "retargeted-project",
            "chat.send",
            {
                "sessionKey": key,
                "message": "no",
                "workspaceId": project.workspace_id,
            },
            stack.context,
        )

        assert response.ok is False
        assert response.error.code == "WORKSPACE_UNAVAILABLE"
        assert await stack.storage.get_session(key) is None


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
async def test_project_replay_and_conflict_precede_mutable_workspace_validation(
    tmp_path: Path,
) -> None:
    async with open_stack(tmp_path / "sessions.db") as stack:
        project = await add_project(stack, tmp_path / "project")
        assert project is not None
        params = {
            "sessionKey": "agent:main:webchat:project-replay-order",
            "message": "accepted once",
            "workspaceId": project.workspace_id,
            "clientRequestId": "project-replay-order-request",
        }
        accepted = await get_dispatcher().dispatch(
            "project-replay-order-first",
            "chat.send",
            params,
            stack.context,
        )
        await asyncio.wait_for(stack.started.wait(), timeout=2.0)
        assert accepted.ok is True
        await stack.storage.remove_project_workspace(project.workspace_id)

        replay = await get_dispatcher().dispatch(
            "project-replay-order-same",
            "chat.send",
            params,
            stack.context,
        )
        conflict = await get_dispatcher().dispatch(
            "project-replay-order-conflict",
            "chat.send",
            {**params, "message": "different fingerprint"},
            stack.context,
        )

        assert replay.ok is True
        assert replay.payload["replayed"] is True
        assert replay.payload["message_id"] == accepted.payload["message_id"]
        assert replay.payload["task_id"] == accepted.payload["task_id"]
        assert conflict.ok is False
        assert conflict.error.code == "IDEMPOTENCY_CONFLICT"


@pytest.mark.asyncio
async def test_replay_survives_project_removal_and_missing_directory(
    tmp_path: Path,
) -> None:
    async with open_stack(tmp_path / "sessions.db") as stack:
        project_path = tmp_path / "project"
        project = await add_project(stack, project_path)
        assert project is not None
        params = {
            "sessionKey": "agent:main:webchat:workspace-replay",
            "message": "pwd",
            "workspaceId": project.workspace_id,
            "clientRequestId": "stable-project-request",
        }
        first = await get_dispatcher().dispatch(
            "workspace-replay-first",
            "chat.send",
            params,
            stack.context,
        )
        assert first.ok is True
        await stack.storage.remove_project_workspace(project.workspace_id)
        project_path.rmdir()
        replay = await get_dispatcher().dispatch(
            "workspace-replay-second",
            "chat.send",
            params,
            stack.context,
        )
        assert replay.ok is True
        assert replay.payload["replayed"] is True
        assert replay.payload["task_id"] == first.payload["task_id"]


@pytest.mark.asyncio
async def test_replay_conflict_precedes_workspace_unavailable(
    tmp_path: Path,
) -> None:
    async with open_stack(tmp_path / "sessions.db") as stack:
        project = await add_project(stack, tmp_path / "project")
        assert project is not None
        params = {
            "sessionKey": "agent:main:webchat:workspace-conflict",
            "message": "first",
            "workspaceId": project.workspace_id,
            "clientRequestId": "stable-conflict-request",
        }
        first = await get_dispatcher().dispatch(
            "workspace-conflict-first",
            "chat.send",
            params,
            stack.context,
        )
        assert first.ok is True
        await stack.storage.remove_project_workspace(project.workspace_id)
        conflict = await get_dispatcher().dispatch(
            "workspace-conflict-second",
            "chat.send",
            {**params, "message": "changed"},
            stack.context,
        )
        assert conflict.ok is False
        assert conflict.error.code == "IDEMPOTENCY_CONFLICT"


@pytest.mark.asyncio
async def test_project_first_send_without_task_runtime_is_atomic(
    tmp_path: Path,
) -> None:
    async with open_stack(tmp_path / "sessions.db") as stack:
        project = await add_project(stack, tmp_path / "project")
        assert project is not None
        ran = asyncio.Event()
        run_count = 0

        class Runner:
            async def run(self, message: str, session_key: str, **kwargs: Any):
                nonlocal run_count
                run_count += 1
                ran.set()
                yield DoneEvent()

        stack.context.task_runtime = None
        stack.context.turn_runner = Runner()
        key = "agent:main:webchat:direct-project"
        params = {
            "sessionKey": key,
            "message": "pwd",
            "workspaceId": project.workspace_id,
            "clientRequestId": "direct-1",
            "clientMessageId": "direct-client-message",
            "surfaceId": "webui:direct-project",
        }
        accepted = await get_dispatcher().dispatch(
            "direct-project",
            "chat.send",
            params,
            stack.context,
        )
        assert accepted.ok is True
        await asyncio.wait_for(ran.wait(), timeout=2)
        session = await stack.storage.get_session(key)
        assert session is not None
        assert len(await stack.storage.get_transcript(session.session_id)) == 1
        async with stack.storage.conn.execute(
            "SELECT task_id FROM turn_ingress_receipts WHERE accepted_session_key = ?",
            (key,),
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert row["task_id"] is None

        replay = await get_dispatcher().dispatch(
            "direct-project-replay",
            "chat.send",
            params,
            stack.context,
        )
        await asyncio.sleep(0)
        assert replay.ok is True
        assert replay.payload["replayed"] is True
        for field in ("turn_id", "client_message_id", "surface_id"):
            assert replay.payload[field] == accepted.payload[field]
        assert run_count == 1
        assert len(await stack.storage.get_transcript(session.session_id)) == 1


@pytest.mark.asyncio
async def test_attachment_failure_without_task_runtime_leaves_no_project_session(
    tmp_path: Path,
) -> None:
    async with open_stack(tmp_path / "sessions.db") as stack:
        project = await add_project(stack, tmp_path / "project")
        assert project is not None
        stack.context.task_runtime = None
        key = "agent:main:webchat:missing-attachment-project"
        failed = await get_dispatcher().dispatch(
            "missing-attachment-project",
            "chat.send",
            {
                "sessionKey": key,
                "message": "inspect",
                "workspaceId": project.workspace_id,
                "clientRequestId": "missing-attachment-request",
                "attachments": [
                    {
                        "type": "file",
                        "mime": "text/plain",
                        "name": "missing.txt",
                        "file_uuid": "missing-upload",
                    }
                ],
            },
            stack.context,
        )
        assert failed.ok is False
        assert await stack.storage.get_session(key) is None


@pytest.mark.asyncio
async def test_existing_project_continuation_resolves_persisted_binding_guard(
    tmp_path: Path,
) -> None:
    async with open_stack(tmp_path / "sessions.db") as stack:
        project = await add_project(stack, tmp_path / "project")
        assert project is not None
        key = "agent:main:webchat:project-continuation"
        session = await stack.manager.create(
            key,
            workspace_id=project.workspace_id,
            origin={
                RUN_CONTEXT_ORIGIN_KEY: {
                    "run_mode": "standard",
                    "workspace": project.path,
                }
            },
        )

        accepted = await get_dispatcher().dispatch(
            "project-continuation",
            "sessions.send",
            {
                "key": key,
                "message": "continue",
                "clientRequestId": "project-continuation-request",
            },
            stack.context,
        )
        await asyncio.wait_for(stack.started.wait(), timeout=2.0)

        assert accepted.ok is True
        assert [
            entry.content
            for entry in await stack.storage.get_transcript(session.session_id)
        ] == ["continue"]


@pytest.mark.asyncio
async def test_project_removal_before_atomic_commit_maps_without_partial_writes(
    tmp_path: Path,
) -> None:
    async with open_stack(tmp_path / "sessions.db") as stack:
        project = await add_project(stack, tmp_path / "project")
        assert project is not None
        key = "agent:main:webchat:project-removal-race"
        session = await stack.manager.create(
            key,
            workspace_id=project.workspace_id,
        )
        original_accept_turn = stack.storage.accept_turn

        async def remove_before_accept(*args: Any, **kwargs: Any) -> Any:
            await stack.storage.remove_project_workspace(project.workspace_id)
            return await original_accept_turn(*args, **kwargs)

        stack.storage.accept_turn = remove_before_accept  # type: ignore[method-assign]
        rejected = await get_dispatcher().dispatch(
            "project-removal-race",
            "sessions.send",
            {
                "key": key,
                "message": "must roll back",
                "clientRequestId": "project-removal-race-request",
            },
            stack.context,
        )

        assert rejected.ok is False
        assert rejected.error.code == "WORKSPACE_NOT_FOUND"
        assert await stack.storage.get_transcript(session.session_id) == []
        async with stack.storage.conn.execute(
            "SELECT COUNT(*) FROM turn_ingress_receipts WHERE request_session_key = ?",
            (key,),
        ) as cursor:
            receipt_count = await cursor.fetchone()
        assert receipt_count is not None
        assert receipt_count[0] == 0


@pytest.mark.asyncio
async def test_direct_project_cancellation_after_commit_still_starts_once(
    tmp_path: Path,
) -> None:
    async with open_stack(tmp_path / "sessions.db") as stack:
        project = await add_project(stack, tmp_path / "project")
        assert project is not None
        ran = asyncio.Event()
        run_count = 0

        class Runner:
            async def run(self, message: str, session_key: str, **kwargs: Any):
                nonlocal run_count
                run_count += 1
                ran.set()
                yield DoneEvent()

        stack.context.task_runtime = None
        stack.context.turn_runner = Runner()
        original_accept_turn = stack.storage.accept_turn
        committed = asyncio.Event()
        release_accept = asyncio.Event()

        async def pause_after_commit(*args: Any, **kwargs: Any) -> Any:
            acceptance = await original_accept_turn(*args, **kwargs)
            committed.set()
            await release_accept.wait()
            return acceptance

        stack.storage.accept_turn = pause_after_commit  # type: ignore[method-assign]
        request = asyncio.create_task(
            get_dispatcher().dispatch(
                "direct-project-cancel",
                "chat.send",
                {
                    "sessionKey": "agent:main:webchat:direct-project-cancel",
                    "message": "pwd",
                    "workspaceId": project.workspace_id,
                    "clientRequestId": "direct-project-cancel-request",
                },
                stack.context,
            )
        )
        await asyncio.wait_for(committed.wait(), timeout=2.0)
        request.cancel()
        await asyncio.sleep(0)
        release_accept.set()

        response = await asyncio.wait_for(request, timeout=2.0)
        await asyncio.wait_for(ran.wait(), timeout=2.0)
        assert response.ok is True
        assert run_count == 1


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


@pytest.mark.asyncio
async def test_chat_fork_stays_in_project_workspace_and_sidebar_group(
    tmp_path: Path,
) -> None:
    async with open_stack(tmp_path / "sessions.db") as stack:
        project = await add_project(stack, tmp_path / "project")
        assert project is not None
        parent = await stack.manager.create(
            "agent:main:webchat:project-fork-parent",
            workspace_id=project.workspace_id,
            origin={
                RUN_CONTEXT_ORIGIN_KEY: {
                    "run_mode": "standard",
                    "workspace": project.path,
                }
            },
        )
        await stack.manager.append_message(parent.session_key, "user", "A marker")
        fork_before = await stack.manager.append_message(
            parent.session_key,
            "user",
            "B marker",
        )
        await stack.manager.append_message(parent.session_key, "user", "C marker")

        response = await get_dispatcher().dispatch(
            "project-fork-send",
            "chat.send",
            {
                "sessionKey": parent.session_key,
                "message": "B edited",
                "forkBeforeMessageId": fork_before.message_id,
                "clientRequestId": "project-fork-request-1",
            },
            stack.context,
        )
        await asyncio.wait_for(stack.started.wait(), timeout=2.0)

        assert response.ok is True
        child_key = response.payload["sessionKey"]
        assert child_key != parent.session_key

        child = await stack.storage.get_session(child_key)
        assert child is not None
        assert child.workspace_id == project.workspace_id
        assert child.origin == parent.origin
        assert stack.runs[0].envelope.session_key == child_key
        assert (
            stack.runs[0].envelope.metadata["sandbox_run_context"]["workspace"]
            == project.path
        )

        child_entries = await stack.manager.get_transcript(child_key)
        assert [entry.content for entry in child_entries] == ["A marker", "B edited"]

        listed = await get_dispatcher().dispatch(
            "project-fork-list",
            "sessions.list",
            {"limit": 50},
            stack.context,
        )
        assert listed.ok is True
        child_row = next(
            row for row in listed.payload["sessions"] if row["key"] == child_key
        )
        assert child_row["workspaceId"] == project.workspace_id
        assert child_row["workspace"] == project.path
