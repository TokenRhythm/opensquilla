"""Tests for the bridge upload endpoint + store.

These tests cover the core upload mechanics: the in-memory store with
per-uuid asyncio.Lock + TTL sweep + ``.meta`` marker, the multipart
``POST /api/v1/files/upload`` route with auth, the validator's ``file_uuid``
resolution path, and the query-token rejection surface for multipart uploads.

Runtime/turn-runner integration has separate coverage for the explicit eviction
hook, concurrent resolution serialization, sweep-during-resolution lock-skip,
and cross-origin/wrong-scope variants.
"""

from __future__ import annotations

import asyncio
import gc
import hashlib
import json
import time
from pathlib import Path
from typing import Any

import pytest

from opensquilla.contracts.attachments import MAX_STAGED_TEXT_BYTES
from opensquilla.gateway.attachment_ingest import (
    IMAGE_ATTACHMENT_BYTES,
    MAX_ATTACHMENT_BYTES,
    MAX_STAGED_PDF_BYTES,
    MAX_TOTAL_ATTACHMENT_BYTES,
    TEXT_ATTACHMENT_BYTES,
    resolve_attachments,
    validate_attachments,
)
from opensquilla.gateway.uploads import (
    AttachmentLostInRestartError,
    AttachmentNotFoundError,
    UploadOversizeError,
    UploadStore,
    UploadStoreFullError,
    UploadUnsupportedMimeError,
)

# ---------------------------------------------------------------------------
# Direct unit tests against UploadStore (no network).
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> UploadStore:
    return UploadStore(
        marker_dir=tmp_path / "inbound",
        ttl_seconds=600,
        max_file_bytes=30 * 1024 * 1024,
    )


class _FakeUploadStore:
    def __init__(self, entries: dict[str, tuple[bytes, dict[str, Any]]]) -> None:
        self.entries = entries

    async def get(self, file_uuid: str) -> tuple[bytes, dict[str, Any]]:
        return self.entries[file_uuid]


def _exact_pdf(size: int) -> bytes:
    header = b"%PDF-1.4\n"
    return header + b"a" * (size - len(header))


def _exact_png(size: int) -> bytes:
    header = b"\x89PNG\r\n\x1a\n"
    return header + b"a" * (size - len(header))


def test_upload_round_trip(store: UploadStore) -> None:
    """put + get returns the same bytes; the file_uuid is opaque."""
    payload = b"%PDF-1.4\nbody\n"
    file_uuid = asyncio.run(store.put("r.pdf", "application/pdf", payload))
    assert isinstance(file_uuid, str) and len(file_uuid) > 8

    bytes_out, meta = asyncio.run(store.get(file_uuid))
    assert bytes_out == payload
    assert meta["mime"] == "application/pdf"
    assert meta["name"] == "r.pdf"
    assert meta["size"] == len(payload)


def test_upload_too_large_30mb_plus_rejected(store: UploadStore) -> None:
    # 30 MB + 1 byte exceeds the locked cap.
    too_big = b"%PDF-1.4\n" + b"a" * MAX_STAGED_PDF_BYTES
    with pytest.raises(UploadOversizeError):
        asyncio.run(store.put("big.pdf", "application/pdf", too_big))


def test_upload_text_family_uses_staged_text_cap(store: UploadStore) -> None:
    ok_uuid = asyncio.run(store.put("ok.txt", "text/plain", b"a" * (TEXT_ATTACHMENT_BYTES + 1)))
    assert ok_uuid.startswith("u-")

    with pytest.raises(UploadOversizeError):
        asyncio.run(
            store.put("too-large.txt", "text/plain", b"a" * (MAX_STAGED_TEXT_BYTES + 1))
        )


def test_upload_image_uses_five_mib_cap(store: UploadStore) -> None:
    ok_uuid = asyncio.run(store.put("ok.png", "image/png", _exact_png(IMAGE_ATTACHMENT_BYTES)))
    assert ok_uuid.startswith("u-")

    with pytest.raises(UploadOversizeError):
        asyncio.run(store.put("too-large.png", "image/png", _exact_png(IMAGE_ATTACHMENT_BYTES + 1)))


def test_upload_pdf_accepts_exact_staged_cap(store: UploadStore) -> None:
    ok_uuid = asyncio.run(
        store.put("ok.pdf", "application/pdf", _exact_pdf(MAX_STAGED_PDF_BYTES))
    )
    assert ok_uuid.startswith("u-")


def test_upload_normalizes_content_type_parameters(store: UploadStore) -> None:
    file_uuid = asyncio.run(
        store.put("ok.txt", "text/plain; charset=utf-8", b"a" * TEXT_ATTACHMENT_BYTES)
    )

    _payload, meta = asyncio.run(store.get(file_uuid))
    assert meta["mime"] == "text/plain"


def test_upload_store_accepts_opaque_mime(store: UploadStore) -> None:
    # Any normalizable mime stages; opaque bytes are workspace-only downstream.
    file_uuid = asyncio.run(store.put("x.sh", "application/x-shellscript", b"#!/bin/sh\n"))
    assert file_uuid.startswith("u-")
    _payload, meta = asyncio.run(store.get(file_uuid))
    assert meta["mime"] == "application/x-shellscript"


def test_upload_store_rejects_opaque_mime_when_admission_disabled(tmp_path: Path) -> None:
    # accept_opaque=False keeps the store as the legacy fail-closed second
    # layer so a disallowed MIME never lands in memory or on disk.
    strict = UploadStore(
        marker_dir=tmp_path / "strict-inbound",
        ttl_seconds=600,
        max_file_bytes=30 * 1024 * 1024,
        accept_opaque=False,
    )
    with pytest.raises(UploadUnsupportedMimeError):
        asyncio.run(strict.put("x.sh", "application/x-shellscript", b"#!/bin/sh\n"))


def test_upload_store_rejects_unnormalizable_mime(store: UploadStore) -> None:
    with pytest.raises(UploadUnsupportedMimeError):
        asyncio.run(store.put("x.bin", "   ", b"\x00\x01"))


def test_failed_upload_does_not_leak_uuid(store: UploadStore) -> None:
    """Validation failures inside put() must not leave a half-written entry.

    Specifically: if the store rejects on size or MIME, no marker file is
    written and no uuid is reachable.
    """
    initial_markers = list((store.marker_dir).glob("*.meta")) if store.marker_dir.exists() else []
    with pytest.raises(UploadOversizeError):
        asyncio.run(
            store.put(
                "big.pdf",
                "application/pdf",
                b"%PDF-1.4\n" + b"a" * (30 * 1024 * 1024 + 1),
            )
        )
    final_markers = list((store.marker_dir).glob("*.meta")) if store.marker_dir.exists() else []
    assert final_markers == initial_markers


def test_get_miss_does_not_leak_locks(store: UploadStore) -> None:
    """Unknown/expired uuid lookups must not grow the per-uuid lock map."""

    async def _run() -> None:
        for i in range(200):
            with pytest.raises(AttachmentNotFoundError):
                await store.get(f"bogus-{i}")

    asyncio.run(_run())
    gc.collect()
    assert len(store._entries) == 0
    assert len(store._locks) == 0


def test_uuid_evicted_after_send_success(store: UploadStore) -> None:
    """The store's evict() drops the entry; subsequent get raises NotFound.

    The explicit eviction hook lives in rpc_sessions._handle_sessions_send:
    after start_turn_via_runtime accepts the turn, every consumed uuid is
    evict()ed. This unit test locks the store contract that backs the
    integration.
    """

    file_uuid = asyncio.run(store.put("r.pdf", "application/pdf", b"%PDF-1.4\n"))
    # Round-trip works pre-evict.
    asyncio.run(store.get(file_uuid))
    existed = asyncio.run(store.evict(file_uuid))
    assert existed is True
    with pytest.raises(AttachmentNotFoundError):
        asyncio.run(store.get(file_uuid))
    # Idempotent: second evict reports the entry was already gone.
    assert asyncio.run(store.evict(file_uuid)) is False


def test_uuid_not_evicted_when_chat_send_rejected_post_resolution(
    store: UploadStore,
) -> None:
    """Locked semantic: a turn rejected post-resolution does NOT evict.

    Verified at the store level: evict is the explicit call site; the
    rpc_sessions handler omits it on every error path. Here we assert
    that *not calling* evict leaves the entry resolvable until TTL.
    """

    file_uuid = asyncio.run(store.put("r.pdf", "application/pdf", b"%PDF-1.4\n"))
    # Simulate "turn rejected" — the handler simply does NOT call evict.
    # The entry must still be reachable so the user can retry.
    payload, _meta = asyncio.run(store.get(file_uuid))
    assert payload == b"%PDF-1.4\n"


def test_unknown_uuid_rejected(store: UploadStore) -> None:
    with pytest.raises(AttachmentNotFoundError):
        asyncio.run(store.get("u-doesnotexist"))


def test_file_uuid_resolved_via_store_returns_material_ref(
    store: UploadStore,
    tmp_path: Path,
) -> None:
    """validate -> resolve -> content-addressed material ref.

    The validator accepts the ``{file_uuid, mime, name}`` envelope and the
    resolver materialises it via the upload store. Runtime receives a stable
    material ref; it must not carry the upload uuid or long-lived base64 data.
    """

    pdf = b"%PDF-1.4\nbody\n"
    file_uuid = asyncio.run(store.put("r.pdf", "application/pdf", pdf))

    validated, _failures = validate_attachments(
        [{"file_uuid": file_uuid, "mime": "application/pdf", "name": "r.pdf"}]
    )
    resolved, consumed = asyncio.run(
        resolve_attachments(
            validated,
            store=store,
            material_root=tmp_path,
            session_id="s1",
        )
    )
    assert consumed == [file_uuid]
    assert len(resolved) == 1
    item = resolved[0]
    assert "file_uuid" not in item
    assert "data" not in item
    assert item["kind"] == "attachment_ref"
    assert item["type"] == "application/pdf"
    assert item["mime"] == "application/pdf"
    assert item["name"] == "r.pdf"
    assert item["size"] == len(pdf)
    sha = hashlib.sha256(pdf).hexdigest()
    assert item["sha256"] == sha
    assert item["material_id"] == sha
    assert item["store"] == "transcript"
    assert item["scope"] == "s1"
    assert (tmp_path / "transcripts" / "s1" / sha).read_bytes() == pdf


def test_file_uuid_resolution_requires_material_target(store: UploadStore) -> None:
    file_uuid = asyncio.run(store.put("r.pdf", "application/pdf", b"%PDF-1.4\nbody\n"))
    validated, _failures = validate_attachments(
        [{"file_uuid": file_uuid, "mime": "application/pdf", "name": "r.pdf"}]
    )

    with pytest.raises(ValueError, match="material target"):
        asyncio.run(resolve_attachments(validated, store=store))


def test_file_uuid_resolution_revalidates_mime_from_staged_bytes(
    store: UploadStore,
    tmp_path: Path,
) -> None:
    pdf = b"%PDF-1.4\nbody\n"
    file_uuid = asyncio.run(store.put("misnamed.txt", "text/plain", pdf))

    validated, _failures = validate_attachments(
        [{"file_uuid": file_uuid, "mime": "text/plain", "name": "misnamed.txt"}]
    )
    resolved, consumed = asyncio.run(
        resolve_attachments(
            validated,
            store=store,
            material_root=tmp_path,
            session_id="s1",
        )
    )

    assert consumed == [file_uuid]
    item = resolved[0]
    assert item["type"] == "application/pdf"
    assert item["mime"] == "application/pdf"
    assert item["_was_staged"] is True
    assert "data" not in item


def test_file_uuid_resolution_allows_large_staged_pdf(
    store: UploadStore,
    tmp_path: Path,
) -> None:
    pdf = b"%PDF-1.4\n" + b"a" * (MAX_ATTACHMENT_BYTES + 1)
    assert len(pdf) < MAX_STAGED_PDF_BYTES
    file_uuid = asyncio.run(store.put("large.pdf", "application/pdf", pdf))

    validated, _failures = validate_attachments(
        [{"file_uuid": file_uuid, "mime": "application/pdf", "name": "large.pdf"}]
    )
    resolved, consumed = asyncio.run(
        resolve_attachments(
            validated,
            store=store,
            material_root=tmp_path,
            session_id="s1",
        )
    )

    assert consumed == [file_uuid]
    item = resolved[0]
    assert item["type"] == "application/pdf"
    assert item["_was_staged"] is True
    assert item["size"] == len(pdf)
    assert item["sha256"] == hashlib.sha256(pdf).hexdigest()
    assert "data" not in item


def test_file_uuid_resolution_rejects_large_staged_image() -> None:
    payload = _exact_png(IMAGE_ATTACHMENT_BYTES + 1)
    store = _FakeUploadStore(
        {
            "u-large-image": (
                payload,
                {
                    "mime": "image/png",
                    "name": "large.png",
                    "sha256": "x",
                    "size": len(payload),
                },
            )
        }
    )

    validated, _failures = validate_attachments(
        [{"file_uuid": "u-large-image", "mime": "image/png", "name": "large.png"}]
    )
    with pytest.raises(ValueError, match="exceeds"):
        asyncio.run(
            resolve_attachments(
                validated,
                store=store,
                material_root=Path.cwd(),
                session_id="s1",
            )
        )


def test_file_uuid_resolution_accepts_staged_text_above_inline_threshold(
    tmp_path: Path,
) -> None:
    # Text is stageable: a clean-UTF-8 text file above the 2MB inline threshold
    # resolves instead of dead-ending (the inline cap only bounds inline sends).
    payload = b"a" * (TEXT_ATTACHMENT_BYTES + 1)
    store = _FakeUploadStore(
        {
            "u-large-text": (
                payload,
                {
                    "mime": "text/csv",
                    "name": "large.csv",
                    "sha256": "x",
                    "size": len(payload),
                },
            )
        }
    )

    validated, _failures = validate_attachments(
        [{"file_uuid": "u-large-text", "mime": "text/csv", "name": "large.csv"}]
    )
    resolved, consumed = asyncio.run(
        resolve_attachments(
            validated,
            store=store,
            material_root=tmp_path,
            session_id="s1",
        )
    )
    assert consumed == ["u-large-text"]
    assert resolved[0]["kind"] == "attachment_ref"
    assert resolved[0]["mime"] == "text/csv"
    assert resolved[0]["size"] == len(payload)


def test_file_uuid_resolution_rejects_aggregate_raw_bytes_above_cap(tmp_path: Path) -> None:
    one_pdf = _exact_pdf(MAX_TOTAL_ATTACHMENT_BYTES // 3 + 1)
    assert len(one_pdf) < MAX_STAGED_PDF_BYTES
    entries = {
        f"u-pdf-{index}": (
            one_pdf,
            {
                "mime": "application/pdf",
                "name": f"{index}.pdf",
                "sha256": "x",
                "size": len(one_pdf),
            },
        )
        for index in range(3)
    }
    store = _FakeUploadStore(entries)
    validated, _failures = validate_attachments(
        [
            {"file_uuid": file_uuid, "mime": "application/pdf", "name": meta["name"]}
            for file_uuid, (_payload, meta) in entries.items()
        ]
    )

    with pytest.raises(ValueError, match="total raw bytes"):
        asyncio.run(
            resolve_attachments(
                validated,
                store=store,
                material_root=tmp_path,
                session_id="s1",
            )
        )


def test_uuid_expires_after_ttl(tmp_path: Path) -> None:
    """Once the TTL elapses, get() raises AttachmentNotFoundError."""
    short_lived = UploadStore(
        marker_dir=tmp_path / "inbound",
        ttl_seconds=0.01,
        max_file_bytes=30 * 1024 * 1024,
    )
    file_uuid = asyncio.run(short_lived.put("x.txt", "text/plain", b"hi"))
    time.sleep(0.05)
    with pytest.raises(AttachmentNotFoundError):
        asyncio.run(short_lived.get(file_uuid))


def test_post_restart_returns_specific_error_for_lost_uuid(tmp_path: Path) -> None:
    """A ``.meta`` marker from a prior process produces a specific error.

    When an in-memory restart drops the bytes but leaves the marker on disk,
    get() raises AttachmentLostInRestartError rather than the generic
    AttachmentNotFoundError so the client can show "uploaded file lost in
    restart, please re-upload" instead of "unknown".
    """
    marker_dir = tmp_path / "inbound"
    marker_dir.mkdir(parents=True, exist_ok=True)

    # Hand-craft a marker as if a prior process had inserted it, then
    # construct a fresh store (simulating the restart) and ask for the uuid.
    file_uuid = "u-restart-1234"
    marker = marker_dir / f"{file_uuid}.meta"
    marker.write_text(
        json.dumps(
            {
                "sha256": "deadbeef" * 8,
                "mime": "application/pdf",
                "name": "lost.pdf",
                "size": 12345,
                "expires_at": time.time() + 600,
            }
        ),
        encoding="utf-8",
    )

    fresh = UploadStore(marker_dir=marker_dir, ttl_seconds=600, max_file_bytes=30 * 1024 * 1024)
    with pytest.raises(AttachmentLostInRestartError):
        asyncio.run(fresh.get(file_uuid))


def test_post_restart_expired_marker_is_not_reported_as_lost(tmp_path: Path) -> None:
    """Expired restart markers are treated as gone and cleaned up."""
    marker_dir = tmp_path / "inbound"
    marker_dir.mkdir(parents=True, exist_ok=True)

    file_uuid = "u-expired-restart"
    marker = marker_dir / f"{file_uuid}.meta"
    marker.write_text(
        json.dumps(
            {
                "sha256": "deadbeef" * 8,
                "mime": "text/plain",
                "name": "old.txt",
                "size": 3,
                "expires_at": time.time() - 1,
            }
        ),
        encoding="utf-8",
    )

    fresh = UploadStore(marker_dir=marker_dir, ttl_seconds=600, max_file_bytes=30 * 1024 * 1024)
    with pytest.raises(AttachmentNotFoundError):
        asyncio.run(fresh.get(file_uuid))
    assert not marker.exists()


# ---------------------------------------------------------------------------
# HTTP-layer security tests.
# ---------------------------------------------------------------------------


def test_upload_route_accepts_text_above_inline_threshold() -> None:
    # The staged path exists precisely so text larger than the inline threshold
    # can upload; only the staged text ceiling rejects.
    pytest.importorskip("starlette.testclient")
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from opensquilla.gateway.config import GatewayConfig
    from opensquilla.gateway.uploads import UploadStore, register_upload_routes

    store = UploadStore(marker_dir=None, ttl_seconds=600, max_file_bytes=30 * 1024 * 1024)
    app = Starlette(debug=False)
    register_upload_routes(app, config=GatewayConfig(), store=store)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/files/upload",
            files={"file": ("big.txt", b"a" * (TEXT_ATTACHMENT_BYTES + 1), "text/plain")},
        )

    assert response.status_code == 200
    assert response.json()["mime"] == "text/plain"


def test_upload_route_rejects_text_above_staged_ceiling() -> None:
    pytest.importorskip("starlette.testclient")
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from opensquilla.gateway.config import GatewayConfig
    from opensquilla.gateway.uploads import UploadStore, register_upload_routes

    store = UploadStore(
        marker_dir=None, ttl_seconds=600, max_file_bytes=MAX_STAGED_TEXT_BYTES + 1024
    )
    app = Starlette(debug=False)
    register_upload_routes(app, config=GatewayConfig(), store=store)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/files/upload",
            files={"file": ("big.txt", b"a" * (MAX_STAGED_TEXT_BYTES + 1), "text/plain")},
        )

    assert response.status_code == 413
    assert response.json()["code"] == "TOO_LARGE"


def _route_client(config=None, store=None):
    pytest.importorskip("starlette.testclient")
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from opensquilla.gateway.config import GatewayConfig
    from opensquilla.gateway.uploads import UploadStore, register_upload_routes

    store = store or UploadStore(marker_dir=None, ttl_seconds=600, max_file_bytes=30 * 1024 * 1024)
    app = Starlette(debug=False)
    register_upload_routes(app, config=config or GatewayConfig(), store=store)
    return TestClient(app)


def _zip_bytes() -> bytes:
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("paper/main.tex", "\\documentclass{article}")
    return buffer.getvalue()


def test_upload_route_accepts_zip_as_opaque() -> None:
    with _route_client() as client:
        response = client.post(
            "/api/v1/files/upload",
            files={"file": ("paper.zip", _zip_bytes(), "application/zip")},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["mime"] == "application/zip"
    assert body["file_uuid"].startswith("u-")


def test_upload_route_normalizes_windows_zip_spelling() -> None:
    with _route_client() as client:
        response = client.post(
            "/api/v1/files/upload",
            files={"file": ("paper.zip", _zip_bytes(), "application/x-zip-compressed")},
        )

    assert response.status_code == 200
    assert response.json()["mime"] == "application/zip"


def test_upload_route_sniffs_rendered_type_for_generic_claim() -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"fake image body"
    with _route_client() as client:
        response = client.post(
            "/api/v1/files/upload",
            files={"file": ("shot", png, "application/octet-stream")},
        )

    assert response.status_code == 200
    assert response.json()["mime"] == "image/png"


def test_upload_route_sniffs_text_for_missing_claim() -> None:
    # A .tex with no browser-reported content type resolves via the whole
    # payload UTF-8 sniff instead of 400-ing on the missing mime.
    with _route_client() as client:
        response = client.post(
            "/api/v1/files/upload",
            files={"file": ("main.tex", b"\\documentclass{article}\n", "")},
        )

    assert response.status_code == 200
    assert response.json()["mime"] == "text/plain"


def test_upload_route_caps_opaque_via_config() -> None:
    from opensquilla.gateway.config import GatewayConfig

    config = GatewayConfig(attachments={"opaque_max_bytes": 1024})
    with _route_client(config=config) as client:
        response = client.post(
            "/api/v1/files/upload",
            files={"file": ("x.bin", b"\x00" + b"a" * 4096, "application/x-unknown")},
        )

    assert response.status_code == 413
    assert response.json()["code"] == "TOO_LARGE"


def test_upload_route_strict_mode_rejects_zip_and_missing_mime() -> None:
    from opensquilla.gateway.config import GatewayConfig
    from opensquilla.gateway.uploads import UploadStore

    config = GatewayConfig(attachments={"accept_opaque": False})
    strict_store = UploadStore(
        marker_dir=None, ttl_seconds=600, max_file_bytes=30 * 1024 * 1024, accept_opaque=False
    )
    with _route_client(config=config, store=strict_store) as client:
        rejected = client.post(
            "/api/v1/files/upload",
            files={"file": ("paper.zip", _zip_bytes(), "application/zip")},
        )
        missing = client.post(
            "/api/v1/files/upload",
            files={"file": ("main.tex", b"\\documentclass{article}\n", "")},
        )

    assert rejected.status_code == 415
    assert rejected.json()["code"] == "UNSUPPORTED_MEDIA_TYPE"
    assert missing.status_code == 400


def test_upload_unauthenticated_rejected() -> None:
    """The HTTP route returns 401 when no token is supplied (auth=token mode).

    Built against a minimal Starlette app that wires uploads.register_routes
    with the same AuthMiddleware production uses.
    """
    pytest.importorskip("starlette.testclient")
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from opensquilla.gateway.config import AuthConfig, GatewayConfig
    from opensquilla.gateway.middleware import AuthMiddleware
    from opensquilla.gateway.uploads import UploadStore, register_upload_routes

    config = GatewayConfig(auth=AuthConfig(mode="token", token="secret"))
    store = UploadStore(marker_dir=None, ttl_seconds=600, max_file_bytes=30 * 1024 * 1024)
    app = Starlette(debug=False)
    register_upload_routes(app, config=config, store=store)
    app.add_middleware(AuthMiddleware, config=config)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/files/upload",
            files={"file": ("x.pdf", b"%PDF-1.4\n", "application/pdf")},
        )
    assert response.status_code == 401


def test_upload_rejects_query_token_when_disallowed_for_multipart() -> None:
    """Query-string tokens are rejected for the multipart upload endpoint.

    The existing JSON-RPC routes accept ``?token=…`` as a convenience for
    browser-side consumers, but a multipart POST is the kind of request a
    malicious cross-origin page can craft, so we force the Authorization header
    for /api/v1/files/upload specifically.
    """
    pytest.importorskip("starlette.testclient")
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from opensquilla.gateway.config import AuthConfig, GatewayConfig
    from opensquilla.gateway.middleware import AuthMiddleware
    from opensquilla.gateway.uploads import UploadStore, register_upload_routes

    config = GatewayConfig(auth=AuthConfig(mode="token", token="secret"))
    store = UploadStore(marker_dir=None, ttl_seconds=600, max_file_bytes=30 * 1024 * 1024)
    app = Starlette(debug=False)
    register_upload_routes(app, config=config, store=store)
    app.add_middleware(AuthMiddleware, config=config)

    with TestClient(app) as client:
        # Auth mode passes the AuthMiddleware via the query token (legacy
        # convenience) but the upload handler MUST refuse it.
        response = client.post(
            "/api/v1/files/upload?token=secret",
            files={"file": ("x.pdf", b"%PDF-1.4\n", "application/pdf")},
        )
    assert response.status_code == 401, response.text
    body: dict[str, Any] = response.json()
    assert "Authorization" in body.get("error", "") or "header" in body.get("error", "").lower()


def test_upload_route_response_exposes_expires_at_and_ttl() -> None:
    """The upload response must advertise the staged lifetime so a client can
    re-upload before a slow compose sends against an expired uuid (issue #468)."""
    pytest.importorskip("starlette.testclient")
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from opensquilla.gateway.config import GatewayConfig
    from opensquilla.gateway.uploads import UploadStore, register_upload_routes

    store = UploadStore(marker_dir=None, ttl_seconds=600, max_file_bytes=30 * 1024 * 1024)
    app = Starlette(debug=False)
    register_upload_routes(app, config=GatewayConfig(), store=store)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/files/upload",
            files={"file": ("x.pdf", b"%PDF-1.4\n", "application/pdf")},
        )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    assert body["file_uuid"].startswith("u-")
    assert body["ttl_seconds"] == 600
    assert isinstance(body["expires_at"], (int, float))
    assert body["expires_at"] > time.time()


def test_upload_route_sniffs_png_for_empty_claim() -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"fake image body"
    with _route_client() as client:
        response = client.post(
            "/api/v1/files/upload",
            files={"file": ("shot", png, "")},
        )

    assert response.status_code == 200
    assert response.json()["mime"] == "image/png"


def test_strict_store_keeps_staged_text_at_inline_cap(tmp_path: Path) -> None:
    # accept_opaque=False restores the legacy stageable set, so text keeps the
    # 2MB inline cap on the staged path instead of the 30MiB staged ceiling.
    strict = UploadStore(
        marker_dir=tmp_path / "strict-inbound",
        ttl_seconds=600,
        max_file_bytes=30 * 1024 * 1024,
        accept_opaque=False,
    )
    with pytest.raises(UploadOversizeError):
        asyncio.run(strict.put("big.txt", "text/plain", b"a" * (TEXT_ATTACHMENT_BYTES + 1)))

    ok_uuid = asyncio.run(strict.put("ok.txt", "text/plain", b"a" * TEXT_ATTACHMENT_BYTES))
    assert ok_uuid.startswith("u-")


def test_app_factory_wires_strict_admission_into_route_and_store(tmp_path: Path) -> None:
    # Boot the real app factory with accept_opaque=False and verify the
    # end-to-end strict behavior: the route 415s a zip upload.
    pytest.importorskip("starlette.testclient")
    from starlette.testclient import TestClient

    from opensquilla.gateway.app import create_gateway_app
    from opensquilla.gateway.config import GatewayConfig
    from opensquilla.gateway.uploads import get_upload_store, set_upload_store

    original_store = get_upload_store()
    set_upload_store(None)
    try:
        config = GatewayConfig(
            attachments={
                "accept_opaque": False,
                "media_root": str(tmp_path / "media"),
                "upload_store_max_total_bytes": 7 * 1024 * 1024,
            },
        )
        app = create_gateway_app(config)
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/files/upload",
                files={"file": ("paper.zip", _zip_bytes(), "application/zip")},
            )
        assert response.status_code == 415
        assert response.json()["code"] == "UNSUPPORTED_MEDIA_TYPE"
        assert get_upload_store().accept_opaque is False
        assert get_upload_store().max_total_bytes == 7 * 1024 * 1024
    finally:
        set_upload_store(original_store)


def test_file_uuid_opaque_reference_resolves_with_store_mime(tmp_path: Path) -> None:
    # An opaque staged upload referenced WITHOUT a declared mime resolves via
    # the store's own metadata into a content-addressed attachment_ref.
    payload = b"PK\x03\x04" + b"\x00" * 128
    store = _FakeUploadStore(
        {
            "u-zip": (
                payload,
                {
                    "mime": "application/zip",
                    "name": "paper.zip",
                    "sha256": "x",
                    "size": len(payload),
                },
            )
        }
    )

    validated, _failures = validate_attachments([{"file_uuid": "u-zip", "name": "paper.zip"}])
    assert validated[0].get("type") is None

    resolved, consumed = asyncio.run(
        resolve_attachments(
            validated,
            store=store,
            material_root=tmp_path,
            session_id="s1",
        )
    )
    assert consumed == ["u-zip"]
    assert resolved[0]["kind"] == "attachment_ref"
    assert resolved[0]["mime"] == "application/zip"
    assert resolved[0]["size"] == len(payload)


# ---------------------------------------------------------------------------
# Aggregate RAM cap: reject-on-full, never evict.
# ---------------------------------------------------------------------------


def _capped_store(tmp_path: Path, max_total_bytes: int, ttl_seconds: float = 600) -> UploadStore:
    return UploadStore(
        marker_dir=tmp_path / "inbound",
        ttl_seconds=ttl_seconds,
        max_file_bytes=30 * 1024 * 1024,
        max_total_bytes=max_total_bytes,
    )


def test_store_full_rejects_without_insert_or_marker(tmp_path: Path) -> None:
    store = _capped_store(tmp_path, max_total_bytes=1024)
    ok_uuid = asyncio.run(store.put("a.txt", "text/plain", b"a" * 900))

    with pytest.raises(UploadStoreFullError, match="upload store is full"):
        asyncio.run(store.put("b.txt", "text/plain", b"b" * 200))

    # The staged entry is untouched (reject, never evict) and the rejected
    # upload left neither a marker nor a stranded per-uuid lock behind.
    payload, _meta = asyncio.run(store.get(ok_uuid))
    assert payload == b"a" * 900
    markers = list((tmp_path / "inbound").glob("*.meta"))
    assert [m.stem for m in markers] == [ok_uuid]
    assert set(store._locks) <= {ok_uuid}


def test_store_full_recovers_after_ttl_sweep(tmp_path: Path) -> None:
    store = _capped_store(tmp_path, max_total_bytes=1024, ttl_seconds=0.01)
    asyncio.run(store.put("a.txt", "text/plain", b"a" * 900))
    time.sleep(0.05)

    # The pre-insert sweep frees the expired entry, so the same-size payload
    # is admitted again.
    ok_uuid = asyncio.run(store.put("b.txt", "text/plain", b"b" * 900))
    assert ok_uuid.startswith("u-")


def test_store_full_boundary_admits_exact_fit(tmp_path: Path) -> None:
    store = _capped_store(tmp_path, max_total_bytes=1000)
    asyncio.run(store.put("a.txt", "text/plain", b"a" * 600))
    ok_uuid = asyncio.run(store.put("b.txt", "text/plain", b"b" * 400))
    assert ok_uuid.startswith("u-")
    with pytest.raises(UploadStoreFullError):
        asyncio.run(store.put("c.txt", "text/plain", b"c"))


def test_upload_route_returns_507_when_store_full(tmp_path: Path) -> None:
    # A genuinely transient condition: the payload fits the cap on its own,
    # but not while earlier staged entries are still alive.
    store = _capped_store(tmp_path, max_total_bytes=1024)
    asyncio.run(store.put("staged.txt", "text/plain", b"a" * 900))
    with _route_client(store=store) as client:
        response = client.post(
            "/api/v1/files/upload",
            files={"file": ("next.txt", b"b" * 200, "text/plain")},
        )

    assert response.status_code == 507
    body = response.json()
    assert body["code"] == "UPLOAD_STORE_FULL"
    assert "retry" in body["error"]


def test_payload_larger_than_total_cap_is_permanent_413(tmp_path: Path) -> None:
    # A payload that can NEVER fit the aggregate cap is a permanent per-payload
    # condition: 413 TOO_LARGE, not the retryable 507.
    store = _capped_store(tmp_path, max_total_bytes=64)
    with _route_client(store=store) as client:
        response = client.post(
            "/api/v1/files/upload",
            files={"file": ("big.txt", b"a" * 128, "text/plain")},
        )

    assert response.status_code == 413
    assert response.json()["code"] == "TOO_LARGE"


def test_non_positive_total_cap_falls_back_to_default(tmp_path: Path) -> None:
    # The RAM cap can be raised but not disabled: a non-positive config value
    # falls back to the default at app construction (with a boot warning).
    pytest.importorskip("starlette.testclient")
    from opensquilla.gateway.app import create_gateway_app
    from opensquilla.gateway.config import GatewayConfig
    from opensquilla.gateway.uploads import (
        _DEFAULT_MAX_TOTAL_BYTES,
        get_upload_store,
        set_upload_store,
    )

    original_store = get_upload_store()
    set_upload_store(None)
    try:
        config = GatewayConfig(
            attachments={
                "upload_store_max_total_bytes": 0,
                "media_root": str(tmp_path / "media"),
            },
        )
        create_gateway_app(config)
        assert get_upload_store().max_total_bytes == _DEFAULT_MAX_TOTAL_BYTES
    finally:
        set_upload_store(original_store)


def test_strict_mime_rejection_takes_precedence_over_full_store(tmp_path: Path) -> None:
    # Validation precedes the capacity check, so strict mode keeps its
    # documented 415 error precedence even when the store is at capacity.
    strict = UploadStore(
        marker_dir=tmp_path / "inbound",
        ttl_seconds=600,
        max_file_bytes=30 * 1024 * 1024,
        accept_opaque=False,
        max_total_bytes=1,
    )
    with pytest.raises(UploadUnsupportedMimeError):
        asyncio.run(strict.put("x.sh", "application/x-shellscript", b"#!/bin/sh\n"))
