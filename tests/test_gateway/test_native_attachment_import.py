"""Native capabilities use synthetic keys and real temporary file material only."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from starlette.applications import Starlette

from opensquilla.gateway import native_attachments
from opensquilla.gateway.config import AuthConfig, GatewayConfig
from opensquilla.gateway.uploads import UploadStore
from opensquilla.tools.types import ToolContext, WorkspaceAccessError

_TOKEN = "synthetic-native-test-token"
_NONCE = "synthetic-native-test-nonce"


class _Sessions:
    def __init__(self, workspace: Path, bound: bool = False) -> None:
        self.current = SimpleNamespace(
            session_id="synthetic-session",
            epoch=1,
            workspace_id=None,
            origin={},
            execution_workspace={
                "version": 1,
                "id": str(uuid4()),
                "kind": "configured",
                "root": str(workspace),
            }
            if bound
            else None,
        )

    async def get_session(self, key):
        return self.current


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, bound: bool = False):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    path = (workspace if bound else tmp_path) / "document.txt"
    path.write_bytes(b"Synthetic document content\n")
    manager = _Sessions(workspace, bound=bound)
    context = ToolContext(workspace_dir=str(workspace), run_mode="full", is_owner=True)
    config = GatewayConfig(auth=AuthConfig(mode="token", token=_TOKEN))
    store = UploadStore(tmp_path / "uploads")
    app = Starlette()
    app.state.desktop_gateway_ownership = SimpleNamespace(
        instance_id="synthetic-instance",
        instance_nonce=_NONCE,
    )

    async def resolve(config, sessions, key):
        if sessions.current is None:
            raise ValueError("selected file session is unavailable")
        return sessions.current, object(), context

    monkeypatch.setattr(native_attachments, "native_selection_context", resolve)
    native_attachments.register_native_attachment_routes(
        app,
        config=config,
        store=store,
        session_manager=manager,
    )
    info = path.stat()
    selection = {
        "v": 1,
        "id": str(uuid4()),
        "instanceId": "synthetic-instance",
        "sessionKey": "synthetic-key",
        "sessionId": manager.current.session_id,
        "sessionEpoch": manager.current.epoch,
        "senderId": 1,
        "path": str(path),
        "name": path.name,
        "mime": "text/plain",
        "size": info.st_size,
        "dev": str(info.st_dev),
        "ino": str(info.st_ino),
        "mtimeNs": str(info.st_mtime_ns),
        "ctimeNs": str(info.st_ctime_ns),
        "birthtimeNs": str(getattr(info, "st_birthtime_ns", 0)),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "expiresAt": time.time() * 1000 + 60_000,
    }
    return app, store, manager, context, path, selection


def test_windows_selection_uses_cross_runtime_birth_time(monkeypatch):
    info = SimpleNamespace(
        st_dev=1, st_ino=2, st_mtime_ns=300,
        st_ctime_ns=900, st_birthtime_ns=100, st_size=4,
    )
    monkeypatch.setattr(native_attachments, "_WINDOWS", True)
    assert native_attachments._selected_identity(info) == ("1", "2", "300", "100", "4")
    monkeypatch.setattr(native_attachments, "_WINDOWS", False)
    assert native_attachments._selected_identity(info) == ("1", "2", "300", "900", "4")


def _packet(selection, *, signature: str | None = None):
    encoded = base64.urlsafe_b64encode(json.dumps(selection).encode()).decode().rstrip("=")
    proof = hmac.new(
        _NONCE.encode(), native_attachments._SIGNING_CONTEXT + encoded.encode(), hashlib.sha256
    ).hexdigest()
    return {"selection": encoded}, {
        "Authorization": f"Bearer {_TOKEN}",
        "x-opensquilla-native-signature": proof if signature is None else signature,
    }


async def _post(app, selection, *, signature: str | None = None, headers=None):
    body, signed_headers = _packet(selection, signature=signature)
    signed_headers.update(headers or {})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://localhost",
    ) as client:
        return await client.post("/api/v1/files/native-import", json=body, headers=signed_headers)


@pytest.mark.asyncio
async def test_native_signed_selection_stages_exact_original_bytes(tmp_path, monkeypatch):
    app, store, _, _, path, selection = _setup(tmp_path, monkeypatch)
    response = await _post(app, selection)
    assert response.status_code == 200, response.text
    data = response.json()
    payload, metadata = await store.get(data["file_uuid"])
    assert payload == path.read_bytes()
    assert metadata["sha256"] == selection["sha256"]
    assert data["size"] == len(payload)


@pytest.mark.asyncio
async def test_native_project_file_returns_live_reference_without_upload(tmp_path, monkeypatch):
    app, store, manager, _, path, selection = _setup(tmp_path, monkeypatch, bound=True)
    response = await _post(app, selection)
    assert response.status_code == 200, response.text
    assert response.json()["workspaceFile"] == {
        "workspaceId": manager.current.execution_workspace["id"],
        "relativePath": path.name,
        "name": path.name,
        "mime": "text/plain",
        "size": path.stat().st_size,
    }
    assert not store._entries


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["signature", "auth", "origin", "expired", "future", "instance"])
async def test_native_rejects_forged_expired_or_foreign_selection(tmp_path, monkeypatch, reason):
    app, store, _, _, _, selection = _setup(tmp_path, monkeypatch)
    signature = None
    headers = {}
    if reason == "signature":
        signature = "0" * 64
    elif reason == "auth":
        headers["Authorization"] = "Bearer invalid-synthetic-token"
    elif reason == "origin":
        headers["Origin"] = "https://attacker.example.test"
    elif reason == "expired":
        selection["expiresAt"] = time.time() * 1000 - 1
    elif reason == "future":
        selection["expiresAt"] = time.time() * 1000 + 180_000
    else:
        selection["instanceId"] = "other-instance"
    response = await _post(app, selection, signature=signature, headers=headers)
    assert response.status_code in {403, 409}
    assert not store._entries


@pytest.mark.asyncio
async def test_native_capability_is_single_use(tmp_path, monkeypatch):
    app, store, _, _, _, selection = _setup(tmp_path, monkeypatch)
    assert (await _post(app, selection)).status_code == 200
    assert (await _post(app, selection)).status_code == 409
    assert len(store._entries) == 1


@pytest.mark.asyncio
async def test_native_denial_never_reads_or_stages_bytes(tmp_path, monkeypatch):
    app, store, _, _, _, selection = _setup(tmp_path, monkeypatch)

    async def deny(*args):
        raise PermissionError("effective filesystem policy denied")

    def forbidden(*args):
        pytest.fail("Denied native reads cannot fall back to a host copy")

    monkeypatch.setattr(native_attachments, "probe_file_access", deny)
    monkeypatch.setattr(native_attachments, "_selected_bytes", forbidden)
    assert (await _post(app, selection)).status_code == 403
    assert not store._entries


@pytest.mark.asyncio
async def test_native_strict_workspace_denial_returns_explicit_error(tmp_path, monkeypatch):
    app, store, _, _, _, selection = _setup(tmp_path, monkeypatch)

    async def deny(*args):
        raise WorkspaceAccessError("effective workspace policy denied")

    monkeypatch.setattr(native_attachments, "probe_file_access", deny)
    response = await _post(app, selection)
    assert response.status_code == 403, response.text
    assert not store._entries


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["contents", "metadata", "session", "gateway", "gateway_in_place"]
)
async def test_native_rechecks_file_and_in_place_session_binding(tmp_path, monkeypatch, change):
    app, store, manager, _, path, selection = _setup(tmp_path, monkeypatch)
    original = native_attachments._selected_bytes

    def mutate(*args):
        payload = original(*args)
        if change == "session":
            manager.current.epoch += 1
        elif change == "gateway_in_place":
            app.state.desktop_gateway_ownership.instance_id = "replacement"
        elif change == "gateway":
            app.state.desktop_gateway_ownership = SimpleNamespace(
                instance_id="replacement",
                instance_nonce=_NONCE,
            )
        return payload

    if change == "contents":
        path.write_bytes(b"Changed selected content\n")
    elif change == "metadata":
        selection["ino"] = "0"
    else:
        monkeypatch.setattr(native_attachments, "_selected_bytes", mutate)
    response = await _post(app, selection)
    assert response.status_code == 409, response.text
    assert not store._entries


@pytest.mark.asyncio
async def test_native_rechecks_binding_after_workspace_validation(tmp_path, monkeypatch):
    from opensquilla import workspace_files

    app, store, manager, _, _, selection = _setup(tmp_path, monkeypatch, bound=True)
    validate = workspace_files.validate_workspace_files

    async def mutate(*args, **kwargs):
        result = await validate(*args, **kwargs)
        manager.current.epoch += 1
        return result

    monkeypatch.setattr(workspace_files, "validate_workspace_files", mutate)
    response = await _post(app, selection)
    assert response.status_code == 409, response.text
    assert not store._entries


@pytest.mark.asyncio
async def test_native_rechecks_binding_after_staging_and_removes_lease(tmp_path, monkeypatch):
    app, store, manager, _, _, selection = _setup(tmp_path, monkeypatch)
    put = store.put_with_expiry

    async def mutate(*args, **kwargs):
        result = await put(*args, **kwargs)
        manager.current.origin["workspace_changed"] = True
        return result

    monkeypatch.setattr(store, "put_with_expiry", mutate)
    response = await _post(app, selection)
    assert response.status_code == 409, response.text
    assert not store._entries


def test_selected_bytes_rejects_symlink_substitution(tmp_path):
    target = tmp_path / "target.txt"
    target.write_bytes(b"original")
    alias = tmp_path / "alias.txt"
    try:
        alias.symlink_to(target)
    except OSError:
        pytest.skip("Host cannot create symlinks")
    with pytest.raises(ValueError, match="path changed"):
        native_attachments._selected_bytes({"path": str(alias)}, 1024)


@pytest.mark.asyncio
@pytest.mark.parametrize("binding", ["sessionId", "sessionEpoch"])
async def test_native_rejects_stale_signed_session_before_file_access(
    tmp_path, monkeypatch, binding
):
    app, store, _, _, _, selection = _setup(tmp_path, monkeypatch)
    selection[binding] = "other-session" if binding == "sessionId" else 0

    async def forbidden(*args):
        pytest.fail("A stale selection cannot probe or read a file for the new session")

    monkeypatch.setattr(native_attachments, "probe_file_access", forbidden)
    response = await _post(app, selection)
    assert response.status_code == 409, response.text
    assert not store._entries


@pytest.mark.asyncio
async def test_native_capability_body_limit_stops_consuming_request(tmp_path, monkeypatch):
    app, store, _, _, _, selection = _setup(tmp_path, monkeypatch)
    _, headers = _packet(selection)
    consumed = []

    async def body():
        consumed.append("oversize")
        yield b"x" * 16385
        consumed.append("unneeded-tail")
        yield b"tail"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://localhost",
    ) as client:
        response = await client.post("/api/v1/files/native-import", content=body(), headers=headers)
    assert response.status_code == 409, response.text
    assert consumed == ["oversize"]
    assert not store._entries


def test_selected_bytes_refuses_opened_descriptor_substitution(tmp_path, monkeypatch):
    import os

    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    info = first.stat()
    selection = {
        "path": str(first),
        "size": info.st_size,
        "dev": str(info.st_dev),
        "ino": str(info.st_ino),
        "mtimeNs": str(info.st_mtime_ns),
        "ctimeNs": str(info.st_ctime_ns),
        "birthtimeNs": str(getattr(info, "st_birthtime_ns", 0)),
        "sha256": hashlib.sha256(first.read_bytes()).hexdigest(),
    }
    real_open = os.open
    monkeypatch.setattr(native_attachments.os, "open", lambda path, flags: real_open(second, flags))
    with pytest.raises(ValueError, match="changed while opening"):
        native_attachments._selected_bytes(selection, 1024)
