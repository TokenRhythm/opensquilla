from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from opensquilla.application.approval_queue import ApprovalQueue
from opensquilla.engine.types import DoneEvent
from opensquilla.gateway.agent_tasks import get_agent_task_registry
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.boot import dispatch_task_runtime_turn
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.routing import build_cli_route_envelope
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.gateway.task_runtime import TaskRuntime
from opensquilla.project_workspaces import ProjectWorkspaceStateError
from opensquilla.sandbox.backend.bubblewrap import BubblewrapBackend
from opensquilla.sandbox.backend.seatbelt import SeatbeltBackend
from opensquilla.sandbox.backend.unavailable import UnavailableBackend
from opensquilla.sandbox.config import SandboxSettings
from opensquilla.sandbox.integration import configure_runtime, reset_runtime
from opensquilla.sandbox.run_context import (
    RUN_CONTEXT_ORIGIN_KEY,
    run_context_from_origin_payload,
)
from opensquilla.sandbox.run_mode import RunMode
from opensquilla.sandbox.types import SandboxBackendError
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import SessionNode
from opensquilla.session.storage import SessionStorage
from opensquilla.tools.builtin import filesystem as fs
from opensquilla.tools.run_mode import full_host_access_for_context
from opensquilla.tools.types import current_tool_context

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


async def await_direct_task(session_key: str) -> None:
    await asyncio.sleep(0)
    task = get_agent_task_registry().get(session_key)
    if task is not None:
        await asyncio.wait_for(asyncio.shield(task), timeout=2.0)


def create_windows_junction(link: Path, target: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "cmd.exe",
            "/d",
            "/c",
            "mklink",
            "/J",
            str(link),
            str(target),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.asyncio
async def test_new_owner_project_uses_full_with_operator_default_provenance(
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
        assert saved_context["run_mode"] == "full"
        assert saved_context["run_mode_source"] == "operator_default"


@pytest.mark.parametrize("selected_mode", ["standard", "trusted", "full"])
@pytest.mark.asyncio
async def test_new_project_persists_selected_web_run_mode(
    tmp_path: Path,
    selected_mode: str,
) -> None:
    async with open_stack(tmp_path / f"selected-{selected_mode}.db") as stack:
        project = await add_project(stack, tmp_path / f"selected-{selected_mode}-project")
        assert project is not None
        key = f"agent:main:webchat:selected-{selected_mode}"

        response = await get_dispatcher().dispatch(
            f"selected-{selected_mode}",
            "sessions.send",
            {
                "key": key,
                "message": "pwd",
                "intent": "new_chat",
                "workspaceId": project.workspace_id,
                "_source": {
                    "caller_kind": "web",
                    "channel_kind": "webchat",
                    "runMode": selected_mode,
                },
            },
            stack.context,
        )
        await asyncio.wait_for(stack.started.wait(), timeout=2.0)

        assert response.ok is True
        session = await stack.storage.get_session(key)
        assert session is not None and session.origin is not None
        saved_context = session.origin[RUN_CONTEXT_ORIGIN_KEY]
        assert saved_context["run_mode"] == selected_mode
        assert saved_context["run_mode_source"] == "user"


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
        restored = run_context_from_origin_payload(session.origin[RUN_CONTEXT_ORIGIN_KEY])
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
        restored = run_context_from_origin_payload(session.origin[RUN_CONTEXT_ORIGIN_KEY])
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
async def test_non_owner_can_continue_an_existing_project_session(
    tmp_path: Path,
) -> None:
    async with open_stack(tmp_path / "sessions.db") as stack:
        project = await add_project(stack, tmp_path / "project")
        assert project is not None
        key = "agent:main:webchat:remote-existing-project"
        await stack.storage.upsert_session(
            SessionNode(session_key=key, workspace_id=project.workspace_id)
        )
        captured: dict[str, Any] = {}
        ran = asyncio.Event()

        class Runner:
            async def run(self, message: str, session_key: str, **kwargs: Any):
                captured.update(kwargs)
                ran.set()
                yield DoneEvent()

        remote_ctx = RpcContext(
            conn_id="remote-existing-project",
            principal=REMOTE,
            config=stack.context.config,
            session_manager=stack.manager,
            turn_runner=Runner(),
        )
        response = await get_dispatcher().dispatch(
            "remote-existing-project-send",
            "sessions.send",
            {
                "key": key,
                "message": "continue",
                "intent": "continue",
            },
            remote_ctx,
        )
        await asyncio.wait_for(ran.wait(), timeout=2.0)

        assert response.ok is True
        assert captured["tool_context"].workspace_dir == project.path
        assert captured["tool_context"].is_owner is False


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
            entry.content for entry in await stack.storage.get_transcript(session.session_id)
        ] == ["continue"]


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX symlinks")
@pytest.mark.asyncio
async def test_continue_rejects_retargeted_project_before_runner_starts(
    tmp_path: Path,
) -> None:
    async with open_stack(tmp_path / "sessions.db") as stack:
        project_path = tmp_path / "project"
        project = await add_project(stack, project_path)
        assert project is not None
        key = "agent:main:webchat:retargeted-continue"
        await stack.storage.upsert_session(
            SessionNode(
                session_key=key,
                workspace_id=project.workspace_id,
                origin={
                    RUN_CONTEXT_ORIGIN_KEY: {
                        "run_mode": "standard",
                        "workspace": project.path,
                    }
                },
            )
        )
        replacement = tmp_path / "replacement"
        replacement.mkdir()
        project_path.rename(tmp_path / "project-old")
        project_path.symlink_to(replacement, target_is_directory=True)

        class Runner:
            calls: list[dict[str, Any]] = []

            async def run(self, message: str, session_key: str, **kwargs: Any):
                self.calls.append(kwargs)
                yield DoneEvent()

        runner = Runner()
        stack.context.task_runtime = None
        stack.context.turn_runner = runner
        result = await get_dispatcher().dispatch(
            "retargeted-continue",
            "chat.send",
            {"sessionKey": key, "message": "pwd", "intent": "continue"},
            stack.context,
        )
        assert result.ok is False
        assert result.error.code == "WORKSPACE_UNAVAILABLE"
        assert result.error.details["reason"] == "canonical_changed"
        assert runner.calls == []


@pytest.mark.asyncio
async def test_origin_workspace_tamper_cannot_change_project_tool_context(
    tmp_path: Path,
) -> None:
    async with open_stack(tmp_path / "sessions.db") as stack:
        project = await add_project(stack, tmp_path / "project")
        assert project is not None
        outside = tmp_path / "outside"
        outside.mkdir()
        key = "agent:main:webchat:tampered-origin"
        await stack.storage.upsert_session(
            SessionNode(
                session_key=key,
                workspace_id=project.workspace_id,
                origin={
                    RUN_CONTEXT_ORIGIN_KEY: {
                        "run_mode": "standard",
                        "workspace": str(outside),
                    }
                },
            )
        )
        captured: dict[str, Any] = {}
        ran = asyncio.Event()

        class Runner:
            async def run(self, message: str, session_key: str, **kwargs: Any):
                captured.update(kwargs)
                ran.set()
                yield DoneEvent()

        stack.context.task_runtime = None
        stack.context.turn_runner = Runner()
        result = await get_dispatcher().dispatch(
            "tampered-origin",
            "chat.send",
            {"sessionKey": key, "message": "pwd", "intent": "continue"},
            stack.context,
        )
        await asyncio.wait_for(ran.wait(), timeout=2.0)
        assert result.ok is True
        assert captured["tool_context"].workspace_dir == project.path


@pytest.mark.asyncio
async def test_legacy_implicit_full_project_context_remains_full(
    tmp_path: Path,
) -> None:
    async with open_stack(tmp_path / "sessions.db") as stack:
        project = await add_project(stack, tmp_path / "project")
        assert project is not None
        key = "agent:main:webchat:legacy-implicit-full"
        await stack.storage.upsert_session(
            SessionNode(
                session_key=key,
                workspace_id=project.workspace_id,
                origin={
                    RUN_CONTEXT_ORIGIN_KEY: {
                        "run_mode": "full",
                        "workspace": project.path,
                    }
                },
            )
        )
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
            "legacy-implicit-full",
            "sessions.send",
            {"key": key, "message": "pwd"},
            stack.context,
        )
        await asyncio.wait_for(ran.wait(), timeout=2.0)

        assert response.ok is True
        assert captured["tool_context"].run_mode == "full"
        assert captured["tool_context"].sandbox_run_context.run_mode_source is None


@pytest.mark.asyncio
async def test_explicit_full_project_context_preserves_user_provenance(
    tmp_path: Path,
) -> None:
    async with open_stack(tmp_path / "sessions.db") as stack:
        project = await add_project(stack, tmp_path / "project")
        assert project is not None
        key = "agent:main:webchat:explicit-user-full"
        await stack.storage.upsert_session(
            SessionNode(
                session_key=key,
                workspace_id=project.workspace_id,
                origin={
                    RUN_CONTEXT_ORIGIN_KEY: {
                        "run_mode": "full",
                        "run_mode_source": "user",
                        "workspace": project.path,
                    }
                },
            )
        )
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
            "explicit-user-full",
            "sessions.send",
            {"key": key, "message": "pwd"},
            stack.context,
        )
        await asyncio.wait_for(ran.wait(), timeout=2.0)

        assert response.ok is True
        assert captured["tool_context"].run_mode == "full"
        assert captured["tool_context"].sandbox_run_context.run_mode_source == "user"


@pytest.mark.parametrize(
    ("saved_mode", "requested_mode"),
    [("full", "standard"), ("standard", "full")],
)
@pytest.mark.asyncio
async def test_direct_web_project_turn_preserves_authorized_request_mode_at_execution(
    tmp_path: Path,
    saved_mode: str,
    requested_mode: str,
) -> None:
    async with open_stack(tmp_path / f"direct-{saved_mode}-{requested_mode}.db") as stack:
        project = await add_project(stack, tmp_path / f"direct-{saved_mode}-project")
        assert project is not None
        key = f"agent:main:webchat:direct-{saved_mode}-to-{requested_mode}"
        await stack.manager.create(
            key,
            workspace_id=project.workspace_id,
            origin={
                RUN_CONTEXT_ORIGIN_KEY: {
                    "run_mode": saved_mode,
                    "run_mode_source": "operator_default",
                    "workspace": project.path,
                    "domains": [
                        {
                            "domain": "example.com",
                            "scope": "chat",
                            "source": "manual",
                        }
                    ],
                }
            },
        )
        captured: dict[str, Any] = {}

        class Runner:
            async def run(self, message: str, session_key: str, **kwargs: Any):
                captured.update(kwargs)
                yield DoneEvent()

        stack.context.task_runtime = None
        stack.context.turn_runner = Runner()
        response = await get_dispatcher().dispatch(
            f"direct-{saved_mode}-to-{requested_mode}",
            "sessions.send",
            {
                "key": key,
                "message": "pwd",
                "_source": {
                    "caller_kind": "web",
                    "channel_kind": "webchat",
                    "runMode": requested_mode,
                },
            },
            stack.context,
        )
        await await_direct_task(key)

        assert response.ok is True
        tool_context = captured["tool_context"]
        assert tool_context.run_mode == requested_mode
        assert tool_context.workspace_dir == project.path
        assert tool_context.sandbox_run_context.run_mode_source == "user"
        assert [grant.domain for grant in tool_context.sandbox_run_context.domains] == [
            "example.com"
        ]
        persisted_session = await stack.storage.get_session(key)
        assert persisted_session is not None and persisted_session.origin is not None
        persisted_context = persisted_session.origin[RUN_CONTEXT_ORIGIN_KEY]
        assert persisted_context["run_mode"] == requested_mode
        assert persisted_context["run_mode_source"] == "user"
        assert [grant["domain"] for grant in persisted_context["domains"]] == [
            "example.com"
        ]


@pytest.mark.parametrize(
    ("requested_mode", "expected_mode", "expected_mode_source"),
    [
        (None, "standard", "operator_default"),
        ("full", "full", "user"),
    ],
)
@pytest.mark.asyncio
async def test_direct_web_unbound_turn_refreshes_durable_context_before_typed_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    requested_mode: str | None,
    expected_mode: str,
    expected_mode_source: str,
) -> None:
    async with open_stack(tmp_path / f"direct-unbound-{requested_mode or 'saved'}.db") as stack:
        stale_workspace = tmp_path / "stale-workspace"
        current_workspace = tmp_path / "current-workspace"
        stale_workspace.mkdir()
        current_workspace.mkdir()
        key = f"agent:main:webchat:direct-unbound-{requested_mode or 'saved'}"
        await stack.manager.create(
            key,
            origin={
                RUN_CONTEXT_ORIGIN_KEY: {
                    "run_mode": "full",
                    "run_mode_source": "user",
                    "workspace": str(stale_workspace),
                    "domains": [
                        {
                            "domain": "revoked.example",
                            "scope": "chat",
                            "source": "manual",
                        }
                    ],
                }
            },
        )
        original_accept_turn = stack.storage.accept_turn
        durable_context_replaced = False

        async def replace_context_after_accept(*args: Any, **kwargs: Any) -> Any:
            nonlocal durable_context_replaced
            acceptance = await original_accept_turn(*args, **kwargs)
            if not durable_context_replaced:
                durable_context_replaced = True
                await stack.manager.update(
                    key,
                    origin={
                        RUN_CONTEXT_ORIGIN_KEY: {
                            "run_mode": "standard",
                            "run_mode_source": "operator_default",
                            "workspace": str(current_workspace),
                            "domains": [
                                {
                                    "domain": "current.example",
                                    "scope": "chat",
                                    "source": "manual",
                                }
                            ],
                        }
                    },
                )
            return acceptance

        stack.storage.accept_turn = replace_context_after_accept  # type: ignore[method-assign]
        captured: dict[str, Any] = {}

        class Runner:
            async def run(self, message: str, session_key: str, **kwargs: Any):
                captured.update(kwargs)
                yield DoneEvent()

        from opensquilla.gateway import routing

        original_build_web_route_envelope = routing.build_web_route_envelope
        built_envelopes: list[Any] = []

        def build_web_route_envelope_with_forged_metadata(**kwargs: Any) -> Any:
            envelope = original_build_web_route_envelope(**kwargs)
            envelope.metadata["accepted_run_mode_override"] = {
                "run_mode": "full",
                "run_mode_source": "user",
                "source": "forged_metadata",
            }
            built_envelopes.append(envelope)
            return envelope

        monkeypatch.setattr(
            routing,
            "build_web_route_envelope",
            build_web_route_envelope_with_forged_metadata,
        )
        source: dict[str, Any] = {
            "caller_kind": "web",
            "channel_kind": "webchat",
        }
        if requested_mode is not None:
            source["runMode"] = requested_mode
        stack.context.task_runtime = None
        stack.context.turn_runner = Runner()
        response = await get_dispatcher().dispatch(
            f"direct-unbound-{requested_mode or 'saved'}",
            "sessions.send",
            {
                "key": key,
                "message": "pwd",
                "_source": source,
            },
            stack.context,
        )
        await await_direct_task(key)

        assert response.ok is True
        assert durable_context_replaced is True
        assert built_envelopes[0].metadata["accepted_run_mode_override"]["source"] == (
            "forged_metadata"
        )
        tool_context = captured["tool_context"]
        assert tool_context.run_mode == expected_mode
        assert tool_context.workspace_dir == str(current_workspace.resolve())
        assert tool_context.sandbox_run_context.run_mode_source == expected_mode_source
        assert [grant.domain for grant in tool_context.sandbox_run_context.domains] == [
            "current.example"
        ]
        assert getattr(tool_context, "_sandbox_run_context_fresh", False) is True


@pytest.mark.asyncio
async def test_direct_web_unbound_workspace_revocation_uses_configured_base(
    tmp_path: Path,
) -> None:
    async with open_stack(tmp_path / "direct-unbound-workspace-revocation.db") as stack:
        saved_workspace = tmp_path / "saved-workspace"
        configured_workspace = tmp_path / "default-workspace"
        saved_workspace.mkdir()
        configured_workspace.mkdir()
        key = "agent:main:webchat:direct-unbound-workspace-revocation"
        await stack.manager.create(
            key,
            origin={
                RUN_CONTEXT_ORIGIN_KEY: {
                    "run_mode": "standard",
                    "run_mode_source": "operator_default",
                    "workspace": str(saved_workspace),
                    "domains": [
                        {
                            "domain": "retained.example",
                            "scope": "chat",
                            "source": "manual",
                        }
                    ],
                }
            },
        )
        original_accept_turn = stack.storage.accept_turn
        workspace_revoked = False

        async def revoke_workspace_after_accept(*args: Any, **kwargs: Any) -> Any:
            nonlocal workspace_revoked
            acceptance = await original_accept_turn(*args, **kwargs)
            if not workspace_revoked:
                workspace_revoked = True
                await stack.manager.update(
                    key,
                    origin={
                        RUN_CONTEXT_ORIGIN_KEY: {
                            "run_mode": "standard",
                            "run_mode_source": "operator_default",
                            "workspace": None,
                            "domains": [
                                {
                                    "domain": "retained.example",
                                    "scope": "chat",
                                    "source": "manual",
                                }
                            ],
                        }
                    },
                )
            return acceptance

        stack.storage.accept_turn = revoke_workspace_after_accept  # type: ignore[method-assign]
        captured: dict[str, Any] = {}

        class Runner:
            async def run(self, message: str, session_key: str, **kwargs: Any):
                captured.update(kwargs)
                yield DoneEvent()

        stack.context.task_runtime = None
        stack.context.turn_runner = Runner()
        response = await get_dispatcher().dispatch(
            "direct-unbound-workspace-revocation",
            "sessions.send",
            {
                "key": key,
                "message": "pwd",
                "_source": {
                    "caller_kind": "web",
                    "channel_kind": "webchat",
                },
            },
            stack.context,
        )
        await await_direct_task(key)

        assert response.ok is True
        assert workspace_revoked is True
        tool_context = captured["tool_context"]
        assert tool_context.run_mode == "standard"
        assert tool_context.workspace_dir == str(configured_workspace.resolve())
        assert tool_context.workspace_dir != str(saved_workspace.resolve())
        assert [grant.domain for grant in tool_context.sandbox_run_context.domains] == [
            "retained.example"
        ]
        assert getattr(tool_context, "_sandbox_run_context_fresh", False) is True


@pytest.mark.parametrize(
    ("saved_mode", "requested_mode"),
    [("full", "standard"), ("standard", "full")],
)
@pytest.mark.asyncio
async def test_queued_project_turn_preserves_authorized_request_mode_at_execution(
    tmp_path: Path,
    saved_mode: str,
    requested_mode: str,
) -> None:
    async with open_stack(tmp_path / f"queued-{saved_mode}-{requested_mode}.db") as stack:
        project = await add_project(stack, tmp_path / f"queued-{saved_mode}-project")
        assert project is not None
        key = f"agent:main:webchat:queued-{saved_mode}-to-{requested_mode}"
        await stack.manager.create(
            key,
            workspace_id=project.workspace_id,
            origin={
                RUN_CONTEXT_ORIGIN_KEY: {
                    "run_mode": saved_mode,
                    "run_mode_source": "operator_default",
                    "workspace": project.path,
                    "domains": [
                        {
                            "domain": "example.com",
                            "scope": "chat",
                            "source": "manual",
                        }
                    ],
                }
            },
        )
        captured: dict[str, Any] = {}
        ran = asyncio.Event()

        class Runner:
            async def run(self, message: str, session_key: str, **kwargs: Any):
                captured.update(kwargs)
                ran.set()
                yield DoneEvent()

        async def execute(run: Any) -> None:
            await dispatch_task_runtime_turn(
                run,
                config=stack.context.config,
                session_manager=stack.manager,
                turn_runner=Runner(),
                event_emitter=AsyncMock(),
            )

        stack.runtime._turn_handler = execute
        response = await get_dispatcher().dispatch(
            f"queued-{saved_mode}-to-{requested_mode}",
            "sessions.send",
            {
                "key": key,
                "message": "pwd",
                "_source": {
                    "caller_kind": "web",
                    "channel_kind": "webchat",
                    "runMode": requested_mode,
                },
            },
            stack.context,
        )
        await asyncio.wait_for(ran.wait(), timeout=2.0)

        assert response.ok is True
        tool_context = captured["tool_context"]
        assert tool_context.run_mode == requested_mode
        assert tool_context.workspace_dir == project.path
        assert tool_context.sandbox_run_context.run_mode_source == "user"
        assert [grant.domain for grant in tool_context.sandbox_run_context.domains] == [
            "example.com"
        ]


@pytest.mark.parametrize(
    ("pending_mode", "later_mode"),
    [("full", "standard"), ("standard", "full")],
)
@pytest.mark.asyncio
async def test_collect_reserves_separate_tasks_for_different_accepted_modes(
    tmp_path: Path,
    pending_mode: str,
    later_mode: str,
) -> None:
    async with open_stack(tmp_path / f"collect-{pending_mode}-to-{later_mode}.db") as stack:
        project = await add_project(
            stack,
            tmp_path / f"collect-{pending_mode}-project",
        )
        assert project is not None
        key = f"agent:main:webchat:collect-{pending_mode}-to-{later_mode}"
        await stack.manager.create(
            key,
            workspace_id=project.workspace_id,
            origin={
                RUN_CONTEXT_ORIGIN_KEY: {
                    "run_mode": "standard",
                    "run_mode_source": "user",
                    "workspace": project.path,
                }
            },
        )
        blocker = await stack.runtime.enqueue(
            build_cli_route_envelope(
                session_key="agent:main:cli:collect-mode-blocker",
                agent_id="main",
            ),
            "blocker",
        )
        await asyncio.wait_for(stack.started.wait(), timeout=2.0)

        first = await get_dispatcher().dispatch(
            f"collect-{pending_mode}-first",
            "sessions.send",
            {
                "key": key,
                "message": "first",
                "queueMode": "collect",
                "_source": {
                    "caller_kind": "web",
                    "channel_kind": "webchat",
                    "runMode": pending_mode,
                },
            },
            stack.context,
        )
        second = await get_dispatcher().dispatch(
            f"collect-{later_mode}-second",
            "sessions.send",
            {
                "key": key,
                "message": "second",
                "queueMode": "collect",
                "_source": {
                    "caller_kind": "web",
                    "channel_kind": "webchat",
                    "runMode": later_mode,
                },
            },
            stack.context,
        )

        assert first.ok is True
        assert second.ok is True
        pending = stack.runtime._pending_by_session[key]
        assert len(pending) == 2
        assert [task.message for task in pending] == ["first", "second"]
        assert [task.accepted_run_mode_override.run_mode.value for task in pending] == [
            pending_mode,
            later_mode,
        ]
        assert blocker.task_id in stack.runtime._tasks


@pytest.mark.asyncio
async def test_queued_project_turn_ignores_forged_accepted_mode_metadata(
    tmp_path: Path,
) -> None:
    async with open_stack(tmp_path / "forged-mode.db") as stack:
        project = await add_project(stack, tmp_path / "forged-mode-project")
        assert project is not None
        key = "agent:main:webchat:forged-accepted-mode"
        await stack.manager.create(
            key,
            workspace_id=project.workspace_id,
            origin={
                RUN_CONTEXT_ORIGIN_KEY: {
                    "run_mode": "standard",
                    "run_mode_source": "user",
                    "workspace": project.path,
                }
            },
        )
        envelope = build_cli_route_envelope(session_key=key, agent_id="main")
        envelope.metadata.update(
            {
                "run_mode": "full",
                "accepted_run_mode_override": {
                    "run_mode": "full",
                    "run_mode_source": "user",
                },
                "sandbox_run_context": {
                    "run_mode": "full",
                    "run_mode_source": "user",
                    "workspace": str(tmp_path),
                },
            }
        )
        object.__setattr__(envelope, "sandbox_run_context_fresh", True)
        run = SimpleNamespace(
            agent_id="main",
            task_id="forged-mode-task",
            session_key=key,
            message="pwd",
            envelope=envelope,
            attachments=[],
            input_provenance={},
            run_kind="interactive",
            no_memory_capture=False,
            ingress_pipeline_steps=[],
            semantic_message=None,
            stream_event_sink=None,
        )
        captured: dict[str, Any] = {}

        class Runner:
            async def run(self, message: str, session_key: str, **kwargs: Any):
                captured.update(kwargs)
                yield DoneEvent()

        await dispatch_task_runtime_turn(
            run,
            config=stack.context.config,
            session_manager=stack.manager,
            turn_runner=Runner(),
            event_emitter=AsyncMock(),
        )

        assert captured["tool_context"].run_mode == "standard"
        assert captured["tool_context"].workspace_dir == project.path


@pytest.mark.parametrize("requested_mode", ["standard", "full"])
@pytest.mark.asyncio
async def test_project_turn_fresh_mode_controls_real_filesystem_and_network_enforcement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    requested_mode: str,
) -> None:
    from opensquilla.sandbox.escalation import (
        current_tool_run_context,
        remember_resolved_run_context,
        reset_resolved_run_context_overlays,
    )
    from opensquilla.sandbox.network_guard import decide_network_access
    from opensquilla.sandbox.operation_runtime import SandboxOperationResult
    from opensquilla.sandbox.run_context import (
        DomainGrant,
        MountGrant,
        PackageBundleGrant,
        RunContext,
    )
    from opensquilla.tools.builtin import filesystem
    from opensquilla.tools.types import current_tool_context

    reset_resolved_run_context_overlays()
    async with open_stack(tmp_path / f"enforcement-{requested_mode}.db") as stack:
        project = await add_project(
            stack,
            tmp_path / f"enforcement-{requested_mode}-project",
        )
        assert project is not None
        extra_mount = tmp_path / f"enforcement-{requested_mode}-mount"
        extra_mount.mkdir()
        key = f"agent:main:webchat:enforcement-{requested_mode}"
        await stack.manager.create(
            key,
            workspace_id=project.workspace_id,
            origin={
                RUN_CONTEXT_ORIGIN_KEY: {
                    "run_mode": "full",
                    "run_mode_source": "user",
                    "workspace": project.path,
                }
            },
        )
        remember_resolved_run_context(
            key,
            project.path,
            RunContext(
                run_mode=RunMode.FULL,
                workspace=project.path,
                mounts=(
                    MountGrant(
                        path=str(extra_mount),
                        access="rw",
                        scope="chat",
                    ),
                ),
                domains=(
                    DomainGrant(
                        domain="overlay-grant.example",
                        scope="chat",
                        source="manual",
                    ),
                ),
                bundles=(
                    PackageBundleGrant(
                        bundle_id="python-package-install",
                        scope="chat",
                        source="manual",
                    ),
                ),
                run_mode_source="user",
                source="resolved_overlay",
            ),
        )
        backend_operations: list[Any] = []

        class RecordingFilesystemBackend:
            name = "recording-filesystem"

            def operation_domains_supported(self) -> frozenset[str]:
                return frozenset({"filesystem"})

            async def run_operation(self, operation: Any) -> SandboxOperationResult:
                backend_operations.append(operation)
                request = operation.request
                assert request.path is not None
                request.path.write_text(request.content, encoding="utf-8")
                return SandboxOperationResult(
                    message=f"sandboxed write: {request.path}",
                    created=True,
                )

        runtime = SimpleNamespace(
            effective=SimpleNamespace(sandbox_enabled=True),
            backend=RecordingFilesystemBackend(),
            settings=SimpleNamespace(host_root_readonly=False),
            workspace=Path(project.path),
        )
        monkeypatch.setattr(filesystem, "get_runtime", lambda: runtime)
        captured: dict[str, Any] = {}
        target = Path(project.path) / "guarded.txt"

        class Runner:
            async def run(self, message: str, session_key: str, **kwargs: Any):
                tool_context = kwargs["tool_context"]
                token = current_tool_context.set(tool_context)
                try:
                    captured["write_result"] = await filesystem.write_file(
                        str(target),
                        "guarded",
                    )
                    effective = current_tool_run_context()
                    assert effective is not None
                    captured["effective"] = effective
                    captured["granted_network"] = decide_network_access(
                        "overlay-grant.example",
                        effective,
                    )
                    captured["unknown_network"] = decide_network_access(
                        "unlisted-authority-gap.example",
                        effective,
                    )
                finally:
                    current_tool_context.reset(token)
                yield DoneEvent()

        stack.context.task_runtime = None
        stack.context.turn_runner = Runner()
        try:
            response = await get_dispatcher().dispatch(
                f"enforcement-{requested_mode}",
                "sessions.send",
                {
                    "key": key,
                    "message": "write guarded.txt",
                    "_source": {
                        "caller_kind": "web",
                        "channel_kind": "webchat",
                        "runMode": requested_mode,
                    },
                },
                stack.context,
            )
            await await_direct_task(key)
        finally:
            reset_resolved_run_context_overlays()

        assert response.ok is True
        effective = captured["effective"]
        assert effective.run_mode.value == requested_mode
        assert effective.workspace == project.path
        assert effective.run_mode_source == "user"
        assert [grant.path for grant in effective.mounts] == [str(extra_mount)]
        assert captured["granted_network"].reason == (
            "full_host_access" if requested_mode == "full" else "domain_grant"
        )
        assert captured["unknown_network"].status == (
            "allow" if requested_mode == "full" else "ask"
        )
        assert captured["unknown_network"].reason == (
            "full_host_access" if requested_mode == "full" else "unknown_domain"
        )
        assert len(backend_operations) == (0 if requested_mode == "full" else 1)
        assert target.read_text(encoding="utf-8") == "guarded"


@pytest.mark.asyncio
async def test_runtime_send_rehydrates_unbound_session_before_real_enforcement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from opensquilla.sandbox.escalation import (
        current_tool_run_context,
        reset_resolved_run_context_overlays,
    )
    from opensquilla.sandbox.network_guard import decide_network_access
    from opensquilla.sandbox.operation_runtime import SandboxOperationResult
    from opensquilla.sandbox.run_context import RunContext
    from opensquilla.tools.builtin import filesystem
    from opensquilla.tools.types import current_tool_context

    reset_resolved_run_context_overlays()
    storage = await SessionStorage.open(str(tmp_path / "runtime-send.db"))
    manager = SessionManager(storage, inject_time_prefix=False)
    workspace = tmp_path / "unbound-workspace"
    workspace.mkdir()
    key = "agent:main:webchat:runtime-send-rehydrate"
    full_context = RunContext(
        run_mode=RunMode.FULL,
        workspace=str(workspace),
        run_mode_source="user",
        source="saved",
    )
    standard_context = RunContext(
        run_mode=RunMode.STANDARD,
        workspace=str(workspace),
        run_mode_source="user",
        source="saved",
    )
    await manager.create(
        key,
        origin={RUN_CONTEXT_ORIGIN_KEY: full_context.to_origin_payload()},
    )

    backend_operations: list[Any] = []

    class RecordingFilesystemBackend:
        name = "recording-filesystem"

        def operation_domains_supported(self) -> frozenset[str]:
            return frozenset({"filesystem"})

        async def run_operation(self, operation: Any) -> SandboxOperationResult:
            backend_operations.append(operation)
            request = operation.request
            assert request.path is not None
            request.path.write_text(request.content, encoding="utf-8")
            return SandboxOperationResult(
                message=f"sandboxed write: {request.path}",
                created=True,
            )

    monkeypatch.setattr(
        filesystem,
        "get_runtime",
        lambda: SimpleNamespace(
            effective=SimpleNamespace(sandbox_enabled=True),
            backend=RecordingFilesystemBackend(),
            settings=SimpleNamespace(host_root_readonly=False),
            workspace=workspace,
        ),
    )
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    observations: list[dict[str, Any]] = []

    class Runner:
        async def run(self, message: str, session_key: str, **kwargs: Any):
            index = len(observations)
            tool_context = kwargs["tool_context"]
            token = current_tool_context.set(tool_context)
            try:
                effective = current_tool_run_context()
                assert effective is not None
                target = workspace / f"runtime-send-{index}.txt"
                write_result = await filesystem.write_file(
                    str(target),
                    f"turn-{index}",
                )
                observations.append(
                    {
                        "mode": effective.run_mode.value,
                        "fresh": bool(
                            getattr(
                                tool_context,
                                "_sandbox_run_context_fresh",
                                False,
                            )
                        ),
                        "network": decide_network_access(
                            "runtime-send-unknown.example",
                            effective,
                        ),
                        "write_result": write_result,
                        "target": target,
                    }
                )
            finally:
                current_tool_context.reset(token)
            if index == 0:
                first_started.set()
                await release_first.wait()
            yield DoneEvent()

    config = GatewayConfig(
        workspace_dir=str(workspace),
        memory={"flush_enabled": False},
        naming={"enabled": False},
        agent_stream_heartbeat_interval_seconds=0.0,
        agent_stream_idle_timeout_seconds=1.0,
    )

    async def handle(run: Any) -> None:
        await dispatch_task_runtime_turn(
            run,
            config=config,
            session_manager=manager,
            turn_runner=Runner(),
            event_emitter=AsyncMock(),
        )

    runtime = TaskRuntime(
        storage=storage,
        turn_handler=handle,
        max_concurrency=1,
        running_heartbeat_interval_s=None,
    )
    envelope = build_cli_route_envelope(
        session_key=key,
        agent_id="main",
        principal_is_owner=True,
        run_mode="full",
    )
    envelope.metadata["sandbox_run_context"] = full_context.to_origin_payload()
    object.__setattr__(envelope, "sandbox_run_context_fresh", True)

    try:
        first = await runtime.enqueue(envelope, "first")
        await asyncio.wait_for(first_started.wait(), timeout=2.0)
        cached_after_first = runtime._last_envelope_by_session[key]
        await manager.update(
            key,
            origin={RUN_CONTEXT_ORIGIN_KEY: standard_context.to_origin_payload()},
        )
        followup = await runtime.send(key, "followup")
        release_first.set()
        assert (await runtime.wait(first.task_id, timeout=2.0)).status == "succeeded"
        assert (await runtime.wait(followup.task_id, timeout=2.0)).status == "succeeded"

        assert [item["mode"] for item in observations] == ["full", "standard"]
        assert [item["network"].status for item in observations] == ["allow", "ask"]
        assert [item["network"].reason for item in observations] == [
            "full_host_access",
            "unknown_domain",
        ]
        assert len(backend_operations) == 1
        assert observations[0]["target"].read_text(encoding="utf-8") == "turn-0"
        assert observations[1]["target"].read_text(encoding="utf-8") == "turn-1"
        assert cached_after_first.sandbox_run_context_fresh is False
        assert cached_after_first.metadata is not envelope.metadata
        assert key not in runtime._last_envelope_by_session
    finally:
        release_first.set()
        await runtime.shutdown(cancel=True, timeout=2.0)
        await storage.close()
        reset_resolved_run_context_overlays()


def test_only_trusted_envelope_freshness_reaches_tool_context() -> None:
    from opensquilla.gateway.routing import tool_context_from_envelope
    from opensquilla.sandbox.run_context import RunContext

    envelope = build_cli_route_envelope(
        session_key="agent:main:cli:freshness-marker",
        agent_id="main",
        principal_is_owner=True,
        run_mode="standard",
    )
    envelope.metadata["sandbox_run_context"] = RunContext(
        run_mode=RunMode.STANDARD,
        workspace="/tmp/project",
        source="saved",
    ).to_origin_payload()
    envelope.metadata["sandbox_run_context_fresh"] = True

    forged = tool_context_from_envelope(envelope, is_owner=True)
    assert getattr(forged, "_sandbox_run_context_fresh", False) is False

    object.__setattr__(envelope, "sandbox_run_context_fresh", True)
    trusted = tool_context_from_envelope(envelope, is_owner=True)
    assert getattr(trusted, "_sandbox_run_context_fresh", False) is True


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX symlinks")
@pytest.mark.asyncio
async def test_queued_turn_revalidates_project_before_tool_context(
    tmp_path: Path,
) -> None:
    async with open_stack(tmp_path / "sessions.db") as stack:
        project_path = tmp_path / "project"
        project = await add_project(stack, project_path)
        assert project is not None
        key = "agent:main:webchat:queued-retarget"
        await stack.storage.upsert_session(
            SessionNode(session_key=key, workspace_id=project.workspace_id)
        )
        envelope = build_cli_route_envelope(session_key=key, agent_id="main")
        envelope.metadata["sandbox_run_context"] = {
            "run_mode": "standard",
            "workspace": project.path,
        }
        object.__setattr__(envelope, "sandbox_run_context_fresh", True)
        run = SimpleNamespace(
            agent_id="main",
            task_id="queued-retarget-task",
            session_key=key,
            message="pwd",
            envelope=envelope,
            attachments=[],
            input_provenance={},
            run_kind="interactive",
            no_memory_capture=False,
            ingress_pipeline_steps=[],
            semantic_message=None,
            stream_event_sink=None,
        )
        replacement = tmp_path / "replacement"
        replacement.mkdir()
        project_path.rename(tmp_path / "project-old")
        project_path.symlink_to(replacement, target_is_directory=True)

        class RecordingTurnRunner:
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []

            async def run(
                self,
                message: str,
                session_key: str,
                **kwargs: Any,
            ):
                self.calls.append(kwargs)
                yield DoneEvent()

        turn_runner = RecordingTurnRunner()
        with pytest.raises(ProjectWorkspaceStateError):
            await dispatch_task_runtime_turn(
                run,
                config=stack.context.config,
                session_manager=stack.manager,
                turn_runner=turn_runner,
                event_emitter=AsyncMock(),
            )
        assert turn_runner.calls == []


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows junctions")
@pytest.mark.asyncio
async def test_queued_turn_revalidates_windows_junction_before_tool_context(
    tmp_path: Path,
) -> None:
    async with open_stack(tmp_path / "junction-sessions.db") as stack:
        project_path = tmp_path / "project"
        project = await add_project(stack, project_path)
        assert project is not None
        key = "agent:main:webchat:queued-junction-retarget"
        await stack.storage.upsert_session(
            SessionNode(session_key=key, workspace_id=project.workspace_id)
        )
        envelope = build_cli_route_envelope(session_key=key, agent_id="main")
        envelope.metadata["sandbox_run_context"] = {
            "run_mode": "standard",
            "workspace": project.path,
        }
        object.__setattr__(envelope, "sandbox_run_context_fresh", True)
        run = SimpleNamespace(
            agent_id="main",
            task_id="queued-junction-retarget-task",
            session_key=key,
            message="pwd",
            envelope=envelope,
            attachments=[],
            input_provenance={},
            run_kind="interactive",
            no_memory_capture=False,
            ingress_pipeline_steps=[],
            semantic_message=None,
            stream_event_sink=None,
        )
        replacement = tmp_path / "replacement"
        replacement.mkdir()
        original = tmp_path / "project-original"
        project_path.rename(original)
        junction_created = False
        try:
            result = create_windows_junction(project_path, replacement)
            if result.returncode != 0:
                pytest.skip(f"could not create junction: {result.stderr or result.stdout}")
            junction_created = True

            class RecordingTurnRunner:
                def __init__(self) -> None:
                    self.calls: list[dict[str, Any]] = []

                async def run(
                    self,
                    message: str,
                    session_key: str,
                    **kwargs: Any,
                ):
                    self.calls.append(kwargs)
                    yield DoneEvent()

            turn_runner = RecordingTurnRunner()
            with pytest.raises(ProjectWorkspaceStateError) as raised:
                await dispatch_task_runtime_turn(
                    run,
                    config=stack.context.config,
                    session_manager=stack.manager,
                    turn_runner=turn_runner,
                    event_emitter=AsyncMock(),
                )
            assert raised.value.reason == "canonical_changed"
            assert turn_runner.calls == []
        finally:
            if junction_created:
                os.rmdir(project_path)
            if original.exists():
                original.rename(project_path)


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX symlinks")
@pytest.mark.asyncio
async def test_direct_web_turn_revalidates_after_durable_acceptance(
    tmp_path: Path,
) -> None:
    async with open_stack(tmp_path / "sessions.db") as stack:
        project_path = tmp_path / "project"
        project = await add_project(stack, project_path)
        assert project is not None
        key = "agent:main:webchat:direct-post-accept-retarget"
        await stack.manager.create(
            key,
            workspace_id=project.workspace_id,
            origin={
                RUN_CONTEXT_ORIGIN_KEY: {
                    "run_mode": "standard",
                    "workspace": project.path,
                }
            },
        )
        calls: list[dict[str, Any]] = []

        class Runner:
            async def run(self, message: str, session_key: str, **kwargs: Any):
                calls.append(kwargs)
                yield DoneEvent()

        original_accept_turn = stack.storage.accept_turn
        replacement = tmp_path / "replacement"
        replacement.mkdir()
        retargeted = False

        async def retarget_after_accept(*args: Any, **kwargs: Any) -> Any:
            nonlocal retargeted
            acceptance = await original_accept_turn(*args, **kwargs)
            if not retargeted:
                retargeted = True
                project_path.rename(tmp_path / "project-old")
                project_path.symlink_to(replacement, target_is_directory=True)
            return acceptance

        stack.storage.accept_turn = retarget_after_accept  # type: ignore[method-assign]
        stack.context.task_runtime = None
        stack.context.turn_runner = Runner()
        response = await get_dispatcher().dispatch(
            "direct-post-accept-retarget",
            "sessions.send",
            {"key": key, "message": "pwd"},
            stack.context,
        )
        await await_direct_task(key)

        assert response.ok is True
        assert retargeted is True
        assert calls == []


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows junctions")
@pytest.mark.asyncio
async def test_direct_web_turn_revalidates_windows_junction_after_acceptance(
    tmp_path: Path,
) -> None:
    async with open_stack(tmp_path / "direct-junction-sessions.db") as stack:
        project_path = tmp_path / "project"
        project = await add_project(stack, project_path)
        assert project is not None
        key = "agent:main:webchat:direct-post-accept-junction"
        await stack.manager.create(
            key,
            workspace_id=project.workspace_id,
            origin={
                RUN_CONTEXT_ORIGIN_KEY: {
                    "run_mode": "standard",
                    "workspace": project.path,
                }
            },
        )
        calls: list[dict[str, Any]] = []

        class Runner:
            async def run(self, message: str, session_key: str, **kwargs: Any):
                calls.append(kwargs)
                yield DoneEvent()

        original_accept_turn = stack.storage.accept_turn
        replacement = tmp_path / "replacement"
        replacement.mkdir()
        original = tmp_path / "project-original"
        retargeted = False
        junction_created = False

        async def retarget_after_accept(*args: Any, **kwargs: Any) -> Any:
            nonlocal junction_created, retargeted
            acceptance = await original_accept_turn(*args, **kwargs)
            if not retargeted:
                project_path.rename(original)
                result = create_windows_junction(project_path, replacement)
                if result.returncode != 0:
                    pytest.skip(f"could not create junction: {result.stderr or result.stdout}")
                junction_created = True
                retargeted = True
            return acceptance

        stack.storage.accept_turn = retarget_after_accept  # type: ignore[method-assign]
        stack.context.task_runtime = None
        stack.context.turn_runner = Runner()
        try:
            response = await get_dispatcher().dispatch(
                "direct-post-accept-junction",
                "sessions.send",
                {"key": key, "message": "pwd"},
                stack.context,
            )
            await await_direct_task(key)

            assert response.ok is True
            assert retargeted is True
            assert calls == []
        finally:
            if junction_created:
                os.rmdir(project_path)
            if original.exists():
                original.rename(project_path)


@pytest.mark.asyncio
async def test_reset_revalidates_missing_project_after_durable_acceptance(
    tmp_path: Path,
) -> None:
    async with open_stack(tmp_path / "sessions.db") as stack:
        project_path = tmp_path / "project"
        project = await add_project(stack, project_path)
        assert project is not None
        key = "agent:main:webchat:reset-post-accept-missing"
        await stack.manager.create(
            key,
            workspace_id=project.workspace_id,
            origin={
                RUN_CONTEXT_ORIGIN_KEY: {
                    "run_mode": "standard",
                    "workspace": project.path,
                }
            },
        )
        calls: list[dict[str, Any]] = []

        class Runner:
            async def run(self, message: str, session_key: str, **kwargs: Any):
                calls.append(kwargs)
                yield DoneEvent()

        original_accept_turn = stack.storage.accept_turn
        removed = False

        async def remove_after_accept(*args: Any, **kwargs: Any) -> Any:
            nonlocal removed
            acceptance = await original_accept_turn(*args, **kwargs)
            if not removed:
                removed = True
                project_path.rmdir()
            return acceptance

        stack.storage.accept_turn = remove_after_accept  # type: ignore[method-assign]
        stack.context.task_runtime = None
        stack.context.turn_runner = Runner()
        response = await get_dispatcher().dispatch(
            "reset-post-accept-missing",
            "sessions.send",
            {
                "key": key,
                "message": "start over",
                "intent": "reset_same_key",
            },
            stack.context,
        )
        await await_direct_task(key)

        assert response.ok is True
        assert removed is True
        assert calls == []


@pytest.mark.asyncio
async def test_fork_revalidates_file_replacement_after_durable_acceptance(
    tmp_path: Path,
) -> None:
    async with open_stack(tmp_path / "sessions.db") as stack:
        project_path = tmp_path / "project"
        project = await add_project(stack, project_path)
        assert project is not None
        parent = await stack.manager.create(
            "agent:main:webchat:fork-post-accept-file",
            workspace_id=project.workspace_id,
            origin={
                RUN_CONTEXT_ORIGIN_KEY: {
                    "run_mode": "standard",
                    "workspace": project.path,
                }
            },
        )
        fork_before = await stack.manager.append_message(
            parent.session_key,
            "user",
            "fork here",
        )
        calls: list[dict[str, Any]] = []

        class Runner:
            async def run(self, message: str, session_key: str, **kwargs: Any):
                calls.append(kwargs)
                yield DoneEvent()

        original_accept_turn = stack.storage.accept_turn
        replaced = False

        async def replace_after_accept(*args: Any, **kwargs: Any) -> Any:
            nonlocal replaced
            acceptance = await original_accept_turn(*args, **kwargs)
            if not replaced:
                replaced = True
                project_path.rmdir()
                project_path.write_text("not a directory", encoding="utf-8")
            return acceptance

        stack.storage.accept_turn = replace_after_accept  # type: ignore[method-assign]
        stack.context.task_runtime = None
        stack.context.turn_runner = Runner()
        response = await get_dispatcher().dispatch(
            "fork-post-accept-file",
            "sessions.send",
            {
                "key": parent.session_key,
                "message": "forked",
                "forkBeforeMessageId": fork_before.message_id,
            },
            stack.context,
        )
        assert response.ok is True
        child_key = response.payload["sessionKey"]
        await await_direct_task(child_key)

        assert replaced is True
        assert calls == []


@pytest.mark.asyncio
async def test_bootstrap_uses_canonical_project_path_and_snapshot(
    tmp_path: Path,
) -> None:
    async with open_stack(tmp_path / "sessions.db") as stack:
        project = await add_project(stack, tmp_path / "project")
        assert project is not None
        outside = tmp_path / "outside"
        outside.mkdir()
        session = await stack.manager.create(
            "agent:main:webchat:bootstrap-authoritative",
            workspace_id=project.workspace_id,
            origin={
                RUN_CONTEXT_ORIGIN_KEY: {
                    "run_mode": "standard",
                    "workspace": str(outside),
                }
            },
        )

        response = await get_dispatcher().dispatch(
            "bootstrap-authoritative",
            "sessions.bootstrap",
            {"key": session.session_key},
            stack.context,
        )

        assert response.ok is True
        assert response.payload["session"]["workspace"] == project.path
        assert response.payload["session"]["projectWorkspace"] == {
            "id": project.workspace_id,
            "name": project.display_name,
            "path": project.path,
            "available": True,
            "removed": False,
            "availabilityReason": None,
        }


@pytest.mark.asyncio
async def test_messages_subscribe_projects_unavailable_workspace_without_failing_history(
    tmp_path: Path,
) -> None:
    async with open_stack(tmp_path / "sessions.db") as stack:
        project_path = tmp_path / "project"
        project = await add_project(stack, project_path)
        assert project is not None
        session = await stack.manager.create(
            "agent:main:webchat:subscribe-unavailable",
            workspace_id=project.workspace_id,
        )
        project_path.rmdir()

        response = await get_dispatcher().dispatch(
            "subscribe-unavailable",
            "sessions.messages.subscribe",
            {"key": session.session_key},
            stack.context,
        )

        assert response.ok is True
        assert response.payload["workspaceId"] == project.workspace_id
        assert response.payload["projectWorkspace"] == {
            "id": project.workspace_id,
            "name": project.display_name,
            "path": project.path,
            "available": False,
            "removed": False,
            "availabilityReason": "unavailable",
        }


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
        assert stack.runs[0].envelope.metadata["sandbox_run_context"]["workspace"] == project.path

        child_entries = await stack.manager.get_transcript(child_key)
        assert [entry.content for entry in child_entries] == ["A marker", "B edited"]

        listed = await get_dispatcher().dispatch(
            "project-fork-list",
            "sessions.list",
            {"limit": 50},
            stack.context,
        )
        assert listed.ok is True
        child_row = next(row for row in listed.payload["sessions"] if row["key"] == child_key)
        assert child_row["workspaceId"] == project.workspace_id
        assert child_row["workspace"] == project.path


@pytest.mark.asyncio
async def test_explicit_standard_project_drives_real_sandbox_filesystem_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if sys.platform.startswith("linux"):
        probe = BubblewrapBackend()
    elif sys.platform == "darwin":
        probe = SeatbeltBackend()
    else:
        pytest.skip("native project sandbox proof is POSIX-only")
    if not probe.available():
        pytest.skip(f"{probe.name} is unavailable")

    queue = ApprovalQueue(db_path=str(tmp_path / "approvals.sqlite"))
    try:
        async with open_stack(tmp_path / "sessions.db") as stack:
            project_path = tmp_path / "project"
            sibling = tmp_path / "sibling"
            sibling.mkdir()
            project = await add_project(stack, project_path)
            assert project is not None
            inside = project_path / "inside.txt"
            outside = sibling / "outside.txt"
            outcomes: dict[str, Any] = {}
            completed = asyncio.Event()

            runtime = configure_runtime(
                SandboxSettings(
                    run_mode="standard",
                    backend=probe.name,
                    network_default="none",
                    exclude_slash_tmp=True,
                    exclude_tmpdir_env_var=True,
                ),
                approval_queue=queue,
                workspace=project_path,
                default_run_mode=RunMode.FULL,
            )
            assert runtime.backend.name == probe.name
            native_operations: list[Any] = []
            native_run_operation = runtime.backend.run_operation

            async def counted_native_operation(operation: Any) -> Any:
                native_operations.append(operation)
                return await native_run_operation(operation)

            monkeypatch.setattr(
                runtime.backend,
                "run_operation",
                counted_native_operation,
            )

            class Runner:
                async def run(
                    self,
                    message: str,
                    session_key: str,
                    **kwargs: Any,
                ):
                    project_ctx = kwargs["tool_context"]
                    outcomes["tool_context"] = project_ctx
                    token = current_tool_context.set(project_ctx)
                    try:
                        outcomes["inside"] = await fs.write_file(
                            str(inside),
                            "inside",
                        )
                        outcomes["outside"] = json.loads(
                            await fs.write_file(str(outside), "outside")
                        )
                        outcomes["outside_after_standard"] = outside.exists()
                    except BaseException as exc:  # surfaced to the test task
                        outcomes["error"] = exc
                    else:
                        yield DoneEvent()
                    finally:
                        current_tool_context.reset(token)
                        completed.set()

            stack.context.task_runtime = None
            stack.context.turn_runner = Runner()
            response = await get_dispatcher().dispatch(
                "project-standard-proof",
                "sessions.send",
                {
                    "key": "agent:main:webchat:project-standard-proof",
                    "message": "write",
                    "intent": "new_chat",
                    "workspaceId": project.workspace_id,
                    "clientRequestId": "project-standard-proof-1",
                    "_source": {
                        "caller_kind": "web",
                        "channel_kind": "webchat",
                        "runMode": "standard",
                    },
                },
                stack.context,
            )
            await asyncio.wait_for(completed.wait(), timeout=10.0)
            if "error" in outcomes:
                raise outcomes["error"]

            assert response.ok is True
            tool_ctx = outcomes["tool_context"]
            assert tool_ctx.run_mode == "standard"
            assert tool_ctx.workspace_dir == str(project_path.resolve())
            assert full_host_access_for_context(tool_ctx) is False
            assert inside.read_text(encoding="utf-8") == "inside"
            outside_result = outcomes["outside"]
            assert outside_result["status"] == "elevation_required"
            assert outside_result["reason"] == "mount_requires_write_access"
            assert outside_result["path"] == str(outside.resolve())
            assert outside_result["access"] == "rw"
            assert outcomes["outside_after_standard"] is False
            assert [operation.kind for operation in native_operations] == ["write_text"]

            full_completed = asyncio.Event()

            class FullRunner:
                async def run(
                    self,
                    message: str,
                    session_key: str,
                    **kwargs: Any,
                ):
                    full_ctx = kwargs["tool_context"]
                    outcomes["full_tool_context"] = full_ctx
                    token = current_tool_context.set(full_ctx)
                    try:
                        await fs.write_file(str(outside), "full-host")
                    except BaseException as exc:
                        outcomes["full_error"] = exc
                    else:
                        yield DoneEvent()
                    finally:
                        current_tool_context.reset(token)
                        full_completed.set()

            stack.context.turn_runner = FullRunner()
            full_response = await get_dispatcher().dispatch(
                "ordinary-full-proof",
                "sessions.send",
                {
                    "key": "agent:main:webchat:ordinary-full-proof",
                    "message": "write",
                    "intent": "new_chat",
                    "clientRequestId": "ordinary-full-proof-1",
                },
                stack.context,
            )
            await asyncio.wait_for(full_completed.wait(), timeout=10.0)
            if "full_error" in outcomes:
                raise outcomes["full_error"]

            assert full_response.ok is True
            assert outcomes["full_tool_context"].run_mode == "full"
            assert full_host_access_for_context(outcomes["full_tool_context"]) is True
            assert outside.read_text(encoding="utf-8") == "full-host"
            assert len(native_operations) == 1
    finally:
        reset_runtime()
        queue.close()


@pytest.mark.asyncio
async def test_default_full_project_bypasses_unavailable_sandbox_backend(
    tmp_path: Path,
) -> None:
    queue = ApprovalQueue(db_path=str(tmp_path / "approvals.sqlite"))
    try:
        async with open_stack(tmp_path / "sessions.db") as stack:
            project_path = tmp_path / "project"
            sibling = tmp_path / "sibling"
            sibling.mkdir()
            project = await add_project(stack, project_path)
            assert project is not None
            outside = sibling / "outside.txt"
            outcomes: dict[str, Any] = {}
            completed = asyncio.Event()

            runtime = configure_runtime(
                SandboxSettings(
                    run_mode="standard",
                    backend="noop",
                    allow_legacy_mode=True,
                ),
                approval_queue=queue,
                workspace=project_path,
                default_run_mode=RunMode.FULL,
            )
            runtime.backend = UnavailableBackend("test unavailable")

            class Runner:
                async def run(
                    self,
                    message: str,
                    session_key: str,
                    **kwargs: Any,
                ):
                    project_ctx = kwargs["tool_context"]
                    outcomes["tool_context"] = project_ctx
                    token = current_tool_context.set(project_ctx)
                    try:
                        await fs.write_file(str(outside), "full-host")
                    except BaseException as exc:
                        outcomes["error"] = exc
                    else:
                        yield DoneEvent()
                    finally:
                        current_tool_context.reset(token)
                        completed.set()

            stack.context.task_runtime = None
            stack.context.turn_runner = Runner()
            response = await get_dispatcher().dispatch(
                "project-full-proof",
                "sessions.send",
                {
                    "key": "agent:main:webchat:project-full-proof",
                    "message": "write",
                    "intent": "new_chat",
                    "workspaceId": project.workspace_id,
                    "clientRequestId": "project-full-proof-1",
                },
                stack.context,
            )
            await asyncio.wait_for(completed.wait(), timeout=2.0)
            if "error" in outcomes:
                raise outcomes["error"]

            assert response.ok is True
            project_ctx = outcomes["tool_context"]
            assert project_ctx.run_mode == "full"
            assert project_ctx.workspace_dir == str(project_path.resolve())
            assert full_host_access_for_context(project_ctx) is True
            assert outside.read_text(encoding="utf-8") == "full-host"
    finally:
        reset_runtime()
        queue.close()


@pytest.mark.asyncio
async def test_project_standard_fails_closed_when_native_backend_is_unavailable(
    tmp_path: Path,
) -> None:
    queue = ApprovalQueue(db_path=str(tmp_path / "approvals.sqlite"))
    active_token = None
    try:
        async with open_stack(tmp_path / "sessions.db") as stack:
            project_path = tmp_path / "project"
            sibling = tmp_path / "sibling"
            sibling.mkdir()
            project = await add_project(stack, project_path)
            assert project is not None
            inside = project_path / "inside.txt"
            outside = sibling / "outside.txt"
            outcomes: dict[str, Any] = {}
            completed = asyncio.Event()

            runtime = configure_runtime(
                SandboxSettings(
                    run_mode="standard",
                    backend="noop",
                    allow_legacy_mode=True,
                ),
                approval_queue=queue,
                workspace=project_path,
                default_run_mode=RunMode.FULL,
            )
            runtime.backend = UnavailableBackend("test unavailable")

            class Runner:
                async def run(
                    self,
                    message: str,
                    session_key: str,
                    **kwargs: Any,
                ):
                    outcomes["tool_context"] = kwargs["tool_context"]
                    completed.set()
                    yield DoneEvent()

            stack.context.task_runtime = None
            stack.context.turn_runner = Runner()
            response = await get_dispatcher().dispatch(
                "project-unavailable-proof",
                "sessions.send",
                {
                    "key": "agent:main:webchat:project-unavailable-proof",
                    "message": "write",
                    "intent": "new_chat",
                    "workspaceId": project.workspace_id,
                    "clientRequestId": "project-unavailable-proof-1",
                    "_source": {
                        "caller_kind": "web",
                        "channel_kind": "webchat",
                        "runMode": "standard",
                    },
                },
                stack.context,
            )
            await asyncio.wait_for(completed.wait(), timeout=2.0)

            assert response.ok is True
            project_ctx = outcomes["tool_context"]
            assert project_ctx.run_mode == "standard"
            assert project_ctx.workspace_dir == str(project_path.resolve())
            assert full_host_access_for_context(project_ctx) is False

            active_token = current_tool_context.set(project_ctx)
            try:
                with pytest.raises(
                    SandboxBackendError,
                    match="must sandbox filesystem operations",
                ):
                    await fs.write_file(str(inside), "blocked")
            finally:
                current_tool_context.reset(active_token)
                active_token = None
            assert inside.exists() is False

            full_ctx = replace(project_ctx, run_mode="full")
            assert full_host_access_for_context(full_ctx) is True
            active_token = current_tool_context.set(full_ctx)
            try:
                await fs.write_file(str(outside), "full-host")
            finally:
                current_tool_context.reset(active_token)
                active_token = None

            assert outside.read_text(encoding="utf-8") == "full-host"
    finally:
        if active_token is not None:
            current_tool_context.reset(active_token)
        reset_runtime()
        queue.close()
