"""Real SQLite contracts for workspace failures at the acceptance boundary."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import tomli_w

from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.execution_workspaces import build_execution_workspace_factory
from opensquilla.gateway.project_workspace_runtime import map_project_workspace_error
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.gateway.task_runtime import TaskRuntime
from opensquilla.memory.source_protection import SourceMaintenancePolicy
from opensquilla.project_workspaces import ProjectWorkspaceStateError
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage

KEY = "agent:main:webchat:workspace-contract"


@dataclass
class Stack:
    root: Path
    storage: SessionStorage
    manager: SessionManager
    runtime: TaskRuntime
    context: RpcContext
    runs: list[str]

    async def counts(self) -> tuple[int, ...]:
        async with self.storage._conn.execute(
            "SELECT (SELECT count(*) FROM sessions), "
            "(SELECT count(*) FROM transcript_entries), "
            "(SELECT count(*) FROM agent_tasks), "
            "(SELECT count(*) FROM turn_ingress_receipts)"
        ) as cursor:
            return tuple(await cursor.fetchone())

    async def send(self, method: str = "chat.send", **overrides: Any) -> Any:
        return await get_dispatcher().dispatch(
            "workspace-contract",
            method,
            {
                "key": KEY,
                "sessionKey": KEY,
                "intent": "new_chat",
                "message": "One acceptance only",
                "clientRequestId": "workspace-request",
                "clientMessageId": "workspace-message",
                "_source": {"caller_kind": "web", "channel_kind": "webchat"},
                **overrides,
            },
            self.context,
        )


@asynccontextmanager
async def open_stack(tmp_path: Path) -> AsyncIterator[Stack]:
    root = tmp_path / "configured"
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        tomli_w.dumps(
            {
                "workspace_dir": str(root),
                "state_dir": str(tmp_path / "state"),
                "attachments": {"media_root": str(tmp_path / "media")},
                "sandbox": {"run_mode": "full"},
                "naming": {"enabled": False},
                "squilla_router": {"enabled": False},
                "llm_ensemble": {"enabled": False},
            }
        ),
        encoding="utf-8",
    )
    config = GatewayConfig.load(config_path, read_only=True)
    assert config.workspace_dir_source == "configured"
    assert not SourceMaintenancePolicy(config).bootstrap_allowed("main")
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    manager = SessionManager(
        storage,
        inject_time_prefix=False,
        execution_workspace_factory=build_execution_workspace_factory(
            config, profile_home=tmp_path
        ),
    )
    runs: list[str] = []

    async def handle(run: Any) -> None:
        runs.append(run.task_id)

    runtime = TaskRuntime(
        storage=storage,
        turn_handler=handle,
        max_concurrency=1,
        running_heartbeat_interval_s=None,
    )
    context = RpcContext(
        conn_id="workspace-contract",
        principal=Principal(
            role="operator", scopes=frozenset({"operator.admin"}), is_owner=True, authenticated=True
        ),
        config=config,
        session_manager=manager,
        task_runtime=runtime,
    )
    try:
        yield Stack(root, storage, manager, runtime, context, runs)
    finally:
        await runtime.shutdown(cancel=True, timeout=5)
        await storage.close()


@pytest.mark.parametrize("method", ["chat.send", "sessions.send"])
async def test_missing_configured_root_rejects_before_acceptance_then_repair_replays_once(
    tmp_path: Path,
    method: str,
) -> None:
    async with open_stack(tmp_path) as stack:
        rejected = await stack.send(method)
        assert not rejected.ok
        assert rejected.error.code == "WORKSPACE_UNAVAILABLE"
        assert rejected.error.accepted is False
        assert rejected.error.details == {"reason": "unavailable", "requestRetired": True}
        retired = await stack.send(method)
        assert not retired.ok
        assert retired.error.code == "IDEMPOTENCY_CONFLICT"
        assert retired.error.accepted is False
        assert await stack.counts() == (0, 0, 0, 0)
        assert stack.runs == []
        assert not stack.root.exists()  # No implicit protected-directory bootstrap.

        stack.root.mkdir()
        accepted = await stack.send(method, clientRequestId="fresh-after-repair")
        assert accepted.ok, accepted.error
        task_id = accepted.payload["task_id"]
        await stack.runtime.wait(task_id, timeout=10)
        committed_counts = await stack.counts()
        assert committed_counts == (1, 1, 1, 1)
        assert stack.runs == [task_id]

        stack.root.rmdir()  # Empty, owned test root only; a receipt wins over revalidation.
        replay = await stack.send(method, clientRequestId="fresh-after-repair")
        assert replay.ok, replay.error
        assert replay.payload["task_id"] == task_id
        assert replay.payload["accepted"] is True
        assert await stack.counts() == committed_counts
        assert stack.runs == [task_id]


@pytest.mark.parametrize("method", ["chat.send", "sessions.send"])
async def test_deleted_accepted_receipt_never_reports_nonacceptance(
    tmp_path: Path,
    method: str,
) -> None:
    async with open_stack(tmp_path) as stack:
        stack.root.mkdir()
        accepted = await stack.send(method)
        assert accepted.ok, accepted.error
        task_id = accepted.payload["task_id"]
        await stack.runtime.wait(task_id, timeout=10)
        await stack.storage.delete_session(KEY)

        replay = await stack.send(method)
        assert not replay.ok
        assert replay.error.code == "IDEMPOTENCY_CONFLICT"
        assert replay.error.accepted is None
        assert replay.error.retryable is False
        assert stack.runs == [task_id]
        assert await stack.counts() == (0, 0, 0, 0)
        async with stack.storage.conn.execute(
            "SELECT state FROM turn_request_resolution WHERE request_session_key=?",
            (KEY,),
        ) as cursor:
            assert [row[0] for row in await cursor.fetchall()] == ["accepted"]


@pytest.mark.parametrize("method", ["chat.send", "sessions.send"])
@pytest.mark.parametrize("missing_record", [False, True])
async def test_selected_workspace_failure_is_explicit_rejection_without_mutation(
    tmp_path: Path,
    method: str,
    missing_record: bool,
) -> None:
    async with open_stack(tmp_path) as stack:
        project_id = "does-not-exist"
        if not missing_record:
            project = tmp_path / "selected"
            project.mkdir()
            opened = await get_dispatcher().dispatch(
                "open",
                "workspaces.open",
                {"path": str(project), "trusted": True},
                stack.context,
            )
            assert opened.ok, opened.error
            project_id = opened.payload["workspace"]["id"]
            project.rmdir()
        result = await stack.send(method, workspaceId=project_id)
        assert not result.ok
        assert result.error.code == (
            "WORKSPACE_NOT_FOUND" if missing_record else "WORKSPACE_UNAVAILABLE"
        )
        assert result.error.accepted is False
        assert await stack.counts() == (0, 0, 0, 0)
        assert stack.runs == []


async def test_existing_configured_session_revalidates_before_accepting_continuation(
    tmp_path: Path,
) -> None:
    async with open_stack(tmp_path) as stack:
        stack.root.mkdir()
        await stack.manager.create(KEY)
        stack.root.rmdir()
        result = await stack.send(intent="continue")
        assert not result.ok
        assert result.error.code == "WORKSPACE_UNAVAILABLE"
        assert result.error.accepted is False
        assert await stack.counts() == (1, 0, 0, 0)
        assert stack.runs == []


async def test_unclassified_prepare_failure_keeps_acceptance_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with open_stack(tmp_path) as stack:

        async def fail(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("unclassified preparation failure")

        monkeypatch.setattr(stack.manager, "prepare_intent", fail)
        result = await stack.send()
        assert not result.ok
        assert result.error.code == "INTERNAL_ERROR"
        assert result.error.accepted is None
        assert await stack.counts() == (0, 0, 0, 0)


async def test_workspace_exception_after_commit_must_not_report_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with open_stack(tmp_path) as stack:
        stack.root.mkdir()
        original = stack.storage.accept_turn

        async def fail_after_commit(*args: Any, **kwargs: Any) -> Any:
            await original(*args, **kwargs)
            raise ProjectWorkspaceStateError("unavailable")

        monkeypatch.setattr(stack.storage, "accept_turn", fail_after_commit)
        result = await stack.send()
        assert not result.ok
        assert result.error.code == "WORKSPACE_UNAVAILABLE"
        assert result.error.accepted is None
        assert await stack.counts() == (1, 1, 1, 1)
        replay = await stack.send()
        assert replay.ok, replay.error
        assert replay.payload["accepted"] is True
        assert await stack.counts() == (1, 1, 1, 1)
        assert stack.runs == []


def test_shared_workspace_mapper_does_not_assume_preacceptance() -> None:
    error = ProjectWorkspaceStateError("unavailable")
    assert map_project_workspace_error(error, owner=True).accepted is None
    assert map_project_workspace_error(error, owner=True, accepted=False).accepted is False


async def test_concurrent_sqlite_acceptance_wins_over_a_later_workspace_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with open_stack(tmp_path) as stack:
        stack.root.mkdir()
        peer_storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
        peer_manager = SessionManager(
            peer_storage,
            execution_workspace_factory=(
                build_execution_workspace_factory(stack.context.config, profile_home=tmp_path)
            ),
        )
        peer_runs: list[str] = []

        async def peer_handle(run: Any) -> None:
            peer_runs.append(run.task_id)

        peer_runtime = TaskRuntime(
            storage=peer_storage,
            turn_handler=peer_handle,
            max_concurrency=1,
            running_heartbeat_interval_s=None,
        )
        peer = Stack(
            stack.root,
            peer_storage,
            peer_manager,
            peer_runtime,
            RpcContext(
                conn_id="independent-writer",
                principal=stack.context.principal,
                config=stack.context.config,
                session_manager=peer_manager,
                task_runtime=peer_runtime,
            ),
            peer_runs,
        )

        async def peer_commits_before_local_failure(*args: Any, **kwargs: Any) -> Any:
            accepted = await peer.send()
            assert accepted.ok, accepted.error
            await peer_runtime.wait(accepted.payload["task_id"], timeout=10)
            stack.root.rmdir()
            raise ProjectWorkspaceStateError("unavailable")

        monkeypatch.setattr(stack.manager, "prepare_intent", peer_commits_before_local_failure)
        try:
            result = await stack.send()
            assert not result.ok
            assert result.error.code == "WORKSPACE_UNAVAILABLE"
            assert result.error.accepted is None
            assert not result.error.details.get("requestRetired")
            replay = await stack.send()
            assert replay.ok, replay.error
            assert replay.payload["task_id"] == peer_runs[0]
            assert await stack.counts() == (1, 1, 1, 1)
            assert len(peer_runs) == 1
            assert stack.runs == []
        finally:
            await peer_runtime.shutdown(cancel=True, timeout=5)
            await peer_storage.close()
