from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio

from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc import RpcContext
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
