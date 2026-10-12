from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.attachment_refs import write_transcript_material


class _FakeSessionManager:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id

    async def get_session(self, session_key: str) -> object | None:
        if session_key == "agent:main:webchat:ok":
            return SimpleNamespace(session_id=self.session_id)
        return None


def _app(tmp_path: Path):
    pytest.importorskip("starlette.testclient")
    from starlette.applications import Starlette

    from opensquilla.gateway.attachments import register_attachment_routes
    from opensquilla.gateway.config import AttachmentsConfig, AuthConfig, GatewayConfig
    from opensquilla.gateway.middleware import AuthMiddleware

    config = GatewayConfig(
        auth=AuthConfig(mode="token", token="secret"),
        attachments=AttachmentsConfig(media_root=str(tmp_path)),
    )
    app = Starlette(debug=False)
    register_attachment_routes(
        app,
        config=config,
        session_manager=_FakeSessionManager("session-1"),
    )
    app.add_middleware(AuthMiddleware, config=config)
    return app


@pytest.mark.parametrize("preview", [False, True])
def test_transcript_attachment_download_requires_auth_and_session_scope(
    tmp_path: Path, preview: bool,
) -> None:
    pytest.importorskip("starlette.testclient")
    from starlette.testclient import TestClient

    sha, _path, _wrote = write_transcript_material(
        media_root=tmp_path,
        session_id="session-1",
        payload=b"hello attachment",
    )

    with TestClient(_app(tmp_path)) as client:
        suffix = "&preview=1" if preview else ""
        unauthenticated = client.get(
            f"/api/v1/attachments/{sha}?sessionKey=agent:main:webchat:ok{suffix}"
        )
        wrong_session = client.get(
            f"/api/v1/attachments/{sha}?sessionKey=agent:main:webchat:other{suffix}",
            headers={"Authorization": "Bearer secret"},
        )
        missing_session = client.get(
            f"/api/v1/attachments/{sha}" + ("?preview=1" if preview else ""),
            headers={"Authorization": "Bearer secret"},
        )

    assert unauthenticated.status_code == 401
    assert wrong_session.status_code == 404
    assert missing_session.status_code == 404


def test_transcript_attachment_download_serves_file_response(tmp_path: Path) -> None:
    pytest.importorskip("starlette.testclient")
    from starlette.testclient import TestClient

    sha, _path, _wrote = write_transcript_material(
        media_root=tmp_path,
        session_id="session-1",
        payload=b"hello attachment",
    )

    with TestClient(_app(tmp_path)) as client:
        response = client.get(
            (
                f"/api/v1/attachments/{sha}?sessionKey=agent:main:webchat:ok"
                "&name=note%20final.txt&mime=text%2Fplain"
            ),
            headers={"Authorization": "Bearer secret"},
        )

    assert response.status_code == 200
    assert response.content == b"hello attachment"
    assert response.headers["content-type"].startswith("text/plain")
    assert "attachment" in response.headers["content-disposition"]
    assert "note%20final.txt" in response.headers["content-disposition"]


@pytest.mark.parametrize("preview", [False, True])
def test_transcript_attachment_download_reports_missing_and_integrity_errors(
    tmp_path: Path, preview: bool,
) -> None:
    pytest.importorskip("starlette.testclient")
    from starlette.testclient import TestClient

    sha, path, _wrote = write_transcript_material(
        media_root=tmp_path,
        session_id="session-1",
        payload=b"hello attachment",
    )
    path.write_bytes(b"tampered")

    with TestClient(_app(tmp_path)) as client:
        suffix = "&preview=1" if preview else ""
        missing = client.get(
            "/api/v1/attachments/" + ("0" * 64) + "?sessionKey=agent:main:webchat:ok" + suffix,
            headers={"Authorization": "Bearer secret"},
        )
        mismatch = client.get(
            f"/api/v1/attachments/{sha}?sessionKey=agent:main:webchat:ok{suffix}",
            headers={"Authorization": "Bearer secret"},
        )

    assert missing.status_code == 404
    assert mismatch.status_code == 409


@pytest.mark.parametrize("payload,expected,truncated", [
    (b"", "", False),
    ("\ufefffirst\r\nsecond\r\n".encode(), "\ufefffirst\r\nsecond\r\n", False),
    (b"x" * 8192, "x" * 8192, False),
    (b"x" * 8192 + b"tail", "x" * 8192, True),
    (("x" * 8190 + "🦐tail").encode(), "x" * 8190, True),
    (b"x" * 8191 + b"\r\ntail", "x" * 8191, True),
    (b"a\r\n" * 1001, "a\r\n" * 1000, True),
    (b"a\r" * 1001, "a\r" * 1000, True),
    ((b"x" * 4095 + b"\n") * 65, ("x" * 4095 + "\n") * 64, True),
], ids=["empty", "literal-bom-crlf", "exact-line-cap", "line-cap", "utf8-boundary",
        "crlf-boundary", "line-count", "cr-line-count", "total-byte-cap"])
def test_attachment_text_preview_is_bounded_and_literal(
    tmp_path: Path, payload: bytes, expected: str, truncated: bool,
) -> None:
    from starlette.testclient import TestClient

    sha, _path, _wrote = write_transcript_material(
        media_root=tmp_path, session_id="session-1", payload=payload,
    )
    with TestClient(_app(tmp_path)) as client:
        response = client.get(
            f"/api/v1/attachments/{sha}?sessionKey=agent:main:webchat:ok&preview=1",
            headers={"Authorization": "Bearer secret"},
        )
    assert response.status_code == 200
    assert response.json() == {
        "content": expected, "truncated": truncated, "totalBytes": len(payload),
    }
    assert len(response.json()["content"].encode("utf-8")) <= 256 * 1024


def test_attachment_text_preview_rejects_invalid_utf8(tmp_path: Path) -> None:
    from starlette.testclient import TestClient

    sha, _path, _wrote = write_transcript_material(
        media_root=tmp_path, session_id="session-1", payload=b"bad\xfftext",
    )
    with TestClient(_app(tmp_path)) as client:
        response = client.get(
            f"/api/v1/attachments/{sha}?sessionKey=agent:main:webchat:ok&preview=1",
            headers={"Authorization": "Bearer secret"},
        )
    assert response.status_code == 415
    assert response.json()["code"] == "UNSUPPORTED_MEDIA_TYPE"


async def test_attachment_integrity_hash_is_chunked_and_off_event_loop(
    tmp_path: Path, monkeypatch,
) -> None:
    import threading

    from opensquilla.application.artifact_workbench import AttachmentContentQuery
    from opensquilla.gateway.adapters import artifact_content
    from opensquilla.gateway.config import AttachmentsConfig, GatewayConfig

    payload = b"x" * (3 * 1024 * 1024) + b"last byte"
    sha, _path, _wrote = write_transcript_material(
        media_root=tmp_path, session_id="session-1", payload=payload,
    )
    main_thread = threading.get_ident()
    original_verify = artifact_content._verify_attachment_material
    verified = []

    def verify_in_worker(path, expected_sha):
        assert threading.get_ident() != main_thread
        original_verify(path, expected_sha)
        verified.append(expected_sha)

    def forbid_full_read(*args, **kwargs):
        raise AssertionError("attachment integrity must not read the whole file into memory")

    monkeypatch.setattr(artifact_content, "_verify_attachment_material", verify_in_worker)
    monkeypatch.setattr(Path, "read_bytes", forbid_full_read)
    port = artifact_content.GatewayArtifactContentPort(
        GatewayConfig(attachments=AttachmentsConfig(media_root=str(tmp_path))),
        session_manager=_FakeSessionManager("session-1"),
    )
    material = await port.attachment_content(AttachmentContentQuery("agent:main:webchat:ok", sha))
    assert material.path.stat().st_size == len(payload)
    assert verified == [sha]


@pytest.mark.parametrize("replacement_id", ["session-2", ""])
async def test_attachment_integrity_read_rechecks_owning_session(
    tmp_path: Path, monkeypatch, replacement_id: str,
) -> None:
    import asyncio
    import threading

    from opensquilla.application.artifact_workbench import (
        AttachmentContentQuery,
        ContentNotFoundError,
    )
    from opensquilla.gateway.adapters import artifact_content
    from opensquilla.gateway.config import AttachmentsConfig, GatewayConfig

    sha, _path, _wrote = write_transcript_material(
        media_root=tmp_path, session_id="session-1", payload=b"old session content",
    )
    manager = _FakeSessionManager("session-1")
    port = artifact_content.GatewayArtifactContentPort(
        GatewayConfig(attachments=AttachmentsConfig(media_root=str(tmp_path))),
        session_manager=manager,
    )
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()
    original_verify = artifact_content._verify_attachment_material

    def paused_verify(path, expected_sha):
        loop.call_soon_threadsafe(started.set)
        if not release.wait(timeout=5):
            raise AssertionError("test did not release attachment integrity worker")
        original_verify(path, expected_sha)

    monkeypatch.setattr(artifact_content, "_verify_attachment_material", paused_verify)
    pending = asyncio.create_task(
        port.attachment_content(AttachmentContentQuery("agent:main:webchat:ok", sha))
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        manager.session_id = replacement_id
    finally:
        release.set()
    with pytest.raises(ContentNotFoundError, match="attachment session changed"):
        await pending
