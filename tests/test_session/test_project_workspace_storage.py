from __future__ import annotations

import pytest

from opensquilla.session.models import SessionNode
from opensquilla.session.storage import SessionStorage


@pytest.mark.asyncio
async def test_project_workspaces_keep_fixed_unpinned_order(tmp_path) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        first = await storage.create_or_restore_project_workspace(
            path="/repo/first",
            path_key="/repo/first",
            display_name="first",
            trusted_at=100,
            now_ms=100,
        )
        second = await storage.create_or_restore_project_workspace(
            path="/repo/second",
            path_key="/repo/second",
            display_name="second",
            trusted_at=200,
            now_ms=200,
        )

        rows = await storage.list_project_workspaces()
        assert [row.workspace_id for row in rows] == [
            second.workspace_id,
            first.workspace_id,
        ]

        await storage.update_project_workspace(first.workspace_id, display_name="renamed")
        assert [row.workspace_id for row in await storage.list_project_workspaces()] == [
            second.workspace_id,
            first.workspace_id,
        ]
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_newly_pinned_workspace_leads_pinned_region(tmp_path) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        first = await storage.create_or_restore_project_workspace(
            path="/repo/first",
            path_key="/repo/first",
            display_name="first",
            trusted_at=100,
            now_ms=100,
        )
        second = await storage.create_or_restore_project_workspace(
            path="/repo/second",
            path_key="/repo/second",
            display_name="second",
            trusted_at=200,
            now_ms=200,
        )

        await storage.set_project_workspace_pin(first.workspace_id, pinned=True, now_ms=300)
        await storage.set_project_workspace_pin(second.workspace_id, pinned=True, now_ms=400)
        rows = await storage.list_project_workspaces()
        assert [row.workspace_id for row in rows] == [
            second.workspace_id,
            first.workspace_id,
        ]

        await storage.set_project_workspace_pin(second.workspace_id, pinned=False, now_ms=500)
        rows = await storage.list_project_workspaces()
        assert [row.workspace_id for row in rows] == [
            first.workspace_id,
            second.workspace_id,
        ]
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_remove_and_restore_reuses_workspace_identity(tmp_path) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        workspace = await storage.create_or_restore_project_workspace(
            path="/repo/project",
            path_key="/repo/project",
            display_name="project",
            trusted_at=100,
            now_ms=100,
        )
        await storage.remove_project_workspace(workspace.workspace_id, now_ms=200)
        assert await storage.list_project_workspaces() == []

        restored = await storage.create_or_restore_project_workspace(
            path="/repo/project",
            path_key="/repo/project",
            display_name="ignored-new-default",
            trusted_at=300,
            now_ms=300,
        )
        assert restored.workspace_id == workspace.workspace_id
        assert restored.display_name == "project"
        assert restored.removed_at is None
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_workspace_binding_and_history_delete_leave_project(tmp_path) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        workspace = await storage.create_or_restore_project_workspace(
            path="/repo/project",
            path_key="/repo/project",
            display_name="project",
            trusted_at=100,
            now_ms=100,
        )
        await storage.upsert_session(
            SessionNode(
                session_key="agent:main:webchat:workspace-history",
                workspace_id=workspace.workspace_id,
            )
        )

        assert await storage.count_project_workspace_tasks(workspace.workspace_id) == 1
        deleted = await storage.delete_project_workspace_sessions(workspace.workspace_id)
        assert deleted == ["agent:main:webchat:workspace-history"]
        assert (
            await storage.get_session("agent:main:webchat:workspace-history")
            is None
        )
        assert await storage.get_project_workspace(workspace.workspace_id) is not None
    finally:
        await storage.close()
