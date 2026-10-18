from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from opensquilla.gateway import rpc_workspaces
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError
from opensquilla.project_workspaces import project_path_key
from opensquilla.session.storage import SessionStorage


@pytest_asyncio.fixture
async def creation_ctx(tmp_path):
    async with SessionStorage(str(tmp_path / "sessions.db")) as storage:
        yield RpcContext(
            conn_id="project-create-test",
            principal=Principal(
                role="operator", scopes=frozenset({"operator.write", "operator.read"}),
                is_owner=True, authenticated=True,
            ),
            config=GatewayConfig(), session_manager=SimpleNamespace(storage=storage),
        ), storage


def _params(parent: Path, name: str = "project") -> dict:
    return {"parentPath": str(parent), "name": name, "trusted": True}


@pytest.mark.asyncio
async def test_create_empty_project_and_remember_parent(creation_ctx, tmp_path):
    ctx, storage = creation_ctx
    parent = tmp_path / "projects"
    parent.mkdir()
    result = await rpc_workspaces._handle_workspaces_create(_params(parent, "中文 project"), ctx)
    directory = parent / "中文 project"

    assert result["workspace"]["path"] == str(directory)
    assert result["workspace"]["name"] == "中文 project"
    assert result["workspace"]["available"] is True
    assert list(directory.iterdir()) == []
    workspace = await storage.get_project_workspace_by_path_key(project_path_key(directory))
    assert workspace is not None and workspace.trusted_at is not None
    assert await storage.get_runtime_preference("workspaces.create.parentPath") == str(parent)
    listed = await rpc_workspaces._handle_workspaces_list(None, ctx)
    assert listed["creationDefaults"]["parentPath"] == str(parent)
    assert listed["creationDefaults"]["platform"] == (
        "windows" if sys.platform == "win32" else "posix"
    )
    assert isinstance(listed["creationDefaults"]["hostName"], str)


@pytest.mark.asyncio
async def test_creation_default_parent_survives_reopen_and_falls_back_when_missing(
    creation_ctx, tmp_path, monkeypatch,
):
    ctx, storage = creation_ctx
    parent = tmp_path / "projects"
    home = tmp_path / "home"
    parent.mkdir()
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    initial = await rpc_workspaces._handle_workspaces_list(None, ctx)
    assert initial["creationDefaults"]["parentPath"] == str(home)
    assert await storage.get_runtime_preference("workspaces.create.parentPath") is None

    await rpc_workspaces._handle_workspaces_create(_params(parent), ctx)
    await storage.close()
    await storage.connect()
    assert (await rpc_workspaces._handle_workspaces_list(None, ctx))[
        "creationDefaults"
    ]["parentPath"] == str(parent)

    (parent / "project").rmdir()
    parent.rmdir()
    assert (await rpc_workspaces._handle_workspaces_list(None, ctx))[
        "creationDefaults"
    ]["parentPath"] == str(home)
    # Listing is read-only with respect to the persisted user preference.
    assert await storage.get_runtime_preference("workspaces.create.parentPath") == str(parent)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["", " ", ".", "..", "a/b", "a\\b", "a\x00b"])
async def test_create_rejects_invalid_names_without_allocating(creation_ctx, tmp_path, name):
    ctx, storage = creation_ctx
    before = set(tmp_path.iterdir())
    with pytest.raises(RpcHandlerError) as error:
        await rpc_workspaces._handle_workspaces_create(_params(tmp_path, name), ctx)
    assert error.value.code == "INVALID_PARAMS"
    assert set(tmp_path.iterdir()) == before
    assert await storage.list_project_workspaces() == []


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "win32", reason="Windows directory-name semantics")
@pytest.mark.parametrize("name", [
    "CON", "nul.txt", "COM1", "LPT9.log", "project.", "project ", "a:b", "a?b",
])
async def test_create_rejects_windows_reserved_names(creation_ctx, tmp_path, name):
    ctx, storage = creation_ctx
    with pytest.raises(RpcHandlerError) as error:
        await rpc_workspaces._handle_workspaces_create(_params(tmp_path, name), ctx)
    assert error.value.code == "INVALID_PARAMS"
    assert await storage.list_project_workspaces() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("parent", ["", ".", "missing"])
async def test_create_rejects_non_absolute_or_missing_parent(creation_ctx, tmp_path, parent):
    ctx, storage = creation_ctx
    value = str(tmp_path / "missing") if parent == "missing" else parent
    with pytest.raises(RpcHandlerError) as error:
        await rpc_workspaces._handle_workspaces_create(
            {**_params(tmp_path), "parentPath": value}, ctx,
        )
    assert error.value.code == "INVALID_WORKSPACE_PATH"
    assert await storage.list_project_workspaces() == []


@pytest.mark.asyncio
async def test_create_requires_local_owner_and_explicit_trust(creation_ctx, tmp_path):
    ctx, storage = creation_ctx
    with pytest.raises(RpcHandlerError) as error:
        await rpc_workspaces._handle_workspaces_create({**_params(tmp_path), "trusted": False}, ctx)
    assert error.value.code == "WORKSPACE_TRUST_REQUIRED"
    ctx.principal = Principal(
        role="operator", scopes=frozenset({"operator.admin"}),
        is_owner=False, authenticated=True,
    )
    with pytest.raises(RpcHandlerError) as error:
        await rpc_workspaces._handle_workspaces_create(_params(tmp_path), ctx)
    assert error.value.code == "OWNER_REQUIRED"
    assert not (tmp_path / "project").exists()
    assert await storage.list_project_workspaces() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_kind", ["directory", "file"])
async def test_create_does_not_adopt_or_overwrite_existing_path(
    creation_ctx, tmp_path, existing_kind,
):
    ctx, storage = creation_ctx
    existing = tmp_path / "project"
    if existing_kind == "directory":
        existing.mkdir()
    else:
        existing.write_text("keep", encoding="utf-8")
    with pytest.raises(RpcHandlerError) as error:
        await rpc_workspaces._handle_workspaces_create(_params(tmp_path), ctx)
    assert error.value.code == "WORKSPACE_ALREADY_EXISTS"
    assert existing.exists()
    if existing_kind == "file":
        assert existing.read_text(encoding="utf-8") == "keep"
    assert await storage.list_project_workspaces() == []


@pytest.mark.asyncio
async def test_concurrent_create_has_one_winner(creation_ctx, tmp_path):
    ctx, storage = creation_ctx
    results = await asyncio.gather(*(
        rpc_workspaces._handle_workspaces_create(_params(tmp_path), ctx) for _ in range(2)
    ), return_exceptions=True)
    assert sum(isinstance(item, dict) for item in results) == 1
    errors = [item for item in results if isinstance(item, RpcHandlerError)]
    assert len(errors) == 1 and errors[0].code == "WORKSPACE_ALREADY_EXISTS"
    assert len(await storage.list_project_workspaces()) == 1


@pytest.mark.asyncio
async def test_create_permission_denial_has_no_catalog_entry(creation_ctx, tmp_path, monkeypatch):
    ctx, storage = creation_ctx
    original_mkdir = Path.mkdir

    def deny_directory(path, *args, **kwargs):
        if path == tmp_path / "project":
            raise PermissionError("test denied")
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", deny_directory)
    with pytest.raises(RpcHandlerError) as error:
        await rpc_workspaces._handle_workspaces_create(_params(tmp_path), ctx)
    assert error.value.code == "WORKSPACE_CREATE_FAILED"
    assert await storage.list_project_workspaces() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("external_change", ["none", "write", "replace", "unknown"])
async def test_registration_failure_rolls_back_only_owned_empty_directory(
    creation_ctx, tmp_path, monkeypatch, external_change,
):
    ctx, storage = creation_ctx
    directory = tmp_path / "project"

    async def fail_registration(**_kwargs):
        if external_change == "write":
            (directory / "keep.txt").write_text("external", encoding="utf-8")
        elif external_change == "replace":
            directory.rename(tmp_path / "original-allocation")
            directory.mkdir()
        raise RuntimeError("test registration failure")

    monkeypatch.setattr(storage, "create_or_restore_project_workspace", fail_registration)
    if external_change == "unknown":
        monkeypatch.setattr(storage, "get_project_workspace_by_path_key", AsyncMock(
            side_effect=RuntimeError("readback unavailable"),
        ))
    with pytest.raises(RuntimeError, match="test registration failure"):
        await rpc_workspaces._handle_workspaces_create(_params(tmp_path), ctx)
    assert directory.exists() is (external_change != "none")
    if external_change == "write":
        assert (directory / "keep.txt").read_text(encoding="utf-8") == "external"
    assert await storage.list_project_workspaces() == []


@pytest.mark.asyncio
async def test_unknown_post_commit_failure_preserves_registered_directory(
    creation_ctx, tmp_path, monkeypatch,
):
    ctx, storage = creation_ctx
    original = storage.create_or_restore_project_workspace

    async def committed_but_failed(**kwargs):
        await original(**kwargs)
        raise RuntimeError("response unknown")

    monkeypatch.setattr(storage, "create_or_restore_project_workspace", committed_but_failed)
    with pytest.raises(RuntimeError, match="response unknown"):
        await rpc_workspaces._handle_workspaces_create(_params(tmp_path), ctx)
    assert (tmp_path / "project").is_dir()
    listed = await rpc_workspaces._handle_workspaces_list(None, ctx)
    assert len(listed["workspaces"]) == 1
    assert listed["workspaces"][0]["available"] is True


@pytest.mark.asyncio
async def test_cancellation_settles_registration_and_recovers_via_list(
    creation_ctx, tmp_path, monkeypatch,
):
    ctx, storage = creation_ctx
    original = storage.create_or_restore_project_workspace
    entered = asyncio.Event()
    release = asyncio.Event()

    async def delayed_registration(**kwargs):
        entered.set()
        await release.wait()
        return await original(**kwargs)

    monkeypatch.setattr(storage, "create_or_restore_project_workspace", delayed_registration)
    task = asyncio.create_task(rpc_workspaces._handle_workspaces_create(_params(tmp_path), ctx))
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    listed = await rpc_workspaces._handle_workspaces_list(None, ctx)
    assert len(listed["workspaces"]) == 1
    assert (tmp_path / "project").is_dir()


@pytest.mark.asyncio
async def test_parent_preference_failure_does_not_undo_created_project(
    creation_ctx, tmp_path, monkeypatch,
):
    ctx, storage = creation_ctx
    monkeypatch.setattr(storage, "set_runtime_preference", AsyncMock(
        side_effect=RuntimeError("hint failed"),
    ))
    result = await rpc_workspaces._handle_workspaces_create(_params(tmp_path), ctx)
    assert result["workspace"]["available"] is True
    assert (tmp_path / "project").is_dir()
