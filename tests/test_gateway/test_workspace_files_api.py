"""Tests for the workspace file browsing endpoints (GET /api/v1/files*).

Covers: path normalization (traversal, absolute, dot-segments), workspace
resolution errors (missing/untrusted/removed), dot-entry and .gitignore
filtering, symlink escape rejection, content read (truncation, binary,
max_bytes cap), and token auth.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Unit tests: path normalization (no app needed)
# ---------------------------------------------------------------------------
from opensquilla.gateway.files_api import (  # noqa: E402
    WorkspacePathError,
    normalize_workspace_rel_path,
)


def test_normalize_empty_is_root() -> None:
    assert normalize_workspace_rel_path(None) == ""
    assert normalize_workspace_rel_path("") == ""
    assert normalize_workspace_rel_path(".") == ""
    assert normalize_workspace_rel_path("./") == ""


def test_normalize_collapses_dots_and_windows_separators() -> None:
    assert normalize_workspace_rel_path("src/./lib/../lib/a.ts") == "src/lib/a.ts"
    assert normalize_workspace_rel_path("src\\lib\\a.ts") == "src/lib/a.ts"
    assert normalize_workspace_rel_path("src//lib//a.ts") == "src/lib/a.ts"


def test_normalize_internal_dotdot_collapses() -> None:
    # Internal .. segments are legal and collapse; only escapes are rejected.
    assert normalize_workspace_rel_path("a/../b") == "b"
    assert normalize_workspace_rel_path("src/../lib/a.ts") == "lib/a.ts"


def test_normalize_rejects_traversal_and_absolute() -> None:
    with pytest.raises(WorkspacePathError):
        normalize_workspace_rel_path("..")
    with pytest.raises(WorkspacePathError):
        normalize_workspace_rel_path("/etc/passwd")
    with pytest.raises(WorkspacePathError):
        normalize_workspace_rel_path("src/../../x")


# ---------------------------------------------------------------------------
# HTTP tests
# ---------------------------------------------------------------------------


class _FakeWorkspaceStorage:
    """Minimal stand-in for session storage's project-workspace access."""

    def __init__(self, workspaces: dict[str, Any]) -> None:
        self._workspaces = workspaces

    async def get_project_workspace(self, workspace_id: str) -> Any:
        return self._workspaces.get(workspace_id)


class _FakeSessionManager:
    """Wraps the storage the way a real manager exposes it (``.storage``)."""

    def __init__(self, storage: _FakeWorkspaceStorage) -> None:
        self.storage = storage


class _FakeWorkspace:
    def __init__(self, workspace_id: str, path: str, path_key: str) -> None:
        self.workspace_id = workspace_id
        self.path = path
        self.path_key = path_key
        self.display_name = Path(path).name
        self.removed_at = None
        self.trusted_at = 1

    removed_at: int | None
    trusted_at: int | None


def _seed_workspace(tmp_path: Path) -> tuple[str, Path, str]:
    ws = tmp_path / "proj"
    ws.mkdir()
    (ws / "src").mkdir()
    (ws / "src" / "lib").mkdir()
    (ws / "src" / "lib" / "b.ts").write_text("const b = 1\n", encoding="utf-8")
    (ws / "src" / "z.ts").write_text("const z = 1\n", encoding="utf-8")
    (ws / "README.md").write_text("# proj\n", encoding="utf-8")
    (ws / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (ws / "notes.log").write_text("log line\n", encoding="utf-8")
    (ws / ".gitignore").write_text("*.log\n!keep.log\n", encoding="utf-8")
    (ws / "keep.log").write_text("kept\n", encoding="utf-8")
    (ws / "node_modules").mkdir()
    (ws / "node_modules" / "pkg.js").write_text("x\n", encoding="utf-8")
    # Hidden nested file that only shows if dot dirs were listed.
    (ws / ".git").mkdir()
    (ws / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    ws_id = "ws-test-1"
    # Must match what resolve_validated_project_workspace recomputes,
    # otherwise the canonical_changed check rejects the workspace.
    from opensquilla.project_workspaces import project_path_key

    path_key = project_path_key(ws, strict=True)
    return ws_id, ws, path_key


def _app_client(tmp_path: Path, config: Any | None = None, storage: Any | None = None):
    pytest.importorskip("starlette.testclient")
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from opensquilla.gateway.config import GatewayConfig
    from opensquilla.gateway.files_api import register_workspace_files_routes

    ws_id, ws, path_key = _seed_workspace(tmp_path)
    storage = storage or _FakeWorkspaceStorage(
        {ws_id: _FakeWorkspace(ws_id, str(ws), path_key)}
    )
    app = Starlette(debug=False)
    register_workspace_files_routes(
        app, config=config or GatewayConfig(), session_manager=_FakeSessionManager(storage)
    )
    return TestClient(app), ws_id


def test_list_root_excludes_dots_and_honors_gitignore(tmp_path: Path) -> None:
    client, ws_id = _app_client(tmp_path)
    response = client.get(f"/api/v1/files?workspace={ws_id}")
    assert response.status_code == 200
    body = response.json()
    names = [e["name"] for e in body["entries"]]
    # Directories sort before files; dot entries never appear.
    assert ".env" not in names
    assert ".git" not in names
    # node_modules is a dot entry: excluded. notes.log ignored; keep.log re-included.
    assert "node_modules" not in names
    assert "notes.log" not in names
    assert "keep.log" in names
    dirs = [e["name"] for e in body["entries"] if e["type"] == "directory"]
    files = [e["name"] for e in body["entries"] if e["type"] == "file"]
    assert dirs == ["src"]
    # Case-insensitive name sort: keep.log < README.md.
    assert files == ["keep.log", "README.md"]
    assert body["workspace"]["id"] == ws_id


def test_list_nested_dir(tmp_path: Path) -> None:
    client, ws_id = _app_client(tmp_path)
    response = client.get(f"/api/v1/files?workspace={ws_id}&path=src/lib")
    assert response.status_code == 200
    names = [e["name"] for e in response.json()["entries"]]
    assert names == ["b.ts"]


def test_list_rejects_traversal(tmp_path: Path) -> None:
    client, ws_id = _app_client(tmp_path)
    for bad in ("..", "src/../../..", "/etc"):
        response = client.get(f"/api/v1/files?workspace={ws_id}&path={bad}")
        assert response.status_code == 400, (bad, response.status_code)


def test_list_missing_workspace_404(tmp_path: Path) -> None:
    client, _ = _app_client(tmp_path)
    response = client.get("/api/v1/files?workspace=nope")
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"


def test_list_missing_path_404(tmp_path: Path) -> None:
    client, ws_id = _app_client(tmp_path)
    response = client.get(f"/api/v1/files?workspace={ws_id}&path=does-not-exist")
    assert response.status_code == 404


def test_list_requires_workspace_param(tmp_path: Path) -> None:
    client, _ = _app_client(tmp_path)
    response = client.get("/api/v1/files")
    assert response.status_code == 400


def test_content_reads_text(tmp_path: Path) -> None:
    client, ws_id = _app_client(tmp_path)
    response = client.get(
        f"/api/v1/files/content?workspace={ws_id}&path=src/lib/b.ts"
    )
    assert response.status_code == 200
    body = response.json()
    # Read back exactly what the filesystem holds (git autocrlf may have
    # rewritten the seeded newline to \r\n on Windows; read_bytes keeps
    # the raw line endings, unlike read_text's universal-newline rewrite).
    expected = (tmp_path / "proj" / "src" / "lib" / "b.ts").read_bytes().decode(
        "utf-8"
    )
    assert body["content"] == expected
    assert body["content"].startswith("const b = 1")
    assert body["binary"] is False
    assert body["truncated"] is False


def test_content_truncates_at_max_bytes(tmp_path: Path) -> None:
    client, ws_id = _app_client(tmp_path)
    response = client.get(
        f"/api/v1/files/content?workspace={ws_id}&path=README.md&max_bytes=3"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["content"] == "# p"
    assert body["truncated"] is True
    assert body["size"] == (tmp_path / "proj" / "README.md").stat().st_size


def test_content_rejects_oversized_max_bytes(tmp_path: Path) -> None:
    client, ws_id = _app_client(tmp_path)
    response = client.get(
        f"/api/v1/files/content?workspace={ws_id}&path=README.md&max_bytes=99999999"
    )
    assert response.status_code == 200
    assert response.json()["truncated"] is False


def test_content_rejects_binary(tmp_path: Path) -> None:
    client, ws_id = _app_client(tmp_path)
    (tmp_path / "proj" / "blob.bin").write_bytes(b"\x00\x01\x02PNG")
    response = client.get(
        f"/api/v1/files/content?workspace={ws_id}&path=blob.bin"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["binary"] is True
    assert body["content"] is None


def test_content_rejects_dotfile(tmp_path: Path) -> None:
    client, ws_id = _app_client(tmp_path)
    response = client.get(
        f"/api/v1/files/content?workspace={ws_id}&path=.env"
    )
    assert response.status_code == 400


def test_content_rejects_directory(tmp_path: Path) -> None:
    client, ws_id = _app_client(tmp_path)
    response = client.get(f"/api/v1/files/content?workspace={ws_id}&path=src")
    assert response.status_code == 404


def test_symlink_escape_rejected(tmp_path: Path) -> None:
    client, ws_id = _app_client(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret\n", encoding="utf-8")
    link = tmp_path / "proj" / "sneaky"
    os.symlink(secret, link)
    # Reading through the escaping symlink is rejected (400, not 200 with
    # the secret bytes).
    response = client.get(f"/api/v1/files/content?workspace={ws_id}&path=sneaky")
    assert response.status_code == 400
    # The entry itself may appear in the listing (a symlink is just an
    # entry); what matters is that its target is never served.
    listing = client.get(f"/api/v1/files?workspace={ws_id}").json()
    body = response.json()
    assert "top secret" not in str(body.get("content", ""))
    assert isinstance(listing.get("entries"), list)


def test_untrusted_workspace_409(tmp_path: Path) -> None:
    pytest.importorskip("starlette.testclient")
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from opensquilla.gateway.config import GatewayConfig
    from opensquilla.gateway.files_api import register_workspace_files_routes

    ws_id, ws, path_key = _seed_workspace(tmp_path)
    untrusted = _FakeWorkspace(ws_id, str(ws), path_key)
    untrusted.trusted_at = None
    app = Starlette(debug=False)
    register_workspace_files_routes(
        app,
        config=GatewayConfig(),
        session_manager=_FakeSessionManager(
            _FakeWorkspaceStorage({ws_id: untrusted})
        ),
    )
    with TestClient(app) as client:
        response = client.get(f"/api/v1/files?workspace={ws_id}")
    assert response.status_code == 409
    assert response.json()["code"] == "WORKSPACE_UNAVAILABLE"


def test_token_auth_enforced(tmp_path: Path) -> None:
    pytest.importorskip("starlette.testclient")
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from opensquilla.gateway.config import GatewayConfig
    from opensquilla.gateway.files_api import register_workspace_files_routes

    ws_id, ws, path_key = _seed_workspace(tmp_path)
    config = GatewayConfig()
    config.auth.mode = "token"
    config.auth.token = "sekret"
    app = Starlette(debug=False)
    register_workspace_files_routes(
        app,
        config=config,
        session_manager=_FakeSessionManager(
            _FakeWorkspaceStorage({ws_id: _FakeWorkspace(ws_id, str(ws), path_key)})
        ),
    )
    with TestClient(app) as client:
        anon = client.get(f"/api/v1/files?workspace={ws_id}")
        wrong = client.get(
            f"/api/v1/files?workspace={ws_id}",
            headers={"Authorization": "Bearer wrong"},
        )
        right = client.get(
            f"/api/v1/files?workspace={ws_id}",
            headers={"Authorization": "Bearer sekret"},
        )
    assert anon.status_code == 401
    assert wrong.status_code == 401
    assert right.status_code == 200


def test_content_memory_error_returns_500_not_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A MemoryError inside the read must map to a 500, not kill the server."""
    client, ws_id = _app_client(tmp_path)

    def _boom(root: Any, rel: str, max_bytes: int) -> Any:
        raise MemoryError

    monkeypatch.setattr(
        "opensquilla.gateway.files_api._read_content_blocking", _boom
    )
    response = client.get(
        f"/api/v1/files/content?workspace={ws_id}&path=README.md"
    )
    assert response.status_code == 500
    assert response.json()["code"] == "READ_FAILED"


def test_list_memory_error_returns_500_not_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, ws_id = _app_client(tmp_path)

    def _boom(root: Any, rel: str) -> Any:
        raise MemoryError

    monkeypatch.setattr(
        "opensquilla.gateway.files_api._list_dir_blocking", _boom
    )
    response = client.get(f"/api/v1/files?workspace={ws_id}")
    assert response.status_code == 500
    assert response.json()["code"] == "LIST_FAILED"
