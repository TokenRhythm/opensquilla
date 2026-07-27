from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc import RpcContext
from opensquilla.gateway.rpc_workspaces import (
    _handle_workspaces_list,
    _handle_workspaces_open,
)
from opensquilla.project_workspaces import (
    ProjectWorkspaceGuard,
    ProjectWorkspaceStateError,
    project_path_key,
    resolve_validated_project_workspace,
)
from opensquilla.session.models import SessionNode, TranscriptEntry
from opensquilla.session.storage import SessionStorage


@pytest_asyncio.fixture
async def workspace_ctx(tmp_path):
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    manager = SimpleNamespace(storage=storage)
    ctx = RpcContext(
        conn_id="workspace-test",
        principal=Principal(
            role="operator",
            scopes=frozenset({"operator.admin"}),
            is_owner=True,
            authenticated=True,
        ),
        session_manager=manager,
        config=GatewayConfig(),
    )
    try:
        yield ctx, storage
    finally:
        await storage.close()


def _remote_ctx(owner_ctx: RpcContext) -> RpcContext:
    return RpcContext(
        conn_id="remote",
        principal=Principal(
            role="operator",
            scopes=frozenset({"operator.read", "operator.write"}),
            is_owner=False,
            authenticated=True,
        ),
        session_manager=owner_ctx.session_manager,
        config=owner_ctx.config,
    )


async def _assert_workspace_unavailable(
    ctx: RpcContext,
    workspace_id: str,
    reason: str,
) -> None:
    listed = await _handle_workspaces_list(None, ctx)
    row = next(
        item for item in listed["workspaces"] if item["id"] == workspace_id
    )
    assert row["available"] is False
    assert row["availabilityReason"] == reason


@pytest.mark.asyncio
async def test_validated_workspace_returns_canonical_guard(
    workspace_ctx: tuple[RpcContext, SessionStorage],
    tmp_path: Path,
) -> None:
    ctx, storage = workspace_ctx
    project = tmp_path / "guarded"
    project.mkdir()
    opened = await _handle_workspaces_open(
        {"path": str(project), "trusted": True},
        ctx,
    )

    resolved = await resolve_validated_project_workspace(
        storage,
        opened["workspace"]["id"],
    )

    assert resolved.canonical_path == str(project.resolve())
    assert resolved.guard == ProjectWorkspaceGuard(
        workspace_id=opened["workspace"]["id"],
        path=str(project.resolve()),
        path_key=project_path_key(project, strict=True),
    )


@pytest.mark.asyncio
async def test_validated_workspace_rejects_not_found(
    workspace_ctx: tuple[RpcContext, SessionStorage],
) -> None:
    ctx, storage = workspace_ctx

    with pytest.raises(ProjectWorkspaceStateError) as raised:
        await resolve_validated_project_workspace(storage, "missing")

    assert raised.value.reason == "not_found"
    listed = await _handle_workspaces_list(None, ctx)
    assert all(item["id"] != "missing" for item in listed["workspaces"])


@pytest.mark.asyncio
async def test_validated_workspace_rejects_removed(
    workspace_ctx: tuple[RpcContext, SessionStorage],
    tmp_path: Path,
) -> None:
    ctx, storage = workspace_ctx
    project = tmp_path / "removed"
    project.mkdir()
    opened = await _handle_workspaces_open(
        {"path": str(project), "trusted": True},
        ctx,
    )
    workspace_id = opened["workspace"]["id"]
    await storage.remove_project_workspace(workspace_id)

    with pytest.raises(ProjectWorkspaceStateError) as raised:
        await resolve_validated_project_workspace(storage, workspace_id)

    assert raised.value.reason == "removed"
    listed = await _handle_workspaces_list(None, ctx)
    assert all(item["id"] != workspace_id for item in listed["workspaces"])


@pytest.mark.asyncio
async def test_validated_workspace_rejects_untrusted(
    workspace_ctx: tuple[RpcContext, SessionStorage],
    tmp_path: Path,
) -> None:
    ctx, storage = workspace_ctx
    project = tmp_path / "untrusted"
    project.mkdir()
    workspace = await storage.create_or_restore_project_workspace(
        path=str(project.resolve()),
        path_key=project_path_key(project, strict=True),
        display_name=project.name,
        trusted_at=None,
    )

    with pytest.raises(ProjectWorkspaceStateError) as raised:
        await resolve_validated_project_workspace(storage, workspace.workspace_id)

    assert raised.value.reason == "untrusted"
    await _assert_workspace_unavailable(ctx, workspace.workspace_id, "untrusted")


@pytest.mark.asyncio
async def test_validated_workspace_rejects_missing_directory(
    workspace_ctx: tuple[RpcContext, SessionStorage],
    tmp_path: Path,
) -> None:
    ctx, storage = workspace_ctx
    project = tmp_path / "missing"
    project.mkdir()
    opened = await _handle_workspaces_open(
        {"path": str(project), "trusted": True},
        ctx,
    )
    project.rmdir()
    workspace_id = opened["workspace"]["id"]

    with pytest.raises(ProjectWorkspaceStateError) as raised:
        await resolve_validated_project_workspace(storage, workspace_id)

    assert raised.value.reason == "unavailable"
    await _assert_workspace_unavailable(ctx, workspace_id, "unavailable")


@pytest.mark.asyncio
async def test_validated_workspace_rejects_file_path(
    workspace_ctx: tuple[RpcContext, SessionStorage],
    tmp_path: Path,
) -> None:
    ctx, storage = workspace_ctx
    project = tmp_path / "became-file"
    project.mkdir()
    opened = await _handle_workspaces_open(
        {"path": str(project), "trusted": True},
        ctx,
    )
    project.rmdir()
    project.write_text("not a directory", encoding="utf-8")
    workspace_id = opened["workspace"]["id"]

    with pytest.raises(ProjectWorkspaceStateError) as raised:
        await resolve_validated_project_workspace(storage, workspace_id)

    assert raised.value.reason == "unavailable"
    await _assert_workspace_unavailable(ctx, workspace_id, "unavailable")


@pytest.mark.asyncio
async def test_validated_workspace_normalizes_post_scan_path_key_failure(
    workspace_ctx: tuple[RpcContext, SessionStorage],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, storage = workspace_ctx
    project = tmp_path / "vanishes-after-scan"
    project.mkdir()
    opened = await _handle_workspaces_open(
        {"path": str(project), "trusted": True},
        ctx,
    )

    def fail_strict_path_key(value: str | Path, *, strict: bool = False) -> str:
        assert strict is True
        raise FileNotFoundError(value)

    monkeypatch.setattr(
        "opensquilla.project_workspaces.project_path_key",
        fail_strict_path_key,
    )

    with pytest.raises(ProjectWorkspaceStateError) as raised:
        await resolve_validated_project_workspace(
            storage,
            opened["workspace"]["id"],
        )

    assert raised.value.reason == "unavailable"


@pytest.mark.asyncio
async def test_validated_workspace_rejects_filesystem_root(
    workspace_ctx: tuple[RpcContext, SessionStorage],
    tmp_path: Path,
) -> None:
    ctx, storage = workspace_ctx
    root = Path(tmp_path.anchor)
    workspace = await storage.create_or_restore_project_workspace(
        path=str(root),
        path_key=project_path_key(root, strict=True),
        display_name="root",
        trusted_at=1,
    )

    with pytest.raises(ProjectWorkspaceStateError) as raised:
        await resolve_validated_project_workspace(storage, workspace.workspace_id)

    assert raised.value.reason == "unavailable"
    await _assert_workspace_unavailable(ctx, workspace.workspace_id, "unavailable")


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX directory permissions")
@pytest.mark.asyncio
async def test_validated_workspace_rejects_posix_inaccessible_directory(
    workspace_ctx: tuple[RpcContext, SessionStorage],
    tmp_path: Path,
) -> None:
    ctx, storage = workspace_ctx
    project = tmp_path / "inaccessible"
    project.mkdir()
    opened = await _handle_workspaces_open(
        {"path": str(project), "trusted": True},
        ctx,
    )
    workspace_id = opened["workspace"]["id"]
    project.chmod(0)
    try:
        with pytest.raises(ProjectWorkspaceStateError) as raised:
            await resolve_validated_project_workspace(storage, workspace_id)
        assert raised.value.reason == "unavailable"
        await _assert_workspace_unavailable(ctx, workspace_id, "unavailable")
    finally:
        project.chmod(0o700)


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX symlinks")
@pytest.mark.asyncio
async def test_validated_workspace_rejects_symlink_retarget(
    workspace_ctx: tuple[RpcContext, SessionStorage],
    tmp_path: Path,
) -> None:
    ctx, storage = workspace_ctx
    trusted = tmp_path / "trusted"
    replacement = tmp_path / "replacement"
    trusted.mkdir()
    replacement.mkdir()
    opened = await _handle_workspaces_open(
        {"path": str(trusted), "trusted": True},
        ctx,
    )
    moved = tmp_path / "trusted-old"
    trusted.rename(moved)
    trusted.symlink_to(replacement, target_is_directory=True)

    with pytest.raises(ProjectWorkspaceStateError) as raised:
        await resolve_validated_project_workspace(
            storage,
            opened["workspace"]["id"],
        )
    assert raised.value.reason == "canonical_changed"


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX symlinks")
@pytest.mark.asyncio
async def test_workspace_payload_uses_strict_validator_for_availability(
    workspace_ctx: tuple[RpcContext, SessionStorage],
    tmp_path: Path,
) -> None:
    ctx, _storage = workspace_ctx
    trusted = tmp_path / "trusted"
    replacement = tmp_path / "replacement"
    trusted.mkdir()
    replacement.mkdir()
    opened = await _handle_workspaces_open(
        {"path": str(trusted), "trusted": True},
        ctx,
    )
    trusted.rename(tmp_path / "trusted-old")
    trusted.symlink_to(replacement, target_is_directory=True)
    listed = await _handle_workspaces_list(None, ctx)
    row = next(
        item
        for item in listed["workspaces"]
        if item["id"] == opened["workspace"]["id"]
    )
    assert row["available"] is False
    assert row["availabilityReason"] == "canonical_changed"


@pytest.mark.skipif(sys.platform != "win32", reason="requires a Windows junction")
@pytest.mark.asyncio
async def test_validated_workspace_rejects_windows_junction_retarget(
    workspace_ctx: tuple[RpcContext, SessionStorage],
    tmp_path: Path,
) -> None:
    ctx, storage = workspace_ctx
    trusted = tmp_path / "trusted"
    replacement = tmp_path / "replacement"
    trusted.mkdir()
    replacement.mkdir()
    opened = await _handle_workspaces_open(
        {"path": str(trusted), "trusted": True},
        ctx,
    )
    trusted.rename(tmp_path / "trusted-old")
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(trusted), str(replacement)],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"junction creation unavailable: {result.stderr or result.stdout}")
    workspace_id = opened["workspace"]["id"]

    with pytest.raises(ProjectWorkspaceStateError) as raised:
        await resolve_validated_project_workspace(storage, workspace_id)

    assert raised.value.reason == "canonical_changed"
    await _assert_workspace_unavailable(ctx, workspace_id, "canonical_changed")


@pytest.mark.asyncio
async def test_open_lists_empty_workspace_and_normalizes_duplicates(
    workspace_ctx, tmp_path
) -> None:
    from opensquilla.gateway import rpc_workspaces

    ctx, _storage = workspace_ctx
    project = tmp_path / "demo"
    project.mkdir()

    opened = await rpc_workspaces._handle_workspaces_open(
        {"path": str(project / "."), "trusted": True}, ctx
    )
    duplicate = await rpc_workspaces._handle_workspaces_open(
        {"path": str(project), "trusted": True}, ctx
    )
    listed = await rpc_workspaces._handle_workspaces_list(None, ctx)

    assert duplicate["workspace"]["id"] == opened["workspace"]["id"]
    assert listed == {
        "workspaces": [
            {
                "id": opened["workspace"]["id"],
                "name": "demo",
                "path": str(project.resolve()),
                "taskCount": 0,
                "pinned": False,
                "available": True,
            }
        ]
    }


@pytest.mark.asyncio
async def test_open_rejects_untrusted_missing_file_and_root(
    workspace_ctx, tmp_path
) -> None:
    from opensquilla.gateway import rpc_workspaces
    from opensquilla.gateway.rpc import RpcHandlerError

    ctx, _storage = workspace_ctx
    file_path = tmp_path / "file.txt"
    file_path.write_text("x", encoding="utf-8")

    for params in (
        {"path": str(tmp_path), "trusted": False},
        {"path": str(tmp_path / "missing"), "trusted": True},
        {"path": str(file_path), "trusted": True},
        {"path": str(tmp_path.anchor), "trusted": True},
    ):
        with pytest.raises(RpcHandlerError):
            await rpc_workspaces._handle_workspaces_open(params, ctx)


@pytest.mark.asyncio
async def test_workspace_mutations_preserve_fixed_project_order(
    workspace_ctx, tmp_path
) -> None:
    from opensquilla.gateway import rpc_workspaces

    ctx, _storage = workspace_ctx
    first_path = tmp_path / "first"
    second_path = tmp_path / "second"
    first_path.mkdir()
    second_path.mkdir()
    first = (
        await rpc_workspaces._handle_workspaces_open(
            {"path": str(first_path), "trusted": True}, ctx
        )
    )["workspace"]
    second = (
        await rpc_workspaces._handle_workspaces_open(
            {"path": str(second_path), "trusted": True}, ctx
        )
    )["workspace"]

    await rpc_workspaces._handle_workspaces_update(
        {"workspaceId": first["id"], "name": "renamed"}, ctx
    )
    before_pin = await rpc_workspaces._handle_workspaces_list(None, ctx)
    assert [row["id"] for row in before_pin["workspaces"]] == [
        second["id"],
        first["id"],
    ]

    await rpc_workspaces._handle_workspaces_pin(
        {"workspaceId": first["id"], "pinned": True}, ctx
    )
    await rpc_workspaces._handle_workspaces_pin(
        {"workspaceId": second["id"], "pinned": True}, ctx
    )
    pinned = await rpc_workspaces._handle_workspaces_list(None, ctx)
    assert [row["id"] for row in pinned["workspaces"]] == [
        second["id"],
        first["id"],
    ]

    await rpc_workspaces._handle_workspaces_pin(
        {"workspaceId": second["id"], "pinned": False}, ctx
    )
    unpinned = await rpc_workspaces._handle_workspaces_list(None, ctx)
    assert [row["id"] for row in unpinned["workspaces"]] == [
        first["id"],
        second["id"],
    ]


@pytest.mark.asyncio
async def test_remove_restores_identity_and_history_delete_keeps_project(
    workspace_ctx, tmp_path
) -> None:
    from opensquilla.gateway import rpc_workspaces

    ctx, storage = workspace_ctx
    project = tmp_path / "history"
    project.mkdir()
    opened = (
        await rpc_workspaces._handle_workspaces_open(
            {"path": str(project), "trusted": True}, ctx
        )
    )["workspace"]
    await storage.upsert_session(
        SessionNode(
            session_key="agent:main:webchat:project-history",
            workspace_id=opened["id"],
        )
    )

    deleted = await rpc_workspaces._handle_workspaces_history_delete(
        {"workspaceId": opened["id"]}, ctx
    )
    assert deleted["deletedTaskCount"] == 1
    assert deleted["deletedSessionKeys"] == [
        "agent:main:webchat:project-history"
    ]
    assert (await rpc_workspaces._handle_workspaces_list(None, ctx))[
        "workspaces"
    ][0]["taskCount"] == 0

    await rpc_workspaces._handle_workspaces_remove(
        {"workspaceId": opened["id"]}, ctx
    )
    assert await rpc_workspaces._handle_workspaces_list(None, ctx) == {
        "workspaces": []
    }
    restored = await rpc_workspaces._handle_workspaces_open(
        {"path": str(project), "trusted": True}, ctx
    )
    assert restored["workspace"]["id"] == opened["id"]


@pytest.mark.asyncio
async def test_history_delete_maps_transactional_missing_to_workspace_not_found(
    workspace_ctx,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.gateway import rpc_workspaces
    from opensquilla.gateway.rpc import RpcHandlerError

    ctx, storage = workspace_ctx

    async def forbidden_precheck(_workspace_id: str) -> None:
        raise AssertionError("history deletion must not precheck workspace state")

    async def empty_snapshot(_workspace_id: str) -> list[str]:
        return []

    async def missing_in_transaction(
        _workspace_id: str,
        *,
        expected_session_keys: list[str] | None = None,
    ) -> list[str]:
        assert expected_session_keys == []
        raise KeyError("Project workspace not found")

    monkeypatch.setattr(storage, "get_project_workspace", forbidden_precheck)
    monkeypatch.setattr(
        storage,
        "list_project_workspace_session_keys",
        empty_snapshot,
    )
    monkeypatch.setattr(
        storage,
        "delete_project_workspace_sessions",
        missing_in_transaction,
    )

    with pytest.raises(RpcHandlerError) as raised:
        await rpc_workspaces._handle_workspaces_history_delete(
            {"workspaceId": "missing"},
            ctx,
        )
    assert raised.value.code == "WORKSPACE_NOT_FOUND"


@pytest.mark.asyncio
async def test_history_delete_maps_remove_race_to_workspace_not_found(
    workspace_ctx,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.gateway import rpc_workspaces
    from opensquilla.gateway.rpc import RpcHandlerError

    ctx, storage = workspace_ctx
    project_path = tmp_path / "remove-race"
    project_path.mkdir()
    opened = (
        await rpc_workspaces._handle_workspaces_open(
            {"path": str(project_path), "trusted": True},
            ctx,
        )
    )["workspace"]
    await storage.upsert_session(
        SessionNode(
            session_key="agent:main:webchat:remove-race",
            workspace_id=opened["id"],
        )
    )
    removing_storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        delete_called = asyncio.Event()
        release_delete = asyncio.Event()
        original_delete = storage.delete_project_workspace_sessions

        async def paused_delete(
            workspace_id: str,
            *,
            expected_session_keys: list[str] | None = None,
        ) -> list[str]:
            delete_called.set()
            await release_delete.wait()
            return await original_delete(
                workspace_id,
                expected_session_keys=expected_session_keys,
            )

        monkeypatch.setattr(
            storage,
            "delete_project_workspace_sessions",
            paused_delete,
        )
        deleting = asyncio.create_task(
            rpc_workspaces._handle_workspaces_history_delete(
                {"workspaceId": opened["id"]},
                ctx,
            )
        )
        await asyncio.wait_for(delete_called.wait(), timeout=2)
        await removing_storage.remove_project_workspace(opened["id"])
        release_delete.set()

        with pytest.raises(RpcHandlerError) as raised:
            await deleting
        assert raised.value.code == "WORKSPACE_NOT_FOUND"
        assert await storage.get_session("agent:main:webchat:remove-race") is not None
    finally:
        await removing_storage.close()


@pytest.mark.asyncio
async def test_history_delete_maps_removed_workspace_to_not_found(
    workspace_ctx,
    tmp_path: Path,
) -> None:
    from opensquilla.gateway import rpc_workspaces
    from opensquilla.gateway.rpc import RpcHandlerError

    ctx, storage = workspace_ctx
    project_path = tmp_path / "removed-history"
    project_path.mkdir()
    opened = (
        await rpc_workspaces._handle_workspaces_open(
            {"path": str(project_path), "trusted": True},
            ctx,
        )
    )["workspace"]
    await storage.remove_project_workspace(opened["id"])

    with pytest.raises(RpcHandlerError) as raised:
        await rpc_workspaces._handle_workspaces_history_delete(
            {"workspaceId": opened["id"]},
            ctx,
        )
    assert raised.value.code == "WORKSPACE_NOT_FOUND"


@pytest.mark.asyncio
async def test_history_delete_retries_changed_snapshot(
    workspace_ctx,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.gateway import rpc_workspaces

    ctx, storage = workspace_ctx
    project_path = tmp_path / "snapshot-retry"
    project_path.mkdir()
    opened = (
        await rpc_workspaces._handle_workspaces_open(
            {"path": str(project_path), "trusted": True},
            ctx,
        )
    )["workspace"]
    old = SessionNode(
        session_key="agent:main:webchat:snapshot-old",
        workspace_id=opened["id"],
        created_at=100,
        updated_at=100,
    )
    new = SessionNode(
        session_key="agent:main:webchat:snapshot-new",
        workspace_id=opened["id"],
        created_at=200,
        updated_at=200,
    )
    await storage.upsert_session(old)

    acquisition_counts: dict[str, int] = {}
    locks: dict[str, asyncio.Lock] = {}

    class CountingLock(asyncio.Lock):
        def __init__(self, session_key: str) -> None:
            super().__init__()
            self.session_key = session_key

        async def acquire(self) -> bool:
            acquisition_counts[self.session_key] = (
                acquisition_counts.get(self.session_key, 0) + 1
            )
            return await super().acquire()

    def get_lock(session_key: str) -> asyncio.Lock:
        return locks.setdefault(session_key, CountingLock(session_key))

    ctx.turn_runner = SimpleNamespace(get_session_lock=get_lock)
    snapshots: list[list[str] | None] = []
    original_delete = storage.delete_project_workspace_sessions

    async def insert_between_snapshot_and_transaction(
        workspace_id: str,
        *,
        expected_session_keys: list[str] | None = None,
    ) -> list[str]:
        snapshots.append(
            list(expected_session_keys)
            if expected_session_keys is not None
            else None
        )
        if len(snapshots) == 1:
            await storage.upsert_session(new)
        return await original_delete(
            workspace_id,
            expected_session_keys=expected_session_keys,
        )

    monkeypatch.setattr(
        storage,
        "delete_project_workspace_sessions",
        insert_between_snapshot_and_transaction,
    )

    deleted = await asyncio.wait_for(
        rpc_workspaces._handle_workspaces_history_delete(
            {"workspaceId": opened["id"]},
            ctx,
        ),
        timeout=2,
    )

    assert snapshots == [
        [old.session_key],
        [old.session_key, new.session_key],
    ]
    assert deleted["deletedSessionKeys"] == [old.session_key, new.session_key]
    assert acquisition_counts == {
        old.session_key: 2,
        new.session_key: 1,
    }
    assert all(lock.locked() is False for lock in locks.values())


@pytest.mark.asyncio
async def test_history_delete_holds_sorted_session_locks_through_all_cleanup(
    workspace_ctx,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.gateway import rpc_workspaces

    ctx, storage = workspace_ctx
    project_path = tmp_path / "locked-cleanup"
    project_path.mkdir()
    sentinel = project_path / "keep-me.txt"
    sentinel.write_text("project data", encoding="utf-8")
    opened = (
        await rpc_workspaces._handle_workspaces_open(
            {"path": str(project_path), "trusted": True},
            ctx,
        )
    )["workspace"]
    root = SessionNode(
        session_key="agent:main:webchat:history-z",
        workspace_id=opened["id"],
        created_at=100,
        updated_at=100,
    )
    child = SessionNode(
        session_key="agent:main:webchat:history-a",
        workspace_id=opened["id"],
        created_at=200,
        updated_at=200,
        spawn_depth=1,
        spawned_by=root.session_key,
        parent_session_key=root.session_key,
    )
    await storage.upsert_session(root)
    await storage.upsert_session(child)

    acquisition_order: list[str] = []

    class RecordingLock(asyncio.Lock):
        def __init__(self, session_key: str) -> None:
            super().__init__()
            self.session_key = session_key

        async def acquire(self) -> bool:
            acquisition_order.append(self.session_key)
            return await super().acquire()

    locks: dict[str, RecordingLock] = {}

    def get_lock(session_key: str) -> RecordingLock:
        return locks.setdefault(session_key, RecordingLock(session_key))

    ctx.turn_runner = SimpleNamespace(get_session_lock=get_lock)
    cleanup_entered = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_calls: list[str] = []

    async def cleanup(session: SessionNode) -> None:
        cleanup_calls.append(session.session_key)
        if len(cleanup_calls) == 1:
            cleanup_entered.set()
            await release_cleanup.wait()
            raise RuntimeError("injected first cleanup failure")

    monkeypatch.setattr(storage, "_cleanup_deleted_session", cleanup)
    deleting = asyncio.create_task(
        rpc_workspaces._handle_workspaces_history_delete(
            {"workspaceId": opened["id"]},
            ctx,
        )
    )
    await asyncio.wait_for(cleanup_entered.wait(), timeout=2)
    locked_during_cleanup = all(lock.locked() for lock in locks.values())
    release_cleanup.set()
    deleted = await deleting

    assert locked_during_cleanup is True
    assert acquisition_order == sorted([root.session_key, child.session_key])
    assert cleanup_calls == [root.session_key, child.session_key]
    assert deleted["deletedSessionKeys"] == [root.session_key, child.session_key]
    assert deleted["deletedTaskCount"] == len(deleted["deletedSessionKeys"]) == 2
    assert all(lock.locked() is False for lock in locks.values())
    assert sentinel.read_text(encoding="utf-8") == "project data"


@pytest.mark.asyncio
async def test_history_delete_waits_for_accepted_turn_runtime_lock(
    workspace_ctx,
    tmp_path: Path,
) -> None:
    from opensquilla.gateway import rpc_workspaces

    ctx, storage = workspace_ctx
    project_path = tmp_path / "accepted-turn"
    project_path.mkdir()
    opened = (
        await rpc_workspaces._handle_workspaces_open(
            {"path": str(project_path), "trusted": True},
            ctx,
        )
    )["workspace"]
    workspace = await storage.get_project_workspace(opened["id"])
    assert workspace is not None
    session = SessionNode(
        session_key="agent:main:webchat:accepted-before-delete",
        workspace_id=opened["id"],
        created_at=100,
        updated_at=100,
    )
    await storage.upsert_session(session)
    guard = ProjectWorkspaceGuard(
        workspace.workspace_id,
        workspace.path,
        workspace.path_key,
    )

    delete_lock_attempted = asyncio.Event()

    class ObservedLock(asyncio.Lock):
        async def acquire(self) -> bool:
            if self.locked():
                delete_lock_attempted.set()
            return await super().acquire()

    locks: dict[str, ObservedLock] = {}

    def get_lock(session_key: str) -> ObservedLock:
        return locks.setdefault(session_key, ObservedLock())

    ctx.turn_runner = SimpleNamespace(get_session_lock=get_lock)
    turn_committed = asyncio.Event()
    release_turn = asyncio.Event()

    async def accepted_turn() -> None:
        async with get_lock(session.session_key):
            await storage.accept_turn(
                TranscriptEntry(
                    session_id=session.session_id,
                    session_key=session.session_key,
                    message_id="accepted-before-delete-message",
                    role="user",
                    content="accepted history",
                    created_at=200,
                ),
                expected_epoch=0,
                updated_at=200,
                task_record=None,
                source_scope="web:test",
                request_session_key=session.session_key,
                client_request_id="accepted-before-delete-request",
                request_fingerprint="sha256:accepted-before-delete-request",
                workspace_guard=guard,
            )
            turn_committed.set()
            await release_turn.wait()

    accepting = asyncio.create_task(accepted_turn())
    await asyncio.wait_for(turn_committed.wait(), timeout=2)
    deleting = asyncio.create_task(
        rpc_workspaces._handle_workspaces_history_delete(
            {"workspaceId": opened["id"]},
            ctx,
        )
    )
    await asyncio.wait_for(delete_lock_attempted.wait(), timeout=2)
    assert deleting.done() is False
    release_turn.set()
    await accepting
    deleted = await deleting

    assert deleted["deletedSessionKeys"] == [session.session_key]
    assert await storage.get_session(session.session_key) is None
    assert await storage.get_transcript(session.session_id) == []
    assert (
        await storage.get_turn_ingress_receipt(
            source_scope="web:test",
            request_session_key=session.session_key,
            client_request_id="accepted-before-delete-request",
        )
        is None
    )


@pytest.mark.asyncio
async def test_history_delete_orders_all_fences_drains_and_identity_eviction(
    workspace_ctx,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.gateway import rpc_workspaces

    ctx, storage = workspace_ctx
    project_path = tmp_path / "ordered-history-fences"
    project_path.mkdir()
    opened = (
        await rpc_workspaces._handle_workspaces_open(
            {"path": str(project_path), "trusted": True},
            ctx,
        )
    )["workspace"]
    sessions = [
        SessionNode(
            session_key="agent:main:webchat:ordered-history-z",
            session_id="ordered-generation-z",
            workspace_id=opened["id"],
            created_at=100,
            updated_at=100,
        ),
        SessionNode(
            session_key="agent:main:webchat:ordered-history-a",
            session_id="ordered-generation-a",
            workspace_id=opened["id"],
            created_at=100,
            updated_at=100,
        ),
    ]
    for session in sessions:
        await storage.upsert_session(session)

    order: list[str] = []
    active_fences: set[str] = set()

    @asynccontextmanager
    async def fence(name: str) -> AsyncIterator[None]:
        order.append(f"{name}:enter")
        active_fences.add(name)
        try:
            yield
        finally:
            active_fences.remove(name)
            order.append(f"{name}:exit")

    @asynccontextmanager
    async def background_fence(keys: list[str]) -> AsyncIterator[None]:
        assert keys == sorted(session.session_key for session in sessions)
        async with fence("background"):
            yield

    @asynccontextmanager
    async def runtime_fence(keys: list[str]) -> AsyncIterator[None]:
        assert keys == sorted(session.session_key for session in sessions)
        async with fence("runtime"):
            yield

    @asynccontextmanager
    async def direct_fence(keys: list[str]) -> AsyncIterator[None]:
        assert keys == sorted(session.session_key for session in sessions)
        async with fence("direct"):
            yield

    class RecordingLock(asyncio.Lock):
        def __init__(self, session_key: str) -> None:
            super().__init__()
            self.session_key = session_key

        async def acquire(self) -> bool:
            acquired = await super().acquire()
            active_fences.add(f"lock:{self.session_key}")
            order.append(f"lock:{self.session_key}:enter")
            return acquired

        def release(self) -> None:
            active_fences.remove(f"lock:{self.session_key}")
            order.append(f"lock:{self.session_key}:exit")
            super().release()

    locks = {
        session.session_key: RecordingLock(session.session_key)
        for session in sessions
    }

    async def drain_turn_runner(keys: list[str]) -> None:
        assert keys == sorted(session.session_key for session in sessions)
        assert {"background", "runtime", "direct"} <= active_fences
        assert all(lock.locked() for lock in locks.values())
        order.append("turn-drain")

    async def drain_router(keys: list[str]) -> None:
        assert keys == sorted(session.session_key for session in sessions)
        assert {"background", "runtime", "direct"} <= active_fences
        assert all(lock.locked() for lock in locks.values())
        order.append("router-drain")

    ctx.task_runtime = SimpleNamespace(quiesce_sessions=runtime_fence)
    ctx.turn_runner = SimpleNamespace(
        get_session_lock=locks.__getitem__,
        drain_session_background_writes=drain_turn_runner,
    )
    evicted: list[tuple[str, str | None]] = []

    def evict_runtime_state(
        session_key: str,
        *,
        session_id: str | None = None,
    ) -> None:
        assert {"background", "runtime", "direct"} <= active_fences
        assert all(lock.locked() for lock in locks.values())
        evicted.append((session_key, session_id))
        order.append(f"evict:{session_key}")

    ctx.session_manager.evict_session_runtime_state = evict_runtime_state
    registry = SimpleNamespace(quiesce_sessions=direct_fence)
    original_delete = storage.delete_project_workspace_sessions

    async def observed_delete(
        workspace_id: str,
        *,
        expected_session_keys: list[str] | None = None,
    ) -> list[str]:
        assert {"background", "runtime", "direct"} <= active_fences
        assert all(lock.locked() for lock in locks.values())
        order.append("delete")
        return await original_delete(
            workspace_id,
            expected_session_keys=expected_session_keys,
        )

    monkeypatch.setattr(
        rpc_workspaces,
        "quiesce_background_completion_sessions",
        background_fence,
        raising=False,
    )
    monkeypatch.setattr(
        rpc_workspaces,
        "get_agent_task_registry",
        lambda: registry,
        raising=False,
    )
    monkeypatch.setattr(
        rpc_workspaces,
        "drain_pending_flushes_for_sessions",
        drain_router,
        raising=False,
    )
    monkeypatch.setattr(
        storage,
        "delete_project_workspace_sessions",
        observed_delete,
    )

    result = await rpc_workspaces._handle_workspaces_history_delete(
        {"workspaceId": opened["id"]},
        ctx,
    )

    sorted_keys = sorted(session.session_key for session in sessions)
    assert result["deletedSessionKeys"] == sorted_keys
    assert evicted == [
        (session.session_key, session.session_id)
        for session in sorted(sessions, key=lambda item: item.session_key)
    ]
    assert order.index("background:enter") < order.index("runtime:enter")
    assert order.index("runtime:enter") < order.index("direct:enter")
    assert order.index("router-drain") < order.index("delete")
    assert order.index("turn-drain") < order.index("delete")
    assert max(order.index(f"evict:{key}") for key in sorted_keys) < order.index(
        "direct:exit"
    )
    assert active_fences == set()


@pytest.mark.asyncio
async def test_history_delete_repeated_cancellation_waits_for_whole_fenced_operation(
    workspace_ctx,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.gateway import rpc_workspaces

    ctx, storage = workspace_ctx
    project_path = tmp_path / "cancelled-history-fences"
    project_path.mkdir()
    opened = (
        await rpc_workspaces._handle_workspaces_open(
            {"path": str(project_path), "trusted": True},
            ctx,
        )
    )["workspace"]
    session = SessionNode(
        session_key="agent:main:webchat:cancelled-history",
        session_id="cancelled-history-generation",
        workspace_id=opened["id"],
    )
    await storage.upsert_session(session)
    delete_started = asyncio.Event()
    release_delete = asyncio.Event()
    delete_settled = asyncio.Event()
    active_fences: set[str] = set()

    @asynccontextmanager
    async def tracked_fence(name: str) -> AsyncIterator[None]:
        active_fences.add(name)
        try:
            yield
        finally:
            active_fences.remove(name)

    @asynccontextmanager
    async def background_fence(_keys: list[str]) -> AsyncIterator[None]:
        async with tracked_fence("background"):
            yield

    @asynccontextmanager
    async def runtime_fence(_keys: list[str]) -> AsyncIterator[None]:
        async with tracked_fence("runtime"):
            yield

    @asynccontextmanager
    async def direct_fence(_keys: list[str]) -> AsyncIterator[None]:
        async with tracked_fence("direct"):
            yield

    class TrackedLock(asyncio.Lock):
        async def acquire(self) -> bool:
            result = await super().acquire()
            active_fences.add("short-lock")
            return result

        def release(self) -> None:
            active_fences.remove("short-lock")
            super().release()

    lock = TrackedLock()
    ctx.task_runtime = SimpleNamespace(quiesce_sessions=runtime_fence)
    ctx.turn_runner = SimpleNamespace(
        get_session_lock=lambda _key: lock,
        drain_session_background_writes=AsyncMock(return_value=None),
    )
    ctx.session_manager.evict_session_runtime_state = lambda *_args, **_kwargs: None
    registry = SimpleNamespace(quiesce_sessions=direct_fence)

    async def blocked_delete(
        _workspace_id: str,
        *,
        expected_session_keys: list[str] | None = None,
    ) -> list[str]:
        assert expected_session_keys == [session.session_key]
        delete_started.set()
        await release_delete.wait()
        delete_settled.set()
        return [session.session_key]

    monkeypatch.setattr(
        rpc_workspaces,
        "quiesce_background_completion_sessions",
        background_fence,
        raising=False,
    )
    monkeypatch.setattr(
        rpc_workspaces,
        "get_agent_task_registry",
        lambda: registry,
        raising=False,
    )
    monkeypatch.setattr(
        rpc_workspaces,
        "drain_pending_flushes_for_sessions",
        AsyncMock(return_value=None),
        raising=False,
    )
    monkeypatch.setattr(
        storage,
        "delete_project_workspace_sessions",
        blocked_delete,
    )

    deleting = asyncio.create_task(
        rpc_workspaces._handle_workspaces_history_delete(
            {"workspaceId": opened["id"]},
            ctx,
        )
    )
    try:
        await asyncio.wait_for(delete_started.wait(), timeout=1.0)
        assert active_fences == {
            "background",
            "runtime",
            "direct",
            "short-lock",
        }

        deleting.cancel()
        await asyncio.sleep(0)
        assert deleting.done() is False
        deleting.cancel()
        await asyncio.sleep(0)
        assert deleting.done() is False
        assert delete_settled.is_set() is False
        assert active_fences == {
            "background",
            "runtime",
            "direct",
            "short-lock",
        }

        release_delete.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(deleting, timeout=1.0)
        assert delete_settled.is_set() is True
        assert active_fences == set()
    finally:
        release_delete.set()
        if not deleting.done():
            deleting.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await deleting


@pytest.mark.asyncio
async def test_history_delete_real_quiescers_leave_no_late_rows_after_cancellation(
    workspace_ctx,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.gateway import rpc_workspaces
    from opensquilla.gateway.agent_tasks import get_agent_task_registry
    from opensquilla.gateway.background_completion import BackgroundCompletionManager
    from opensquilla.gateway.routing import RouteEnvelope, SourceKind
    from opensquilla.gateway.subagent_announce import (
        set_background_completion_manager,
    )
    from opensquilla.gateway.task_runtime import TaskRuntime
    from opensquilla.session.manager import SessionManager

    ctx, storage = workspace_ctx
    manager = SessionManager(storage, inject_time_prefix=False)
    ctx.session_manager = manager
    project_path = tmp_path / "real-history-quiescers"
    project_path.mkdir()
    opened = (
        await rpc_workspaces._handle_workspaces_open(
            {"path": str(project_path), "trusted": True},
            ctx,
        )
    )["workspace"]
    root = SessionNode(
        session_key="agent:main:webchat:real-history-root",
        session_id="real-history-root-generation",
        agent_id="main",
        workspace_id=opened["id"],
        created_at=100,
        updated_at=100,
    )
    child = SessionNode(
        session_key="agent:main:webchat:real-history-child",
        session_id="real-history-child-generation",
        agent_id="main",
        workspace_id=opened["id"],
        parent_session_key=root.session_key,
        spawned_by=root.session_key,
        spawn_depth=1,
        created_at=200,
        updated_at=200,
    )
    await storage.upsert_session(root)
    await storage.upsert_session(child)

    runtime_started = asyncio.Event()
    direct_started = asyncio.Event()
    watcher_started = asyncio.Event()
    runtime_finalized = asyncio.Event()
    direct_finalized = asyncio.Event()
    watcher_finalized = asyncio.Event()

    async def runtime_handler(_run: Any) -> None:
        runtime_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await manager.append_message(
                root.session_key,
                role="system",
                content="runtime cancellation tail",
            )
            runtime_finalized.set()

    runtime = TaskRuntime(
        storage=storage,
        turn_handler=runtime_handler,
        running_heartbeat_interval_s=None,
    )
    manager.attach_task_runtime(runtime)

    async def no_turn_background_writes(_keys: list[str]) -> None:
        return

    ctx.task_runtime = runtime
    ctx.turn_runner = SimpleNamespace(
        get_session_lock=runtime._get_session_lock_for_turn,
        drain_session_background_writes=no_turn_background_writes,
    )
    runtime_handle = await runtime.enqueue(
        RouteEnvelope(
            source_kind=SourceKind.WEB,
            source_name="history-quiesce-test",
            agent_id="main",
            session_key=root.session_key,
            input_provenance={"kind": "test"},
        ),
        "running project turn",
    )
    await asyncio.wait_for(runtime_started.wait(), timeout=1.0)

    registry = get_agent_task_registry()

    async def direct_task_body() -> None:
        direct_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await manager.append_message(
                child.session_key,
                role="system",
                content="direct cancellation tail",
            )
            direct_finalized.set()

    direct_task = asyncio.create_task(direct_task_body())
    registry.register(child.session_key, direct_task)
    await asyncio.wait_for(direct_started.wait(), timeout=1.0)

    completion = BackgroundCompletionManager(session_manager=manager)
    set_background_completion_manager(completion)
    group_id = completion.group_id(root.session_key, runtime_handle.task_id)

    async def watcher_body() -> None:
        watcher_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await manager.append_message(
                root.session_key,
                role="system",
                content="background watcher cancellation tail",
            )
            watcher_finalized.set()

    watcher = asyncio.create_task(watcher_body())
    async with completion._state_lock:
        completion._watch_tasks.add(watcher)
        completion._watch_task_owners[watcher] = (root.session_key, group_id)
        completion._group_parents[group_id] = root.session_key
        completion._wake_groups.add(group_id)
    watcher.add_done_callback(completion._discard_watch_task)
    await asyncio.wait_for(watcher_started.wait(), timeout=1.0)

    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_calls: list[str] = []

    async def blocked_cleanup(node: SessionNode) -> None:
        cleanup_calls.append(node.session_key)
        if len(cleanup_calls) == 1:
            cleanup_started.set()
            await release_cleanup.wait()

    monkeypatch.setattr(storage, "_cleanup_deleted_session", blocked_cleanup)
    deleting = asyncio.create_task(
        rpc_workspaces._handle_workspaces_history_delete(
            {"workspaceId": opened["id"]},
            ctx,
        )
    )
    try:
        await asyncio.wait_for(cleanup_started.wait(), timeout=2.0)
        assert runtime_finalized.is_set()
        assert direct_finalized.is_set()
        assert watcher_finalized.is_set()

        deleting.cancel()
        await asyncio.sleep(0)
        assert deleting.done() is False
        deleting.cancel()
        await asyncio.sleep(0)
        assert deleting.done() is False

        release_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(deleting, timeout=2.0)
        await asyncio.sleep(0)

        assert cleanup_calls == [root.session_key, child.session_key]
        assert await storage.get_session(root.session_key) is None
        assert await storage.get_session(child.session_key) is None
        assert await storage.get_transcript(root.session_id) == []
        assert await storage.get_transcript(child.session_id) == []
        assert await storage.list_agent_tasks(session_key=root.session_key) == []
        assert runtime._tasks == {}
        assert runtime._driver_tasks_by_session == {}
        assert registry.get(child.session_key) is None
        assert completion._watch_task_owners == {}
    finally:
        release_cleanup.set()
        if not deleting.done():
            deleting.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await deleting
        if not direct_task.done():
            direct_task.cancel()
        if not watcher.done():
            watcher.cancel()
        await asyncio.gather(direct_task, watcher, return_exceptions=True)
        set_background_completion_manager(None)
        await completion.close(timeout=None)
        await runtime.shutdown(cancel=True, timeout=2.0)


@pytest.mark.asyncio
async def test_list_adopts_legacy_non_default_workspace(
    workspace_ctx, tmp_path, monkeypatch
) -> None:
    from opensquilla.gateway import rpc_workspaces

    ctx, storage = workspace_ctx
    candidate_scan = AsyncMock(
        wraps=storage.list_legacy_project_workspace_candidates
    )
    forbidden_generic_list = AsyncMock(
        side_effect=AssertionError("legacy adoption must not call list_sessions")
    )
    monkeypatch.setattr(
        storage,
        "list_legacy_project_workspace_candidates",
        candidate_scan,
    )
    monkeypatch.setattr(storage, "list_sessions", forbidden_generic_list)
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    await storage.upsert_session(
        SessionNode(
            session_key="agent:main:webchat:legacy-project",
            origin={
                "sandbox_run_context": {
                    "run_mode": "standard",
                    "workspace": str(legacy),
                }
            },
        )
    )

    first = await rpc_workspaces._handle_workspaces_list(None, ctx)
    second = await rpc_workspaces._handle_workspaces_list(None, ctx)
    session = await storage.get_session("agent:main:webchat:legacy-project")

    assert len(first["workspaces"]) == 1
    assert second == first
    assert first["workspaces"][0]["taskCount"] == 1
    assert session is not None
    assert session.workspace_id == first["workspaces"][0]["id"]
    assert candidate_scan.await_count == 1
    forbidden_generic_list.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_workspace_lists_share_one_legacy_adoption_scan(
    workspace_ctx, tmp_path, monkeypatch
) -> None:
    from opensquilla.gateway import rpc_workspaces

    ctx, storage = workspace_ctx
    legacy = tmp_path / "legacy-concurrent"
    legacy.mkdir()
    await storage.upsert_session(
        SessionNode(
            session_key="agent:main:webchat:legacy-concurrent",
            origin={
                "sandbox_run_context": {
                    "run_mode": "standard",
                    "workspace": str(legacy),
                }
            },
        )
    )

    original_scan = storage.list_legacy_project_workspace_candidates
    scan_started = asyncio.Event()
    release_scan = asyncio.Event()
    scan_count = 0

    async def controlled_scan(*, after_rowid: int = 0, limit: int = 500):
        nonlocal scan_count
        scan_count += 1
        scan_started.set()
        await release_scan.wait()
        return await original_scan(after_rowid=after_rowid, limit=limit)

    monkeypatch.setattr(
        storage,
        "list_legacy_project_workspace_candidates",
        controlled_scan,
    )
    first_task = asyncio.create_task(rpc_workspaces._handle_workspaces_list(None, ctx))
    await scan_started.wait()
    second_task = asyncio.create_task(rpc_workspaces._handle_workspaces_list(None, ctx))
    await asyncio.sleep(0)
    release_scan.set()
    first, second = await asyncio.gather(first_task, second_task)

    assert first == second
    assert scan_count == 1


@pytest.mark.asyncio
async def test_failed_legacy_adoption_is_retried(
    workspace_ctx, tmp_path, monkeypatch
) -> None:
    from opensquilla.gateway import rpc_workspaces

    ctx, storage = workspace_ctx
    legacy = tmp_path / "legacy-retry"
    legacy.mkdir()
    await storage.upsert_session(
        SessionNode(
            session_key="agent:main:webchat:legacy-retry",
            origin={
                "sandbox_run_context": {
                    "run_mode": "standard",
                    "workspace": str(legacy),
                }
            },
        )
    )

    original_scan = storage.list_legacy_project_workspace_candidates
    attempts = 0

    async def flaky_scan(*, after_rowid: int = 0, limit: int = 500):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("synthetic adoption failure")
        return await original_scan(after_rowid=after_rowid, limit=limit)

    monkeypatch.setattr(
        storage,
        "list_legacy_project_workspace_candidates",
        flaky_scan,
    )
    with pytest.raises(RuntimeError, match="synthetic adoption failure"):
        await rpc_workspaces._handle_workspaces_list(None, ctx)

    listed = await rpc_workspaces._handle_workspaces_list(None, ctx)

    assert len(listed["workspaces"]) == 1
    assert attempts == 2


@pytest.mark.asyncio
async def test_all_workspace_handlers_require_local_owner(
    workspace_ctx, tmp_path
) -> None:
    from opensquilla.gateway import rpc_workspaces
    from opensquilla.gateway.rpc import RpcHandlerError

    owner_ctx, _storage = workspace_ctx
    ctx = _remote_ctx(owner_ctx)
    calls = (
        (rpc_workspaces._handle_workspaces_list, None),
        (
            rpc_workspaces._handle_workspaces_open,
            {"path": str(tmp_path), "trusted": True},
        ),
        (
            rpc_workspaces._handle_workspaces_update,
            {"workspaceId": "missing", "name": "x"},
        ),
        (
            rpc_workspaces._handle_workspaces_pin,
            {"workspaceId": "missing", "pinned": True},
        ),
        (
            rpc_workspaces._handle_workspaces_remove,
            {"workspaceId": "missing"},
        ),
        (
            rpc_workspaces._handle_workspaces_history_delete,
            {"workspaceId": "missing"},
        ),
    )
    for handler, params in calls:
        with pytest.raises(RpcHandlerError) as excinfo:
            await handler(params, ctx)
        assert excinfo.value.code == "OWNER_REQUIRED"
