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
    "content",
    [
        b"x" * (workspace_files.MAX_TEXT_LINE_BYTES + 1),
        (b"x" * (workspace_files.MAX_TEXT_LINE_BYTES - 1) + b"\n") * 5,
        ("文" * (workspace_files.MAX_TEXT_LINE_BYTES // 3 + 1)).encode(),
    ],
    ids=["oversized-line", "oversized-page", "oversized-utf8-line"],
)
async def test_page_has_byte_limits_for_lines_and_response(files, content):
    (files.root / "source.py").write_bytes(content)
    assert (await read_page(files)).status_code == 404


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
    assert response.json() == {"relativePath": "source.py", "totalLines": 502, "matchLine": 501}
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
        headers=metadata_headers(
            files, path=entry["path"], binding=resolved["workspaceBinding"]
        ),
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
            headers=metadata_headers(
                files, path="build.py", binding=resolved["workspaceBinding"]
            ),
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
    headers = metadata_headers(
        files, path="build.py", binding=resolved["workspaceBinding"]
    )
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
