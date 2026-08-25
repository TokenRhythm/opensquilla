from __future__ import annotations

import asyncio
import io
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

import opensquilla.gateway.rpc_artifacts as rpc_artifacts
from opensquilla.artifacts import ArtifactStore
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.guest_rpc_policy import guest_owned_session_key
from opensquilla.gateway.rpc import RpcContext, get_dispatcher, validate_classification
from opensquilla.gateway.scopes import METHOD_SCOPES, READ_SCOPE
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage


@pytest.fixture
async def artifact_rpc_env(tmp_path: Path):
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    media_root = tmp_path / "media"
    config = SimpleNamespace(
        attachments=SimpleNamespace(media_root=str(media_root)),
        state_dir=None,
        config_path=None,
    )
    ctx = RpcContext(conn_id="test", session_manager=manager, config=config)
    try:
        yield manager, ArtifactStore(media_root), ctx
    finally:
        await storage.close()


def _set_created_at(store: ArtifactStore, ref, created_at: str):
    updated = replace(ref, created_at=created_at)
    (store.path_for(ref).parent / "meta.json").write_text(
        json.dumps(updated.to_dict(), ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    return updated


def test_artifact_rpc_methods_are_registered_read_only() -> None:
    registry = get_dispatcher()
    for method in ("artifacts.list", "artifacts.get"):
        assert METHOD_SCOPES[method] == READ_SCOPE
        entry = registry.get_entry(method)
        assert entry is not None
        assert entry.required_scope == READ_SCOPE
    validate_classification(registry)


@pytest.mark.asyncio
async def test_artifacts_list_pages_latest_metadata_and_gets_one(
    artifact_rpc_env,
) -> None:
    manager, store, ctx = artifact_rpc_env
    session_key = "agent:main:webchat:artifacts"
    session = await manager.create(session_key)
    refs = [
        store.publish_bytes(
            f"payload-{index}".encode(),
            session_id=session.session_id,
            session_key=session_key,
            name=f"report-{index}.txt",
            mime="text/plain",
            source="publish_artifact",
        )
        for index in range(3)
    ]
    refs = [
        _set_created_at(store, ref, f"2026-02-0{index + 1}T00:00:00Z")
        for index, ref in enumerate(refs)
    ]

    newest = await get_dispatcher().dispatch(
        "list-newest",
        "artifacts.list",
        {"sessionKey": session_key, "limit": 2},
        ctx,
    )

    assert newest.error is None, newest.error
    assert set(newest.payload) == {
        "artifacts",
        "has_more",
        "oldest_cursor",
        "newest_cursor",
        "total_count",
        "page_size",
    }
    assert [item["id"] for item in newest.payload["artifacts"]] == [
        refs[1].id,
        refs[2].id,
    ]
    assert newest.payload["has_more"] is True
    assert newest.payload["total_count"] == 3
    assert newest.payload["page_size"] == 2
    assert newest.payload["oldest_cursor"] == refs[1].id
    assert newest.payload["newest_cursor"] == refs[2].id

    older = await get_dispatcher().dispatch(
        "list-older",
        "artifacts.list",
        {
            "sessionKey": session_key,
            "limit": 2,
            "before": newest.payload["oldest_cursor"],
        },
        ctx,
    )

    assert older.error is None, older.error
    assert [item["id"] for item in older.payload["artifacts"]] == [refs[0].id]
    assert older.payload["has_more"] is False
    assert older.payload["total_count"] == 3

    fetched = await get_dispatcher().dispatch(
        "get-one",
        "artifacts.get",
        {"sessionKey": session_key, "artifactId": refs[2].id},
        ctx,
    )

    assert fetched.error is None, fetched.error
    payload = fetched.payload["artifact"]
    assert payload["id"] == refs[2].id
    assert payload["download_url"] == f"/api/v1/artifacts/{refs[2].id}"
    assert "session_key" not in payload

    clamped = await get_dispatcher().dispatch(
        "list-clamped",
        "artifacts.list",
        {"sessionKey": session_key, "limit": 999},
        ctx,
    )
    assert clamped.error is None
    assert clamped.payload["page_size"] == 200


@pytest.mark.asyncio
async def test_artifact_rpc_is_session_scoped_and_missing_list_is_empty(
    artifact_rpc_env,
) -> None:
    manager, store, ctx = artifact_rpc_env
    first_key = "agent:main:webchat:first"
    second_key = "agent:main:webchat:second"
    first = await manager.create(first_key)
    second = await manager.create(second_key)
    first_ref = store.publish_bytes(
        b"first",
        session_id=first.session_id,
        session_key=first_key,
        name="first.txt",
        mime="text/plain",
        source="publish_artifact",
    )
    second_ref = store.publish_bytes(
        b"second",
        session_id=second.session_id,
        session_key=second_key,
        name="second.txt",
        mime="text/plain",
        source="publish_artifact",
    )

    listed = await get_dispatcher().dispatch(
        "list-first",
        "artifacts.list",
        {"sessionKey": first_key},
        ctx,
    )
    crossed = await get_dispatcher().dispatch(
        "get-crossed",
        "artifacts.get",
        {"sessionKey": first_key, "artifactId": second_ref.id},
        ctx,
    )
    missing_session = await get_dispatcher().dispatch(
        "list-missing-session",
        "artifacts.list",
        {"sessionKey": "agent:main:webchat:missing"},
        ctx,
    )
    missing_session_get = await get_dispatcher().dispatch(
        "get-missing-session",
        "artifacts.get",
        {"sessionKey": "agent:main:webchat:missing", "artifactId": first_ref.id},
        ctx,
    )

    assert listed.error is None
    assert listed.payload["page_size"] == 100
    assert [item["id"] for item in listed.payload["artifacts"]] == [first_ref.id]
    assert crossed.error is not None
    assert crossed.error.code == "NOT_FOUND"
    assert missing_session.error is None
    assert missing_session.payload == {
        "artifacts": [],
        "has_more": False,
        "oldest_cursor": None,
        "newest_cursor": None,
        "total_count": 0,
        "page_size": 100,
    }
    assert missing_session_get.error is not None
    assert missing_session_get.error.code == "NOT_FOUND"
    assert missing_session_get.error.message == "Artifact not found"


@pytest.mark.asyncio
async def test_artifact_rpc_payload_exposes_thumbnail_without_local_paths(
    artifact_rpc_env,
) -> None:
    manager, store, ctx = artifact_rpc_env
    session_key = "agent:main:webchat:thumbnail"
    session = await manager.create(session_key)
    image = io.BytesIO()
    Image.new("RGB", (8, 8), color="blue").save(image, format="PNG")
    ref = store.publish_bytes(
        image.getvalue(),
        session_id=session.session_id,
        session_key=session_key,
        name="chart.png",
        mime="image/png",
        source="image_generate",
    )
    assert ref.has_thumbnail is True

    result = await get_dispatcher().dispatch(
        "get-thumbnail",
        "artifacts.get",
        {"sessionKey": session_key, "artifactId": ref.id},
        ctx,
    )

    assert result.error is None
    payload = result.payload["artifact"]
    assert payload["thumbnail_url"] == f"/api/v1/artifacts/{ref.id}?variant=thumb"
    assert "session_key" not in payload
    assert "has_thumbnail" not in payload
    assert "path" not in payload
    assert "local_path" not in payload


@pytest.mark.asyncio
async def test_artifact_rpc_rejects_bad_params_and_requires_read_scope(
    artifact_rpc_env,
) -> None:
    manager, _store, ctx = artifact_rpc_env
    session_key = "agent:main:webchat:any"
    await manager.create(session_key)
    denied_ctx = RpcContext(
        conn_id="denied",
        principal=Principal(
            role="operator",
            scopes=frozenset(),
            is_owner=False,
            authenticated=True,
        ),
        session_manager=ctx.session_manager,
        config=ctx.config,
    )

    denied = await get_dispatcher().dispatch(
        "denied", "artifacts.list", {"sessionKey": session_key}, denied_ctx
    )
    client_session_id = await get_dispatcher().dispatch(
        "client-session-id",
        "artifacts.list",
        {"session_id": "not-an-authority-boundary"},
        ctx,
    )
    invalid = await get_dispatcher().dispatch(
        "invalid",
        "artifacts.list",
        {"sessionKey": session_key, "before": 42},
        ctx,
    )
    empty_cursor = await get_dispatcher().dispatch(
        "empty-cursor",
        "artifacts.list",
        {"sessionKey": session_key, "before": "  "},
        ctx,
    )
    missing_cursor = await get_dispatcher().dispatch(
        "missing-cursor",
        "artifacts.list",
        {"sessionKey": session_key, "before": "art-missing"},
        ctx,
    )

    assert denied.error is not None
    assert denied.error.code == "UNAUTHORIZED"
    assert client_session_id.error is not None
    assert client_session_id.error.code == "INVALID_REQUEST"
    assert invalid.error is not None
    assert invalid.error.code == "INVALID_REQUEST"
    assert empty_cursor.error is not None
    assert empty_cursor.error.code == "INVALID_REQUEST"
    assert missing_cursor.error is not None
    assert missing_cursor.error.code == "INVALID_REQUEST"


@pytest.mark.asyncio
async def test_artifact_rpc_offloads_store_reads_to_threads(
    artifact_rpc_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, store, ctx = artifact_rpc_env
    session_key = "agent:main:webchat:threaded"
    session = await manager.create(session_key)
    ref = store.publish_bytes(
        b"threaded",
        session_id=session.session_id,
        session_key=session_key,
        name="threaded.txt",
        mime="text/plain",
        source="publish_artifact",
    )
    original_to_thread = asyncio.to_thread
    calls: list[str] = []

    async def _record_to_thread(func, /, *args, **kwargs):
        if isinstance(getattr(func, "__self__", None), ArtifactStore):
            calls.append(func.__name__)
        return await original_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(rpc_artifacts.asyncio, "to_thread", _record_to_thread)

    await rpc_artifacts._handle_artifacts_list({"sessionKey": session_key}, ctx)
    await rpc_artifacts._handle_artifacts_get(
        {"sessionKey": session_key, "artifactId": ref.id}, ctx
    )

    assert calls == ["list_refs", "get_ref"]


@pytest.mark.asyncio
async def test_artifacts_list_hides_directory_errors_as_retryable_unavailable(
    artifact_rpc_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, _store, ctx = artifact_rpc_env
    session_key = "agent:main:webchat:unavailable"
    await manager.create(session_key)
    sensitive_path = "/private/operator/media/artifacts"

    def _unreadable(_store, **_kwargs):
        raise OSError(f"cannot scan {sensitive_path}")

    monkeypatch.setattr(rpc_artifacts.ArtifactStore, "list_refs", _unreadable)

    result = await get_dispatcher().dispatch(
        "list-unavailable",
        "artifacts.list",
        {"sessionKey": session_key},
        ctx,
    )

    assert result.error is not None
    assert result.error.code == "DOCUMENT_UNAVAILABLE"
    assert result.error.retryable is True
    assert result.error.accepted is False
    assert result.error.message == "This page is temporarily unavailable. Try again."
    assert sensitive_path not in result.error.message


@pytest.mark.asyncio
async def test_artifacts_get_hides_directory_errors_as_retryable_unavailable(
    artifact_rpc_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, store, ctx = artifact_rpc_env
    session_key = "agent:main:webchat:get-unavailable"
    session = await manager.create(session_key)
    ref = store.publish_bytes(
        b"artifact",
        session_id=session.session_id,
        session_key=session_key,
        name="artifact.txt",
        mime="text/plain",
        source="publish_artifact",
    )
    sensitive_path = "/private/operator/media/artifacts"

    def _unreadable(_store, **_kwargs):
        raise OSError(f"cannot inspect {sensitive_path}")

    monkeypatch.setattr(rpc_artifacts.ArtifactStore, "get_ref", _unreadable)

    result = await get_dispatcher().dispatch(
        "get-unavailable",
        "artifacts.get",
        {"sessionKey": session_key, "artifactId": ref.id},
        ctx,
    )

    assert result.error is not None
    assert result.error.code == "DOCUMENT_UNAVAILABLE"
    assert result.error.retryable is True
    assert result.error.accepted is False
    assert result.error.message == "This page is temporarily unavailable. Try again."
    assert sensitive_path not in result.error.message


@pytest.mark.asyncio
async def test_guest_can_read_only_its_session_artifacts(artifact_rpc_env) -> None:
    manager, store, owner_ctx = artifact_rpc_env
    owner_id = "a" * 64
    owned_key = guest_owned_session_key(owner_id, "artifacts")
    other_key = guest_owned_session_key("b" * 64, "artifacts")
    owned_session = await manager.create(owned_key)
    other_session = await manager.create(other_key)
    owned_ref = store.publish_bytes(
        b"owned",
        session_id=owned_session.session_id,
        session_key=owned_key,
        name="owned.txt",
        mime="text/plain",
        source="publish_artifact",
    )
    other_ref = store.publish_bytes(
        b"other",
        session_id=other_session.session_id,
        session_key=other_key,
        name="other.txt",
        mime="text/plain",
        source="publish_artifact",
    )
    guest_ctx = RpcContext(
        conn_id="guest",
        principal=Principal(
            role="operator",
            scopes=frozenset({"operator.read"}),
            is_owner=False,
            authenticated=False,
            capabilities=frozenset({"guest.safe"}),
            auth_state="guest",
            guest_owner_id=owner_id,
            guest_session_key="osqg_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        ),
        session_manager=manager,
        config=owner_ctx.config,
    )

    listed = await get_dispatcher().dispatch(
        "guest-list",
        "artifacts.list",
        {"sessionKey": owned_key},
        guest_ctx,
    )
    fetched = await get_dispatcher().dispatch(
        "guest-get",
        "artifacts.get",
        {"sessionKey": owned_key, "artifactId": owned_ref.id},
        guest_ctx,
    )
    rejected_list = await get_dispatcher().dispatch(
        "guest-list-other",
        "artifacts.list",
        {"sessionKey": other_key},
        guest_ctx,
    )
    rejected_get = await get_dispatcher().dispatch(
        "guest-get-other",
        "artifacts.get",
        {"sessionKey": other_key, "artifactId": other_ref.id},
        guest_ctx,
    )

    assert listed.error is None
    assert [item["id"] for item in listed.payload["artifacts"]] == [owned_ref.id]
    assert fetched.error is None
    assert fetched.payload["artifact"]["id"] == owned_ref.id
    assert rejected_list.error is not None
    assert rejected_list.error.code == "UNAUTHORIZED"
    assert rejected_get.error is not None
    assert rejected_get.error.code == "UNAUTHORIZED"
