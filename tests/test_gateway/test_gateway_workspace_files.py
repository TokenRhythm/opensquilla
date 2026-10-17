from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from starlette.applications import Starlette

from opensquilla.gateway import workspace_files
from opensquilla.gateway.artifacts import register_artifact_routes
from opensquilla.gateway.config import AuthConfig, GatewayConfig
from opensquilla.gateway.execution_workspaces import build_execution_workspace_factory
from opensquilla.gateway.middleware import AuthMiddleware
from opensquilla.paths import native_io_path
from opensquilla.project_workspaces import project_path_key
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage


@pytest.fixture
async def files(tmp_path):
    config = GatewayConfig(auth=AuthConfig(mode="token", token="fixture-secret"))
    async with SessionStorage(tmp_path / "state.db") as storage:
        manager = SessionManager(
            storage,
            execution_workspace_factory=build_execution_workspace_factory(
                config, profile_home=tmp_path
            ),
        )
        session = await manager.create("agent:main:webchat:workspace-files")
        root = Path(session.execution_workspace["root"])
        app = Starlette()
        app.state.desktop_gateway_ownership = SimpleNamespace(
            instance_id="fixture-instance",
            instance_nonce="fixture-instance-nonce",
        )
        register_artifact_routes(app, config=config, session_manager=manager)
        app.add_middleware(AuthMiddleware, config=config)
        headers = {
            "Authorization": "Bearer fixture-secret",
            "x-opensquilla-session-key": session.session_key,
        }
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers=headers,
        ) as client:
            yield SimpleNamespace(
                client=client,
                root=root,
                manager=manager,
                storage=storage,
                session=session,
                config=config,
                app=app,
                headers=headers,
            )


async def resolve(files, paths, **kwargs):
    return await files.client.post(
        "/api/v1/workspace-files/resolve", json={"paths": paths}, **kwargs
    )


async def read_page(files, *, path="source.py", binding=None, start=1, end=200, **kwargs):
    if binding is None:
        binding = (await resolve(files, [path])).json()["workspaceBinding"]
    return await files.client.get(
        "/api/v1/workspace-files/page",
        params={"path": path, "workspaceBinding": binding, "startLine": start, "endLine": end},
        **kwargs,
    )


async def list_dir(files, *, path="", binding="", cursor="0", limit=200, **kwargs):
    params = {"path": path, "cursor": cursor, "limit": limit}
    if binding:
        params["workspaceBinding"] = binding
    return await files.client.get("/api/v1/workspace-files/list", params=params, **kwargs)


async def test_list_directory_is_paginated_sorted_and_hides_noisy_directories(files):
    (files.root / "z.txt").write_text("z")
    (files.root / "a.txt").write_text("a")
    (files.root / "folder").mkdir()
    (files.root / "folder" / "nested.py").write_text("pass\n")
    (files.root / "node_modules").mkdir()
    (files.root / "node_modules" / "dependency.js").write_text("ignored")
    first = await list_dir(files, limit=2)
    assert first.status_code == 200, first.text
    envelope = first.json()
    assert envelope["relativePath"] == ""
    assert [entry["name"] for entry in envelope["entries"]] == ["folder", "a.txt"]
    assert envelope["hasMore"] is True
    assert envelope["entries"][0]["kind"] == "directory"
    binding = envelope["workspaceBinding"]
    second = await list_dir(files, binding=binding, cursor=envelope["nextCursor"], limit=2)
    assert second.status_code == 200, second.text
    assert [entry["name"] for entry in second.json()["entries"]] == ["z.txt"]
    nested = await list_dir(files, path="folder", binding=binding)
    assert [entry["path"] for entry in nested.json()["entries"]] == ["folder/nested.py"]
    assert "node_modules" not in [
        entry["name"] for entry in envelope["entries"] + second.json()["entries"]
    ]


async def test_list_directory_rejects_stale_binding_and_invalid_paging(files):
    (files.root / "source.py").write_text("pass\n")
    binding = (await list_dir(files)).json()["workspaceBinding"]
    assert (await list_dir(files, binding="stale")).status_code == 404
    assert (await list_dir(files, binding=binding, limit=201)).status_code == 400
    assert (await list_dir(files, binding=binding, cursor="-1")).status_code == 400


async def test_directory_snapshot_pages_do_not_repeat_scandir(files, monkeypatch):
    for name in ("a.txt", "b.txt", "c.txt"):
        (files.root / name).write_text(name)
    scans = 0
    original = workspace_files._list_directory

    def counted(*args):
        nonlocal scans
        scans += 1
        return original(*args)

    monkeypatch.setattr(workspace_files, "_list_directory", counted)
    first = (await list_dir(files, limit=1)).json()
    second = (await list_dir(files, cursor=first["nextCursor"], limit=1)).json()
    third = (await list_dir(files, cursor=second["nextCursor"], limit=1)).json()
    assert scans == 1
    assert [page["entries"][0]["name"] for page in (first, second, third)] == [
        "a.txt",
        "b.txt",
        "c.txt",
    ]
    assert first["directoryRevision"] == third["directoryRevision"]


async def test_directory_cursor_rejects_mutation_and_cross_directory_reuse(files):
    for name in ("a.txt", "b.txt"):
        (files.root / name).write_text(name)
    (files.root / "nested").mkdir()
    first = (await list_dir(files, limit=1)).json()
    crossed = await list_dir(files, path="nested", cursor=first["nextCursor"], limit=1)
    assert crossed.status_code == 409
    before = files.root.stat()
    (files.root / "0.txt").write_text("new entry")
    os.utime(files.root, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
    changed = await list_dir(files, cursor=first["nextCursor"], limit=1)
    assert changed.status_code == 409
    assert changed.json()["code"] == "WORKSPACE_DIRECTORY_CHANGED"
    assert (await list_dir(files, limit=1)).status_code == 200


async def test_directory_snapshot_expires_and_lru_is_bounded(files, monkeypatch):
    for name in ("a", "b"):
        (files.root / name).mkdir()
        for filename in ("1.txt", "2.txt"):
            (files.root / name / filename).write_text(filename)
    monkeypatch.setattr(workspace_files, "MAX_DIRECTORY_CACHE_ITEMS", 1)
    service = workspace_files.WorkspaceFiles(files.config, files.manager)
    first = await service.list_dir(files.session.session_key, "a", "", "0", 1)
    await service.list_dir(files.session.session_key, "b", "", "0", 1)
    assert len(service._directory_cache.values) == 1
    assert service._directory_cache.bytes <= service._directory_cache.max_bytes
    with pytest.raises(workspace_files.WorkspaceDirectoryChangedError):
        await service.list_dir(files.session.session_key, "a", "", first["nextCursor"], 1)
    monkeypatch.setattr(workspace_files, "DIRECTORY_SNAPSHOT_TTL_SECONDS", -1)
    expired = await service.list_dir(files.session.session_key, "a", "", "0", 1)
    with pytest.raises(workspace_files.WorkspaceDirectoryChangedError):
        await service.list_dir(files.session.session_key, "a", "", expired["nextCursor"], 1)


async def test_file_revision_fences_page_search_and_download(files):
    target = files.root / "source.py"
    target.write_text("before\n" * 400)
    initial = (await resolve(files, ["source.py"])).json()
    entry = initial["files"][0]
    params = {
        "path": entry["path"],
        "workspaceBinding": initial["workspaceBinding"],
        "expectedRevision": entry["revision"],
    }
    first = await files.client.get(
        "/api/v1/workspace-files/page",
        params={
            **params,
            "startLine": 1,
            "endLine": 200,
        },
    )
    assert first.status_code == 200
    assert first.json()["revision"] == entry["revision"]
    target.write_text("after change\n" * 400)
    for route, extra in (
        ("page", {"startLine": 201, "endLine": 400}),
        ("search", {"query": "after"}),
        ("content", {}),
    ):
        response = await files.client.get(
            f"/api/v1/workspace-files/{route}", params={**params, **extra}
        )
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "WORKSPACE_FILE_CHANGED"
    refreshed = (await resolve(files, ["source.py"])).json()["files"][0]
    assert refreshed["revision"] != entry["revision"]
    assert refreshed["resource"] == entry["resource"]
    assert (await read_page(files)).json()["revision"] == refreshed["revision"]


async def test_text_index_seeks_subsequent_pages_and_search_seeds_index(files, monkeypatch):
    (files.root / "source.py").write_bytes(("中文 source " + "x" * 70 + "\r\n").encode() * 40_000)
    original = workspace_files.os.fdopen
    bytes_read = 0

    class CountingStream:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def read(self, size):
            nonlocal bytes_read
            data = self.stream.read(size)
            bytes_read += len(data)
            return data

    monkeypatch.setattr(workspace_files.os, "fdopen", lambda *args: CountingStream(original(*args)))
    first = await read_page(files, start=1, end=200)
    assert first.status_code == 200
    assert bytes_read > 3_000_000
    bytes_read = 0
    second = await read_page(files, start=20_001, end=20_200)
    assert second.status_code == 200, second.text
    assert second.json()["content"] == first.json()["content"]
    assert 0 < bytes_read <= workspace_files._READ_CHUNK_BYTES
    (files.root / "search.py").write_bytes(("another source " + "x" * 70 + "\n").encode() * 40_000)
    binding = (await resolve(files, ["search.py"])).json()["workspaceBinding"]
    search = await files.client.get(
        "/api/v1/workspace-files/search",
        params={
            "path": "search.py",
            "workspaceBinding": binding,
            "query": "another",
        },
    )
    assert search.status_code == 200
    bytes_read = 0
    preview = await read_page(files, path="search.py", binding=binding, start=20_001, end=20_200)
    assert preview.status_code == 200
    assert 0 < bytes_read <= workspace_files._READ_CHUNK_BYTES


async def test_cached_text_index_revalidates_policy_and_has_bounded_memory(files, monkeypatch):
    monkeypatch.setattr(workspace_files, "MAX_TEXT_INDEX_CACHE_ITEMS", 1)
    service = workspace_files.WorkspaceFiles(files.config, files.manager)
    for name in ("a.txt", "b.txt"):
        (files.root / name).write_text("line\n" * 500)
    binding = (await service.resolve(files.session.session_key, ["a.txt"]))["workspaceBinding"]
    for name in ("a.txt", "b.txt"):
        await service.read_page(files.session.session_key, name, binding, 1, 200)
    assert len(service._text_index_cache.values) == 1
    assert service._text_index_cache.bytes <= service._text_index_cache.max_bytes
    files.config.sandbox.denied_read_globs = ["**/b.txt"]
    with pytest.raises(workspace_files.WorkspaceFileUnavailableError):
        await service.read_page(files.session.session_key, "b.txt", binding, 201, 400)


@pytest.fixture
async def file_apps(files, monkeypatch):
    from opensquilla.gateway import workspace_file_apps

    launched = []
    monkeypatch.setattr(workspace_file_apps, "_interactive_desktop", lambda: True)
    monkeypatch.setattr(
        workspace_file_apps,
        "_installed_apps",
        lambda: [
            workspace_file_apps._App("default", "Default app"),
            workspace_file_apps._App("reveal", "Show in folder"),
            workspace_file_apps._App("vscode", "Visual Studio Code", Path("Code.exe")),
        ],
    )
    monkeypatch.setattr(
        workspace_file_apps, "_launch", lambda app, path: launched.append((app.id, path))
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=files.app),
        base_url="http://127.0.0.1",
        headers=files.headers,
    ) as client:
        yield SimpleNamespace(client=client, launched=launched, module=workspace_file_apps)


async def test_local_webui_apps_launch_verified_current_file(files, file_apps):
    (files.root / "source.py").write_text("pass\n")
    resolved = (await resolve(files, ["source.py"])).json()
    entry = resolved["files"][0]
    catalogue = await file_apps.client.get("/api/v1/workspace-files/apps")
    assert catalogue.status_code == 200
    assert catalogue.json() == {
        "available": True,
        "host": "gateway",
        "apps": [
            {"id": "default", "label": "Default app"},
            {"id": "reveal", "label": "Show in folder"},
            {"id": "vscode", "label": "Visual Studio Code"},
        ],
    }
    for app_id in ("default", "reveal", "vscode"):
        response = await file_apps.client.post(
            "/api/v1/workspace-files/open",
            json={
                "path": entry["path"],
                "workspaceBinding": resolved["workspaceBinding"],
                "expectedRevision": entry["revision"],
                "appId": app_id,
            },
        )
        assert response.status_code == 200, response.text
        assert response.json() == {"ok": True, "host": "gateway", "appId": app_id}
    assert file_apps.launched == [
        (app_id, files.root / "source.py") for app_id in ("default", "reveal", "vscode")
    ]


async def test_app_open_rejects_stale_file_and_arbitrary_launch_arguments(files, file_apps):
    target = files.root / "source.py"
    target.write_text("pass\n")
    resolved = (await resolve(files, ["source.py"])).json()
    params = {
        "path": "source.py",
        "workspaceBinding": resolved["workspaceBinding"],
        "expectedRevision": resolved["files"][0]["revision"],
        "appId": "default",
    }
    for values, expected in (
        ({**params, "executable": "powershell.exe"}, 400),
        ({**params, "appId": "powershell.exe"}, 404),
        ({**params, "path": str(target)}, 400),
        ({**params, "path": "../outside.txt"}, 404),
        ({**params, "workspaceBinding": "a" * 64}, 404),
        ({**params, "expectedRevision": ""}, 400),
    ):
        response = await file_apps.client.post("/api/v1/workspace-files/open", json=values)
        assert response.status_code == expected, response.text
    target.write_text("changed file\n")
    response = await file_apps.client.post("/api/v1/workspace-files/open", json=params)
    assert response.status_code == 409
    assert response.json()["code"] == "WORKSPACE_FILE_CHANGED"
    assert file_apps.launched == []


async def test_app_catalogue_and_open_block_remote_origin_and_headless_host(
    files, file_apps, monkeypatch
):
    for headers, reason, status in (
        ({"x-forwarded-for": "192.168.1.3"}, "remote-client", 200),
        ({"Forwarded": "for=192.168.1.3"}, "remote-client", 200),
        ({"Origin": "https://evil.example"}, "forbidden-origin", 403),
    ):
        catalogue = await file_apps.client.get("/api/v1/workspace-files/apps", headers=headers)
        assert catalogue.status_code == status
        assert catalogue.json()["available"] is False
        assert catalogue.json()["reason"] == reason
        response = await file_apps.client.post(
            "/api/v1/workspace-files/open", json={}, headers=headers
        )
        assert response.status_code == 403
    monkeypatch.setattr(file_apps.module, "_interactive_desktop", lambda: False)
    assert (await file_apps.client.get("/api/v1/workspace-files/apps")).json()[
        "reason"
    ] == "desktop-unavailable"
    assert file_apps.launched == []


async def test_app_open_rechecks_current_host_capability(files, file_apps, monkeypatch):
    async def missing_capability(*args, **kwargs):
        return SimpleNamespace(is_owner=True, capabilities=frozenset())

    monkeypatch.setattr(file_apps.module, "resolve_auth_async", missing_capability)
    result = (await file_apps.client.get("/api/v1/workspace-files/apps")).json()
    assert result["available"] is False
    assert result["reason"] == "host-permission-required"
    assert (await file_apps.client.post("/api/v1/workspace-files/open", json={})).status_code == 403
    assert file_apps.launched == []


async def test_app_open_discards_authority_revoked_during_file_check(files, file_apps, monkeypatch):
    target = files.root / "source.py"
    target.write_text("private file\n")
    resolved = (await resolve(files, ["source.py"])).json()
    original = file_apps.module._readable_file

    def revoke_after_read(*args):
        result = original(*args)
        files.config.sandbox.denied_read_globs = ["**/source.py"]
        return result

    monkeypatch.setattr(file_apps.module, "_readable_file", revoke_after_read)
    response = await file_apps.client.post(
        "/api/v1/workspace-files/open",
        json={
            "path": "source.py",
            "workspaceBinding": resolved["workspaceBinding"],
            "expectedRevision": resolved["files"][0]["revision"],
            "appId": "default",
        },
    )
    assert response.status_code == 404
    assert file_apps.launched == []


async def test_app_open_rechecks_session_after_host_capability_revalidation(
    files, file_apps, monkeypatch,
):
    (files.root / "source.py").write_text("print('current generation')\n")
    resolved = (await resolve(files, ["source.py"])).json()
    original = file_apps.module._access_reason
    checks = 0

    async def revalidate_then_reset(request, config):
        nonlocal checks
        reason = await original(request, config)
        checks += 1
        if checks == 2:
            await files.storage.increment_epoch(files.session.session_key)
        return reason

    monkeypatch.setattr(file_apps.module, "_access_reason", revalidate_then_reset)
    response = await file_apps.client.post(
        "/api/v1/workspace-files/open",
        json={
            "path": "source.py",
            "workspaceBinding": resolved["workspaceBinding"],
            "expectedRevision": resolved["files"][0]["revision"],
            "appId": "default",
        },
    )
    assert checks == 2
    assert response.status_code == 404
    assert file_apps.launched == []


async def test_empty_session_root_metadata_matches_tree_binding_and_opens_directory(
    files, file_apps
):
    metadata = await file_apps.client.get("/api/v1/workspace-files/root")
    assert metadata.status_code == 200, metadata.text
    listing = (await list_dir(files)).json()
    root = metadata.json()
    assert root == {
        "kind": "workspace-root",
        "name": files.root.name,
        "path": str(files.root),
        "workspaceBinding": listing["workspaceBinding"],
    }
    assert metadata.headers["cache-control"] == "no-store"
    # Adding content changes directory mtime, but must not invalidate its identity.
    (files.root / "new.txt").write_text("new content")
    again = (await file_apps.client.get("/api/v1/workspace-files/root")).json()
    assert again["workspaceBinding"] == root["workspaceBinding"]
    for app_id in ("default", "reveal", "vscode"):
        opened = await file_apps.client.post(
            "/api/v1/workspace-files/open-root",
            json={"workspaceBinding": root["workspaceBinding"], "appId": app_id},
        )
        assert opened.status_code == 200, opened.text
        assert opened.json()["appId"] == app_id
    assert file_apps.launched == [
        ("default", files.root),
        ("default", files.root),
        ("vscode", files.root),
    ]


async def test_project_root_without_session_uses_trusted_unicode_directory(
    files, file_apps, tmp_path, monkeypatch
):
    project_root = tmp_path / "项目 & spaces, $(literal)"
    project_root.mkdir()
    project = await files.storage.create_or_restore_project_workspace(
        path=str(project_root.resolve()),
        path_key=project_path_key(project_root, strict=True),
        display_name="Synthetic project",
        trusted_at=1,
    )
    create_session = AsyncMock(side_effect=AssertionError("Must not create a session"))
    monkeypatch.setattr(files.manager, "create", create_session)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=files.app),
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer fixture-secret"},
    ) as client:
        params = {"projectId": project.workspace_id}
        response = await client.get("/api/v1/workspace-files/root", params=params)
        assert response.status_code == 200, response.text
        metadata = response.json()
        assert metadata["path"] == str(project_root.resolve())
        assert metadata["name"] == project_root.name
        catalogue = await client.get("/api/v1/workspace-files/apps", params=params)
        assert catalogue.json()["available"] is True
        for app_id in ("reveal", "vscode"):
            response = await client.post(
                "/api/v1/workspace-files/open-root",
                json={**params, "workspaceBinding": metadata["workspaceBinding"], "appId": app_id},
            )
            assert response.status_code == 200, response.text
        await files.storage.remove_project_workspace(project.workspace_id)
        assert (await client.get("/api/v1/workspace-files/root", params=params)).status_code == 404
        response = await client.post(
            "/api/v1/workspace-files/open-root",
            json={**params, "workspaceBinding": metadata["workspaceBinding"], "appId": "default"},
        )
        assert response.status_code == 404
    assert file_apps.launched == [("default", project_root), ("vscode", project_root)]
    create_session.assert_not_awaited()


@pytest.mark.parametrize("mutation", ["replacement", "missing", "epoch", "project-binding"])
async def test_open_root_rejects_changed_identity_without_recreating_directory(
    files, file_apps, tmp_path, mutation
):
    metadata = (await file_apps.client.get("/api/v1/workspace-files/root")).json()
    if mutation in ("replacement", "missing"):
        files.root.rename(files.root.with_name(files.root.name + "-previous"))
        if mutation == "replacement":
            files.root.mkdir()
    elif mutation == "epoch":
        await files.storage.upsert_session(
            files.session.model_copy(update={"epoch": files.session.epoch + 1})
        )
    else:
        project_root = tmp_path / "other-root"
        project_root.mkdir()
        project = await files.storage.create_or_restore_project_workspace(
            path=str(project_root.resolve()),
            path_key=project_path_key(project_root, strict=True),
            display_name="Other root",
            trusted_at=1,
        )
        await files.storage.upsert_session(
            files.session.model_copy(update={"workspace_id": project.workspace_id})
        )
    response = await file_apps.client.post(
        "/api/v1/workspace-files/open-root",
        json={"workspaceBinding": metadata["workspaceBinding"], "appId": "default"},
    )
    assert response.status_code == 404, response.text
    assert file_apps.launched == []
    if mutation == "missing":
        assert not files.root.exists()


async def test_project_root_rejects_untrusted_unknown_and_missing_directory(
    files, file_apps, tmp_path
):
    root = tmp_path / "untrusted-project"
    root.mkdir()
    project = await files.storage.create_or_restore_project_workspace(
        path=str(root.resolve()),
        path_key=project_path_key(root, strict=True),
        display_name="Untrusted",
        trusted_at=None,
    )
    headers = {"x-opensquilla-session-key": ""}
    for project_id in ("unknown", project.workspace_id):
        params = {"projectId": project_id}
        response = await file_apps.client.get(
            "/api/v1/workspace-files/root", params=params, headers=headers
        )
        assert response.status_code == 404
        catalogue = await file_apps.client.get(
            "/api/v1/workspace-files/apps", params=params, headers=headers
        )
        assert catalogue.json()["available"] is False
    project = await files.storage.create_or_restore_project_workspace(
        path=str(root.resolve()),
        path_key=project_path_key(root, strict=True),
        display_name="Trusted",
        trusted_at=1,
    )
    root.rmdir()
    response = await file_apps.client.get(
        "/api/v1/workspace-files/root",
        params={"projectId": project.workspace_id},
        headers=headers,
    )
    assert response.status_code == 404
    assert not root.exists()


async def test_root_scope_rejects_mixed_scopes_subpaths_and_arbitrary_arguments(files, file_apps):
    binding = (await list_dir(files)).json()["workspaceBinding"]
    for query in ("?projectId=project", "?path=child", "?projectId=", "?projectId=a&projectId=b"):
        assert (
            await file_apps.client.get(f"/api/v1/workspace-files/root{query}")
        ).status_code == 400
    assert (
        await file_apps.client.get(
            "/api/v1/workspace-files/root", headers={"x-opensquilla-session-key": ""}
        )
    ).status_code == 400
    base = {"workspaceBinding": binding, "appId": "default"}
    for values, expected in (
        ({**base, "projectId": "project"}, 400),
        ({**base, "projectId": ""}, 400),
        ({**base, "path": "child"}, 400),
        ({**base, "executable": "powershell.exe"}, 400),
        ({**base, "appId": "powershell.exe"}, 404),
        ({**base, "workspaceBinding": "a" * 64}, 404),
        ({**base, "workspaceBinding": 12}, 400),
        ({**base, "projectId": None}, 400),
    ):
        response = await file_apps.client.post("/api/v1/workspace-files/open-root", json=values)
        assert response.status_code == expected, response.text
    assert file_apps.launched == []


async def test_root_metadata_needs_owner_but_not_host_execute_and_open_requires_local_desktop(
    files, file_apps, monkeypatch
):
    async def no_host_execute(*args, **kwargs):
        return SimpleNamespace(is_owner=True, capabilities=frozenset())

    monkeypatch.setattr(file_apps.module, "resolve_auth_async", no_host_execute)
    assert (await file_apps.client.get("/api/v1/workspace-files/root")).status_code == 200
    assert (
        await file_apps.client.post("/api/v1/workspace-files/open-root", json={})
    ).status_code == 403
    for headers in ({"Origin": "https://evil.example"}, {"Origin": "null"}):
        assert (
            await file_apps.client.get("/api/v1/workspace-files/root", headers=headers)
        ).status_code == 403
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=files.app, client=("192.168.1.8", 1234)),
        base_url="http://127.0.0.1",
        headers=files.headers,
    ) as remote:
        # A direct LAN token is not a locally-proven owner under current auth policy.
        assert (await remote.get("/api/v1/workspace-files/root")).status_code == 403
    assert file_apps.launched == []


@pytest.mark.parametrize(
    "headers", [{"x-forwarded-for": "192.168.1.8"}, {"Origin": "https://evil.example"}]
)
async def test_open_root_blocks_remote_and_wrong_origin(files, file_apps, headers):
    metadata = (await file_apps.client.get("/api/v1/workspace-files/root")).json()
    response = await file_apps.client.post(
        "/api/v1/workspace-files/open-root",
        json={"workspaceBinding": metadata["workspaceBinding"], "appId": "default"},
        headers=headers,
    )
    assert response.status_code == 403
    assert file_apps.launched == []


async def test_open_root_rechecks_policy_after_resolution(files, file_apps, monkeypatch):
    metadata = (await file_apps.client.get("/api/v1/workspace-files/root")).json()
    original = file_apps.module._root_identity

    def revoke(workspace):
        result = original(workspace)
        files.config.sandbox.denied_read_roots = [str(files.root)]
        return result

    monkeypatch.setattr(file_apps.module, "_root_identity", revoke)
    response = await file_apps.client.post(
        "/api/v1/workspace-files/open-root",
        json={"workspaceBinding": metadata["workspaceBinding"], "appId": "default"},
    )
    assert response.status_code == 404
    assert file_apps.launched == []


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
def test_root_launcher_enters_directory_and_editor_uses_fixed_argv(tmp_path, monkeypatch, platform):
    from opensquilla.gateway import workspace_file_apps

    calls = []
    root = tmp_path / "项目 & spaces, $(literal)"
    root.mkdir()
    editor = tmp_path / "editor.exe"
    editor.write_bytes(b"fixture")
    workspace = workspace_files._Workspace(
        root=root,
        binding="fixture",
        profile=workspace_files.FileSystemPermissionProfile.workspace(
            workspace=root,
            denied_read_roots=(),
            denied_read_globs=(),
            host_root_readonly=False,
            tmp_writable=False,
            tmpdir_env_writable=False,
        ),
    )
    monkeypatch.setattr(workspace_file_apps.sys, "platform", platform)
    monkeypatch.setattr(workspace_file_apps.shutil, "which", lambda _: "/usr/bin/xdg-open")
    monkeypatch.setattr(
        workspace_file_apps,
        "_installed_apps",
        lambda: [
            workspace_file_apps._App("reveal", "Show in folder"),
            workspace_file_apps._App("vscode", "VS Code", editor),
        ],
    )
    monkeypatch.setattr(
        workspace_file_apps,
        "_windows_reveal_file",
        lambda _: pytest.fail("A root must be entered, not selected in its parent"),
    )
    monkeypatch.setattr(
        workspace_file_apps.subprocess,
        "Popen",
        lambda *args, **kwargs: calls.append((args[0], kwargs)),
    )
    identity = workspace_file_apps._root_identity(workspace)
    workspace_file_apps._open_verified_root(workspace, identity, "reveal")
    workspace_file_apps._open_verified_root(workspace, identity, "vscode")
    expected_opener = {
        "win32": str(Path(os.environ.get("WINDIR", r"C:\Windows")) / "explorer.exe"),
        "darwin": "/usr/bin/open",
        "linux": "/usr/bin/xdg-open",
    }[platform]
    assert calls[0][0] == [expected_opener, str(root)]
    assert calls[1][0] == [str(editor), "--", str(root)]
    assert all(call[1]["shell"] is False for call in calls)


def test_app_launcher_uses_fixed_argv_and_scrubs_secrets(tmp_path, monkeypatch):
    from opensquilla.gateway import workspace_file_apps

    executable = tmp_path / "editor.exe"
    executable.write_bytes(b"fixture")
    target = tmp_path / "a name & $(secret).txt"
    target.write_text("text")
    calls = []
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-secret")
    monkeypatch.setenv("OPENSQUILLA_TOKEN", "fixture-token")
    monkeypatch.setenv("ELECTRON_RUN_AS_NODE", "1")
    monkeypatch.setattr(
        workspace_file_apps.subprocess,
        "Popen",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    workspace_file_apps._launch(workspace_file_apps._App("vscode", "VS Code", executable), target)
    args, kwargs = calls[0]
    assert args[0] == [str(executable), "--", str(target)]
    assert kwargs["shell"] is False
    assert not {"OPENAI_API_KEY", "OPENSQUILLA_TOKEN", "ELECTRON_RUN_AS_NODE"}.intersection(
        kwargs["env"]
    )


@pytest.mark.parametrize("select_result", [0, -2147467259], ids=["selected", "shell-failure"])
def test_windows_reveal_selects_file_pidl_and_releases_com_resources(
    tmp_path, monkeypatch, select_result
):
    import ctypes

    from opensquilla.gateway import workspace_file_apps

    target = tmp_path / "图表 & comma, with spaces.csv"
    target.write_text("x,y\n1,2")
    calls = []

    def initialize(_reserved, mode):
        calls.append(("initialize", mode))
        return 0

    def parse(path, _context, output, _attributes, _actual):
        calls.append(("parse", path))
        ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = 12345
        return 0

    def select(pidl, count, children, flags):
        calls.append(("select", pidl.value, count, children, flags))
        return select_result

    def free(pidl):
        calls.append(("free", pidl.value))

    def uninitialize():
        calls.append(("uninitialize",))

    shell = SimpleNamespace(SHParseDisplayName=parse, SHOpenFolderAndSelectItems=select)
    ole = SimpleNamespace(
        CoInitializeEx=initialize, CoTaskMemFree=free, CoUninitialize=uninitialize
    )
    monkeypatch.setattr(
        workspace_file_apps.ctypes,
        "WinDLL",
        lambda name, **kwargs: shell if name == "shell32" else ole,
        raising=False,
    )
    if select_result:
        with pytest.raises(OSError, match="select the file"):
            workspace_file_apps._windows_reveal_file(target)
    else:
        workspace_file_apps._windows_reveal_file(target)
    assert calls == [
        ("initialize", 2),
        ("parse", str(target)),
        ("select", 12345, 0, None, 0),
        ("free", 12345),
        ("uninitialize",),
    ]


def metadata_headers(files, *, path: str, binding: str, session_key: str | None = None):
    key = session_key or files.session.session_key
    owner = files.app.state.desktop_gateway_ownership
    return {
        **files.headers,
        "x-opensquilla-session-key": key,
        "x-opensquilla-native-signature": workspace_files._workspace_metadata_signature(
            owner.instance_id, owner.instance_nonce, key, path, binding
        ),
    }


async def test_resolve_and_read_unpublished_files_preserves_current_bytes(files):
    contents = {
        "图表.svg": b"<svg><script>alert(1)</script></svg>",
        "page.html": b"<script>alert(1)</script>",
        "report.pdf": b"%PDF-fixture",
        "plot.png": b"\x89PNG\r\n\x1a\nfixture",
        "empty.txt": b"",
    }
    for name, data in contents.items():
        (files.root / name).write_bytes(data)
    requested = [*contents, str(files.root / "图表.svg"), "./empty.txt", "missing.txt"]
    response = await resolve(files, requested)
    assert response.status_code == 200, response.text
    result = response.json()
    entries = result["files"]
    assert len(entries) == 7
    assert {entry["kind"] for entry in entries} == {"text", "image", "download"}
    assert {entry["name"]: entry["kind"] for entry in entries}["图表.svg"] == "text"
    for entry in entries:
        assert entry["requestedPath"] in requested
        assert entry["size"] == len(contents[entry["name"]])
        assert entry["textPaging"] is (entry["kind"] == "text")
        assert entry["nativeActions"] is True
        response = await files.client.get(entry["contentUrl"])
        assert response.status_code == 200, response.text
        assert response.content == contents[entry["name"]]
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["content-disposition"].startswith("attachment;")
        assert "sandbox" in response.headers["content-security-policy"]
    (files.root / "empty.txt").write_text("edited after resolve")
    entry = next(item for item in entries if item["requestedPath"] == "empty.txt")
    assert (await files.client.get(entry["contentUrl"])).text == "edited after resolve"


@pytest.mark.parametrize(
    ("name", "mime", "kind"),
    [
        ("chart.WEBP", "image/webp", "image"),
        ("data.csv", "text/csv", "text"),
        ("data.tsv", "text/tab-separated-values", "text"),
        ("source.ts", "text/typescript", "text"),
        ("component.tsx", "text/typescript", "text"),
        ("config.yaml", "application/yaml", "text"),
        ("config.toml", "application/toml", "text"),
        ("tone.flac", "audio/flac", "download"),
        ("tone.aac", "audio/aac", "download"),
        ("tone.wav", "audio/wav", "download"),
        ("tone.mp3", "audio/mpeg", "download"),
        ("motion.mp4", "video/mp4", "download"),
        ("motion.webm", "video/webm", "download"),
    ],
)
async def test_preview_mime_is_independent_of_windows_file_associations(
    files, monkeypatch, name, mime, kind
):
    # Windows mappings can omit WebP or claim TS/CSV/FLAC are different media types.
    monkeypatch.setattr(workspace_files, "artifact_mime_for_name", lambda _: "video/x-wrong")
    (files.root / name).write_bytes(b"synthetic\x00bytes")
    resolved = (await resolve(files, [name])).json()
    entry = resolved["files"][0]
    assert entry["mime"] == mime
    assert entry["kind"] == kind
    listed = (await list_dir(files)).json()["entries"]
    assert next(item for item in listed if item["name"] == name)["mime"] == mime
    response = await files.client.get(
        httpx.URL(entry["contentUrl"]).copy_add_param("expectedRevision", entry["revision"])
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].split(";")[0] == mime


async def test_generated_preview_gallery_serves_actual_formats_with_pinned_mime(files):
    script = (
        Path(__file__).resolve().parents[2] / "scripts/testing/create_workbench_preview_fixtures.py"
    )
    spec = importlib.util.spec_from_file_location("workbench_preview_fixtures", script)
    assert spec and spec.loader
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    output = files.root / "preview-gallery"
    # The portable test never launches FFmpeg; the standalone script also creates
    # compressed audio/video when FFmpeg is installed. No optional deps installed here.
    manifest = generator.generate(output)
    assert manifest["syntheticOnly"] is True
    assert len(manifest["files"]) >= 21
    fixtures = {f"preview-gallery/{item['path']}": item for item in manifest["files"]}
    resolved = (await resolve(files, list(fixtures))).json()
    assert len(resolved["files"]) == len(fixtures)
    listing = (await list_dir(files, path="preview-gallery")).json()["entries"]
    mime_by_name = {item["name"]: item["mime"] for item in listing}
    for entry in resolved["files"]:
        expected = fixtures[entry["path"]]
        assert entry["mime"] == expected["expectedMime"], entry["path"]
        assert mime_by_name[entry["name"]] == expected["expectedMime"]
        response = await files.client.get(
            httpx.URL(entry["contentUrl"]).copy_add_param("expectedRevision", entry["revision"])
        )
        assert response.status_code == 200, entry["path"]
        assert response.headers["content-type"].split(";")[0] == expected["expectedMime"]
        assert hashlib.sha256(response.content).hexdigest() == expected["sha256"]
        assert response.headers["x-content-type-options"] == "nosniff"
        assert "sandbox" in response.headers["content-security-policy"]
    # A repeat run must never silently overwrite files, even in a test directory.
    sentinel = output / "user-owned.txt"
    sentinel.write_text("preserve me")
    with pytest.raises(FileExistsError):
        generator.generate(output)
    assert sentinel.read_text() == "preserve me"


async def test_ordinary_gateway_does_not_advertise_native_actions(files):
    (files.root / "source.py").write_text("pass\n")
    files.app.state.desktop_gateway_ownership = None
    entry = (await resolve(files, ["source.py"])).json()["files"][0]
    assert entry["nativeActions"] is False
    assert entry["textPaging"] is True


@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
async def test_read_page_streams_text_ranges_without_returning_absolute_paths(files, newline):
    target = files.root / "large.py"
    target.write_bytes("".join(f"line {index}{newline}" for index in range(1, 451)).encode())
    resolved = (await resolve(files, ["large.py"])).json()
    entry = resolved["files"][0]
    response = await files.client.get(
        "/api/v1/workspace-files/page",
        params={
            "path": entry["path"],
            "workspaceBinding": resolved["workspaceBinding"],
            "startLine": 201,
            "endLine": 400,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "relativePath": "large.py",
        "content": "".join(f"line {index}{newline}" for index in range(201, 401)),
        "totalLines": 450,
        "startLine": 201,
        "endLine": 400,
        "revision": entry["revision"],
        "resource": entry["resource"],
    }
    assert "sourcePath" not in response.json()
    assert (
        await files.client.get(
            "/api/v1/workspace-files/page",
            params={
                "path": "large.py",
                "workspaceBinding": resolved["workspaceBinding"],
                "startLine": 1,
                "endLine": 201,
            },
        )
    ).status_code == 400


@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
async def test_read_page_handles_source_larger_than_two_mib(files, newline):
    line = "text source " + "x" * 64 + newline
    count = 40_000
    content = line * count
    assert len(content) > 2 * 1024 * 1024
    (files.root / "source.py").write_bytes(content.encode())
    response = await read_page(files, start=20_001, end=20_200)
    assert response.status_code == 200, response.text
    assert response.json()["content"] == line * 200
    assert response.json()["totalLines"] == count
    assert len(response.content) < workspace_files.MAX_TEXT_PAGE_BYTES


@pytest.mark.parametrize(
    "content", ["", "a", "a\n", "a\r\nb\rc\n", "a\vb\fc\x1cd\x1de\x1ff\x85g\u2028h\u2029"]
)
async def test_page_line_boundaries_match_python_splitlines(files, monkeypatch, content):
    # Two bytes per read exercises CRLF and multi-byte Unicode boundaries.
    monkeypatch.setattr(workspace_files, "_READ_CHUNK_BYTES", 2)
    (files.root / "source.py").write_bytes(content.encode("utf-8"))
    lines = content.splitlines(keepends=True)
    response = await read_page(files)
    assert response.status_code == 200, response.text
    assert response.json()["content"] == content
    assert response.json()["totalLines"] == max(1, len(lines))
    for index, line in enumerate(lines, 1):
        response = await read_page(files, start=index, end=index)
        assert response.status_code == 200, response.text
        assert response.json()["content"] == line
        assert response.json()["startLine"] == index
        assert response.json()["endLine"] == index


@pytest.mark.parametrize("content", [b"line\ninvalid\xff", b"line\nnul\x00"])
async def test_page_rejects_invalid_text_even_outside_requested_page(files, content):
    (files.root / "source.py").write_bytes(content)
    assert (await read_page(files, start=1, end=1)).status_code == 404


@pytest.mark.parametrize(
    ("content", "limit", "max_bytes"),
    [
        (
            b"x" * (workspace_files.MAX_TEXT_LINE_BYTES + 1),
            "line",
            workspace_files.MAX_TEXT_LINE_BYTES,
        ),
        (
            (b"x" * (workspace_files.MAX_TEXT_LINE_BYTES - 1) + b"\n") * 5,
            "page",
            workspace_files.MAX_TEXT_PAGE_BYTES,
        ),
        (
            ("文" * (workspace_files.MAX_TEXT_LINE_BYTES // 3 + 1)).encode(),
            "line",
            workspace_files.MAX_TEXT_LINE_BYTES,
        ),
    ],
    ids=["oversized-line", "oversized-page", "oversized-utf8-line"],
)
async def test_page_has_byte_limits_for_lines_and_response(files, content, limit, max_bytes):
    (files.root / "source.py").write_bytes(content)
    metadata = (await resolve(files, ["source.py"])).json()
    response = await read_page(files, binding=metadata["workspaceBinding"])
    assert response.status_code == 413
    assert response.json() == {
        "code": "WORKSPACE_TEXT_LIMIT",
        "limit": limit,
        "maxBytes": max_bytes,
    }
    # A preview bound does not make the file unavailable for download/external apps.
    downloaded = await files.client.get(
        "/api/v1/workspace-files/content",
        params={
            "path": "source.py",
            "workspaceBinding": metadata["workspaceBinding"],
            "expectedRevision": metadata["files"][0]["revision"],
        },
    )
    assert downloaded.status_code == 200
    assert downloaded.content == content
    # Permission/binding failures still hide file existence and format details.
    assert (await read_page(files, binding="stale")).status_code == 404


async def test_source_search_reports_oversized_line_without_exposing_paths(files):
    (files.root / "source.py").write_bytes(b"x" * (workspace_files.MAX_TEXT_LINE_BYTES + 1))
    binding = (await resolve(files, ["source.py"])).json()["workspaceBinding"]
    response = await files.client.get(
        "/api/v1/workspace-files/search",
        params={"path": "source.py", "workspaceBinding": binding, "query": "x"},
    )
    assert response.status_code == 413
    assert response.json() == {
        "code": "WORKSPACE_TEXT_LIMIT",
        "limit": "line",
        "maxBytes": workspace_files.MAX_TEXT_LINE_BYTES,
    }


async def test_page_rechecks_session_and_current_binding(files):
    (files.root / "source.py").write_text("private source\n")
    binding = (await resolve(files, ["source.py"])).json()["workspaceBinding"]
    assert (await read_page(files, binding="stale")).status_code == 404
    other = await files.manager.create("agent:main:webchat:page-other")
    assert (
        await read_page(
            files, binding=binding, headers={"x-opensquilla-session-key": other.session_key}
        )
    ).status_code == 404
    await files.storage.upsert_session(
        files.session.model_copy(update={"epoch": files.session.epoch + 1})
    )
    assert (await read_page(files, binding=binding)).status_code == 404


async def test_page_discards_result_when_workspace_authority_changes_during_scan(
    files, monkeypatch
):
    (files.root / "source.py").write_text("private source\n")
    binding = (await resolve(files, ["source.py"])).json()["workspaceBinding"]
    scan = workspace_files._read_file_page
    loop = asyncio.get_running_loop()

    async def revoke():
        await files.storage.upsert_session(
            files.session.model_copy(update={"epoch": files.session.epoch + 1})
        )

    def racing_scan(*args):
        result = scan(*args)
        asyncio.run_coroutine_threadsafe(revoke(), loop).result(timeout=5)
        return result

    monkeypatch.setattr(workspace_files, "_read_file_page", racing_scan)
    assert (await read_page(files, binding=binding)).status_code == 404


async def test_page_discards_file_changed_during_read_even_with_restored_mtime(files, monkeypatch):
    target = files.root / "source.py"
    target.write_text("original\n")
    before = target.stat()
    binding = (await resolve(files, ["source.py"])).json()["workspaceBinding"]
    original_fstat = workspace_files.os.fstat
    calls = 0

    def mutate_before_final_stat(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            target.write_text("mutated!\n")
            os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        return original_fstat(fd)

    monkeypatch.setattr(workspace_files.os, "fstat", mutate_before_final_stat)
    assert (await read_page(files, binding=binding)).status_code == 404


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"], ids=["lf", "crlf"])
async def test_page_does_not_follow_file_growth_past_initial_size(files, monkeypatch, newline):
    target = files.root / "source.py"
    content = b"original" + newline
    target.write_bytes(content)
    binding = (await resolve(files, ["source.py"])).json()["workspaceBinding"]
    original_open = workspace_files.os.fdopen
    reads = []

    class GrowingStream:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def fileno(self):
            return self.stream.fileno()

        def read(self, size):
            reads.append(size)
            with target.open("ab") as writer:
                writer.write(b"growth\n" * 1000)
            return self.stream.read(size)

    monkeypatch.setattr(
        workspace_files.os, "fdopen", lambda *args: GrowingStream(original_open(*args))
    )
    assert (await read_page(files, binding=binding)).status_code == 404
    assert reads == [len(content) + 1]


async def test_search_scans_once_and_obeys_text_and_binding_guards(files, monkeypatch):
    (files.root / "source.py").write_text("before\n" * 500 + "Chosen Match\nlast\n")
    binding = (await resolve(files, ["source.py"])).json()["workspaceBinding"]
    params = {"path": "source.py", "workspaceBinding": binding, "query": "chosen MATCH"}
    original = workspace_files._scan_text_file
    calls = []

    def counted_scan(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(workspace_files, "_scan_text_file", counted_scan)
    response = await files.client.get("/api/v1/workspace-files/search", params=params)
    assert response.status_code == 200, response.text
    result = response.json()
    assert {key: result[key] for key in ("relativePath", "totalLines", "matchLine")} == {
        "relativePath": "source.py",
        "totalLines": 502,
        "matchLine": 501,
    }
    assert len(result["revision"]) == 64
    assert len(calls) == 1
    for query in ["", " ", "x" * 513, "\x00"]:
        assert (
            await files.client.get(
                "/api/v1/workspace-files/search", params={**params, "query": query}
            )
        ).status_code == 400
    assert (
        await files.client.get(
            "/api/v1/workspace-files/search", params={**params, "workspaceBinding": "stale"}
        )
    ).status_code == 404
    (files.root / "source.py").write_bytes(b"chosen Match\n\xff")
    assert (
        await files.client.get("/api/v1/workspace-files/search", params=params)
    ).status_code == 404


async def test_metadata_returns_verified_native_identity_only_for_current_binding(files):
    target = files.root / "build.py"
    target.write_text("print('fixture')\n")
    resolved = (await resolve(files, ["build.py"])).json()
    entry = resolved["files"][0]
    response = await files.client.get(
        "/api/v1/workspace-files/metadata",
        params={"path": entry["path"], "workspaceBinding": resolved["workspaceBinding"]},
    )
    assert response.status_code == 403
    response = await files.client.get(
        "/api/v1/workspace-files/metadata",
        params={"path": entry["path"], "workspaceBinding": resolved["workspaceBinding"]},
        headers=metadata_headers(files, path=entry["path"], binding=resolved["workspaceBinding"]),
    )
    assert response.status_code == 200, response.text
    metadata = response.json()
    assert metadata["relativePath"] == "build.py"
    assert metadata["workspaceBinding"] == resolved["workspaceBinding"]
    assert metadata["sourcePath"] == str(target)
    assert metadata["workspace"] == str(files.root)
    assert metadata["size"] == target.stat().st_size
    identity = target.stat()
    assert metadata["identity"] == {
        "dev": str(identity.st_dev & 0xFFFFFFFF if os.name == "nt" else identity.st_dev),
        "ino": str(identity.st_ino),
        "size": str(identity.st_size),
        "mtimeNs": str(identity.st_mtime_ns),
        "ctimeNs": str(identity.st_ctime_ns),
    }
    assert (
        await files.client.get(
            "/api/v1/workspace-files/metadata",
            params={"path": "build.py", "workspaceBinding": "stale"},
            headers=metadata_headers(files, path="build.py", binding="stale"),
        )
    ).status_code == 404
    await files.storage.upsert_session(
        files.session.model_copy(update={"epoch": files.session.epoch + 1})
    )
    assert (
        await files.client.get(
            "/api/v1/workspace-files/metadata",
            params={"path": "build.py", "workspaceBinding": resolved["workspaceBinding"]},
            headers=metadata_headers(files, path="build.py", binding=resolved["workspaceBinding"]),
        )
    ).status_code == 404


def test_windows_native_identity_matches_libuv_volume_serial_without_truncating_inode(monkeypatch):
    monkeypatch.setattr(workspace_files, "_WINDOWS", True)
    metadata = SimpleNamespace(
        st_dev=0x12345678ABCDEF01,
        st_ino=0x123456789ABCDEF0,
        st_size=17,
        st_mtime_ns=1_700_000_001_000_000_000,
        st_ctime_ns=1_700_000_000_000_000_000,
    )
    assert workspace_files._native_file_identity(metadata) == {
        "dev": str(0xABCDEF01),
        "ino": str(0x123456789ABCDEF0),
        "size": "17",
        "mtimeNs": "1700000001000000000",
        "ctimeNs": "1700000000000000000",
    }
    assert workspace_files._native_identity_supported(metadata)
    metadata.st_ino = 1 << 80
    assert not workspace_files._native_identity_supported(metadata)


def test_windows_cross_stat_comparison_does_not_confuse_creation_and_change_time(monkeypatch):
    monkeypatch.setattr(workspace_files, "_WINDOWS", True)
    metadata = dict(st_dev=1, st_ino=2, st_size=17, st_mtime_ns=200)
    before = SimpleNamespace(**metadata, st_ctime_ns=100)
    opened = SimpleNamespace(**metadata, st_ctime_ns=300)
    assert workspace_files._same_file_snapshot(before, opened)
    opened.st_size = 18
    assert not workspace_files._same_file_snapshot(before, opened)
    opened.st_size = before.st_size
    monkeypatch.setattr(workspace_files, "_WINDOWS", False)
    assert not workspace_files._same_file_snapshot(before, opened)


async def test_metadata_requires_an_active_desktop_owner(files):
    target = files.root / "build.py"
    target.write_text("print('fixture')\n")
    resolved = (await resolve(files, ["build.py"])).json()
    headers = metadata_headers(files, path="build.py", binding=resolved["workspaceBinding"])
    files.app.state.desktop_gateway_ownership = None
    response = await files.client.get(
        "/api/v1/workspace-files/metadata",
        params={"path": "build.py", "workspaceBinding": resolved["workspaceBinding"]},
        headers=headers,
    )
    assert response.status_code == 403


@pytest.mark.parametrize("nonce", [None, "", "non-ascii-密钥", 123])
async def test_invalid_native_owner_cannot_bypass_metadata_signature(files, nonce):
    (files.root / "source.py").write_text("pass\n")
    files.app.state.desktop_gateway_ownership.instance_nonce = nonce
    resolved = (await resolve(files, ["source.py"])).json()
    assert resolved["files"][0]["nativeActions"] is False
    response = await files.client.get(
        "/api/v1/workspace-files/metadata",
        params={"path": "source.py", "workspaceBinding": resolved["workspaceBinding"]},
    )
    assert response.status_code == 403


async def test_native_signature_binds_session_path_and_workspace(files):
    (files.root / "source.py").write_text("pass\n")
    (files.root / "other.py").write_text("private\n")
    binding = (await resolve(files, ["source.py"])).json()["workspaceBinding"]
    headers = metadata_headers(files, path="source.py", binding=binding)
    for path, supplied_binding, session in [
        ("other.py", binding, files.session.session_key),
        ("source.py", "other-binding", files.session.session_key),
        ("source.py", binding, "agent:main:webchat:other"),
    ]:
        response = await files.client.get(
            "/api/v1/workspace-files/metadata",
            params={"path": path, "workspaceBinding": supplied_binding},
            headers={**headers, "x-opensquilla-session-key": session},
        )
        assert response.status_code == 403


@pytest.mark.parametrize(
    "path",
    [
        "../outside.txt",
        "dir/../../outside.txt",
        "C:outside.txt",
        "file:///outside.txt",
        "https://example.com/a.svg",
        "a\x00.svg",
    ],
)
async def test_paths_never_escape_session_workspace(files, tmp_path, path):
    (tmp_path / "outside.txt").write_text("private")
    result = (await resolve(files, [path, str(tmp_path / "outside.txt")])).json()
    assert result["files"] == []
    content = await files.client.get(
        "/api/v1/workspace-files/content",
        params={
            "path": path,
            "workspaceBinding": result["workspaceBinding"],
        },
    )
    assert content.status_code == 404


async def test_auth_owner_and_origin_guard_apply_to_both_endpoints(files):
    (files.root / "report.txt").write_text("private")
    entry = (await resolve(files, ["report.txt"])).json()["files"][0]
    for headers, status in [
        ({"Authorization": "Bearer invalid"}, 401),
        ({"Origin": "https://evil.invalid"}, 403),
        ({"Origin": "null"}, 403),
        ({"x-opensquilla-session-key": ""}, 404),
    ]:
        assert (await resolve(files, ["report.txt"], headers=headers)).status_code == status
        assert (await files.client.get(entry["contentUrl"], headers=headers)).status_code == status
        for endpoint, extra in [
            ("page", {"startLine": 1, "endLine": 1}),
            ("search", {"query": "p"}),
        ]:
            response = await files.client.get(
                f"/api/v1/workspace-files/{endpoint}",
                params={
                    "path": entry["path"],
                    "workspaceBinding": entry["contentUrl"].split("workspaceBinding=", 1)[1],
                    **extra,
                },
                headers=headers,
            )
            assert response.status_code == status
    # A valid API credential from a remote peer is not proof of local ownership.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=files.app, client=("192.168.1.4", 1234)),
        base_url="http://testserver",
        headers=files.headers,
    ) as remote:
        assert (
            await remote.post("/api/v1/workspace-files/resolve", json={"paths": ["report.txt"]})
        ).status_code == 403
        assert (await remote.get(entry["contentUrl"])).status_code == 403
    # Auth-disabled remote/guest access is equally unable to read workspace files.
    files.config.auth.mode = "none"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=files.app, client=("192.168.1.4", 1234)),
        base_url="http://testserver",
        headers={"x-opensquilla-session-key": files.session.session_key},
    ) as guest:
        assert (await guest.get(entry["contentUrl"])).status_code == 403


async def test_same_name_different_session_and_stale_epoch_do_not_cross_bindings(files):
    (files.root / "same.txt").write_text("first")
    entry = (await resolve(files, ["same.txt"])).json()["files"][0]
    other = await files.manager.create("agent:main:webchat:other-files")
    (Path(other.execution_workspace["root"]) / "same.txt").write_text("second")
    headers = {"x-opensquilla-session-key": other.session_key}
    assert (await files.client.get(entry["contentUrl"], headers=headers)).status_code == 404
    other_entry = (await resolve(files, ["same.txt"], headers=headers)).json()["files"][0]
    assert (await files.client.get(other_entry["contentUrl"], headers=headers)).text == "second"
    await files.storage.upsert_session(
        files.session.model_copy(update={"epoch": files.session.epoch + 1})
    )
    assert (await files.client.get(entry["contentUrl"])).status_code == 404


async def test_workspace_rebinding_revocation_and_deletion_fail_closed(files, tmp_path):
    (files.root / "same.txt").write_text("old workspace")
    old = (await resolve(files, ["same.txt"])).json()["files"][0]
    project = tmp_path / "project"
    project.mkdir()
    (project / "same.txt").write_text("project")
    workspace = await files.storage.create_or_restore_project_workspace(
        path=str(project.resolve()),
        path_key=project_path_key(project, strict=True),
        display_name="project",
        trusted_at=1,
    )
    await files.storage.upsert_session(
        files.session.model_copy(update={"workspace_id": workspace.workspace_id})
    )
    assert (await files.client.get(old["contentUrl"])).status_code == 404
    current = (await resolve(files, ["same.txt"])).json()["files"][0]
    assert (await files.client.get(current["contentUrl"])).text == "project"
    await files.storage.remove_project_workspace(workspace.workspace_id)
    assert (await files.client.get(current["contentUrl"])).status_code == 404
    assert (await resolve(files, ["same.txt"])).status_code == 404


async def test_denied_reads_and_size_limits_are_rechecked(files, monkeypatch):
    (files.root / "denied.txt").write_text("hidden")
    (files.root / "large.bin").write_bytes(b"123456789")
    entry = (await resolve(files, ["denied.txt"])).json()["files"][0]
    files.config.sandbox.denied_read_globs = ["**/denied.txt"]
    assert (await resolve(files, ["denied.txt"])).json()["files"] == []
    assert (await files.client.get(entry["contentUrl"])).status_code == 404
    files.config.sandbox.denied_read_globs = []
    files.config.sandbox.denied_read_roots = [str(files.root / "denied.txt")]
    assert (await files.client.get(entry["contentUrl"])).status_code == 404
    monkeypatch.setattr(workspace_files, "MAX_WORKSPACE_FILE_BYTES", 8)
    assert (await resolve(files, ["large.bin"])).json()["files"] == []
    (files.root / "large.bin").unlink()
    (files.root / "directory").mkdir()
    assert (await resolve(files, ["large.bin", "directory"])).json()["files"] == []


async def test_revocation_during_read_discards_bytes(files, monkeypatch):
    (files.root / "report.txt").write_text("private")
    entry = (await resolve(files, ["report.txt"])).json()["files"][0]
    read = workspace_files._read_file
    loop = asyncio.get_running_loop()

    async def revoke():
        await files.storage.upsert_session(
            files.session.model_copy(update={"epoch": files.session.epoch + 1})
        )

    def racing_read(*args):
        result = read(*args)
        asyncio.run_coroutine_threadsafe(revoke(), loop).result(timeout=5)
        return result

    monkeypatch.setattr(workspace_files, "_read_file", racing_read)
    assert (await files.client.get(entry["contentUrl"])).status_code == 404


async def test_symlink_files_never_resolve(files):
    target = files.root / "real.txt"
    target.write_text("source")
    try:
        (files.root / "link.txt").symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    assert (await resolve(files, ["link.txt"])).json()["files"] == []


async def test_native_metadata_and_text_routes_reject_new_symlinks(files):
    target = files.root / "source.py"
    target.write_text("original\n")
    binding = (await resolve(files, ["source.py"])).json()["workspaceBinding"]
    replacement = files.root / "replacement.py"
    replacement.write_text("replacement\n")
    target.unlink()
    try:
        target.symlink_to(replacement)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    assert (await read_page(files, binding=binding)).status_code == 404
    params = {"path": "source.py", "workspaceBinding": binding}
    assert (
        await files.client.get(
            "/api/v1/workspace-files/search", params={**params, "query": "replacement"}
        )
    ).status_code == 404
    assert (
        await files.client.get(
            "/api/v1/workspace-files/metadata",
            params=params,
            headers=metadata_headers(files, path="source.py", binding=binding),
        )
    ).status_code == 404


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFO swapping regression")
async def test_page_fifo_replacement_cannot_block_open(files, monkeypatch):
    target = files.root / "source.py"
    target.write_text("original\n")
    binding = (await resolve(files, ["source.py"])).json()["workspaceBinding"]
    original_open = workspace_files.os.open

    def replace_before_open(path, flags, *args, **kwargs):
        if str(path) == str(native_io_path(target)):
            assert flags & os.O_NONBLOCK
            target.unlink()
            os.mkfifo(target)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(workspace_files.os, "open", replace_before_open)
    assert (await read_page(files, binding=binding)).status_code == 404


async def test_reparse_files_never_resolve(files, monkeypatch):
    target = files.root / "real.txt"
    target.write_text("source")
    original = workspace_files._is_reparse_point
    monkeypatch.setattr(
        workspace_files, "_is_reparse_point", lambda path: path == target or original(path)
    )
    assert (await resolve(files, ["real.txt"])).json()["files"] == []


async def test_resolve_confirms_os_read_permission_without_loading_contents(files, monkeypatch):
    target = files.root / "unreadable.txt"
    target.write_text("private")
    real_open = workspace_files.os.open

    def blocked_open(path, *args, **kwargs):
        if str(path) == str(native_io_path(target)):
            raise PermissionError("fixture denial")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(workspace_files.os, "open", blocked_open)
    assert (await resolve(files, ["unreadable.txt"])).json()["files"] == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX filesystem read bits")
async def test_unreadable_file_is_not_a_link(files):
    if os.geteuid() == 0:
        pytest.skip("root bypasses POSIX read bits")
    target = files.root / "unreadable.txt"
    target.write_text("private")
    target.chmod(0)
    try:
        assert (await resolve(files, ["unreadable.txt"])).json()["files"] == []
    finally:
        target.chmod(0o600)


async def test_read_does_not_initialize_user_grants_or_recreate_missing_workspace(
    files, monkeypatch
):
    from opensquilla.sandbox import run_context

    def unexpected_load():
        raise AssertionError("read endpoint must not initialize unrelated grant storage")

    monkeypatch.setattr(run_context, "load_user_grants_payload", unexpected_load)
    target = files.root / "report.txt"
    target.write_text("ready")
    entry = (await resolve(files, ["report.txt"])).json()["files"][0]
    assert (await files.client.get(entry["contentUrl"])).text == "ready"
    target.unlink()
    assert (await files.client.get(entry["contentUrl"])).status_code == 404
    files.root.rmdir()
    assert (await resolve(files, ["report.txt"])).status_code == 404
    assert not files.root.exists()


async def test_read_policy_revoked_during_read_discards_bytes(files, monkeypatch):
    (files.root / "report.txt").write_text("private")
    entry = (await resolve(files, ["report.txt"])).json()["files"][0]
    read = workspace_files._read_file

    def racing_read(*args):
        result = read(*args)
        files.config.sandbox.denied_read_globs = ["**/report.txt"]
        return result

    monkeypatch.setattr(workspace_files, "_read_file", racing_read)
    assert (await files.client.get(entry["contentUrl"])).status_code == 404


@pytest.mark.skipif(os.name != "nt", reason="Windows junction and long-path regression")
async def test_windows_junction_and_long_path(files, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_text("private")
    junction = files.root / "junction"
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)], check=True, capture_output=True
    )
    try:
        assert (await resolve(files, ["junction/private.txt"])).json()["files"] == []
    finally:
        junction.rmdir()
    target = files.root / ("long-directory-" * 10) / ("nested-directory-" * 8) / "report.svg"
    native_io_path(target.parent).mkdir(parents=True)
    native_io_path(target).write_text("<svg/>")
    assert len(str(target)) > 260
    entry = (await resolve(files, [str(target)])).json()["files"][0]
    assert (await files.client.get(entry["contentUrl"])).text == "<svg/>"


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"paths": "a.txt"},
        {"paths": [None]},
        {"paths": ["a.txt"] * 65},
        {"paths": ["a" * 4097]},
    ],
)
async def test_resolve_input_is_bounded(files, payload):
    assert (
        await files.client.post("/api/v1/workspace-files/resolve", json=payload)
    ).status_code == 400


async def test_resolve_rejects_large_body_and_content_requires_binding(files):
    (files.root / "report.txt").write_text("private")
    assert (
        await files.client.post("/api/v1/workspace-files/resolve", content=b"x" * 65537)
    ).status_code == 400
    assert (
        await files.client.get("/api/v1/workspace-files/content", params={"path": "report.txt"})
    ).status_code == 404
