from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

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
