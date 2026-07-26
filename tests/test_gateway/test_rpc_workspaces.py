from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

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
from opensquilla.session.models import SessionNode
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
async def test_list_adopts_legacy_non_default_workspace(
    workspace_ctx, tmp_path
) -> None:
    from opensquilla.gateway import rpc_workspaces

    ctx, storage = workspace_ctx
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
