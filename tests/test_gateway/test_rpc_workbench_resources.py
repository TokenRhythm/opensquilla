"""Resource inventory, copy-import, and immutable publish RPC contracts."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

import opensquilla.gateway.rpc_workbench_resources as resource_rpc
from opensquilla.artifact_session import (
    ArtifactSessionService,
    DocumentImportMode,
    DocumentSourceType,
    MutationAttemptStatus,
)
from opensquilla.artifacts import ArtifactBundle, ArtifactBundleSourceFile, ArtifactStore
from opensquilla.gateway.rpc import RpcContext, RpcUnavailableError, get_dispatcher
from opensquilla.gateway.scopes import METHOD_SCOPES, READ_SCOPE, WRITE_SCOPE
from opensquilla.gateway.transcripts import build_transcript_attachment_envelope
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import TranscriptEntry
from opensquilla.session.storage import SessionStorage

SESSION_KEY = "agent:main:webchat:workbench-resources"


def test_workbench_resource_method_scopes_are_fail_closed() -> None:
    assert METHOD_SCOPES["workbench.resources.list"] == READ_SCOPE
    assert METHOD_SCOPES["workbench.resources.get"] == READ_SCOPE
    assert METHOD_SCOPES["workbench.previews.create"] == READ_SCOPE
    assert METHOD_SCOPES["documents.import"] == WRITE_SCOPE
    assert METHOD_SCOPES["documents.publish"] == WRITE_SCOPE


@pytest.mark.parametrize(
    ("resource_type", "id_field"),
    (
        ("attachment", "attachmentId"),
        ("document", "documentId"),
        ("deliverable", "artifactId"),
        ("url", "urlId"),
    ),
)
def test_workbench_resource_refs_use_discriminated_ids_with_legacy_alias(
    resource_type: str,
    id_field: str,
) -> None:
    resource_id = f"{resource_type}-fixture"
    assert resource_rpc._resource_ref(
        {"resource": {"type": resource_type, id_field: resource_id}}
    ) == (resource_type, resource_id)
    assert resource_rpc._resource_ref(
        {"resource": {"type": resource_type, "id": resource_id}}
    ) == (resource_type, resource_id)
    assert resource_rpc._resource_ref_payload(resource_type, resource_id) == {
        "type": resource_type,
        id_field: resource_id,
        "id": resource_id,
    }

    with pytest.raises(ValueError, match="must match"):
        resource_rpc._resource_ref(
            {
                "resource": {
                    "type": resource_type,
                    id_field: resource_id,
                    "id": "different-fixture",
                }
            }
        )


@pytest.mark.asyncio
async def test_multifile_deliverable_is_preview_only_and_never_truncated_on_import(
    resource_env,
) -> None:
    env = resource_env
    ref = env.store.publish_bundle(
        ArtifactBundle(
            entrypoint="index.html",
            files=(
                ArtifactBundleSourceFile(
                    path="index.html",
                    mime="text/html",
                    data=b"<link rel='stylesheet' href='style.css'><h1>bundle</h1>",
                ),
                ArtifactBundleSourceFile(
                    path="style.css",
                    mime="text/css",
                    data=b"h1 { color: red; }",
                ),
            ),
        ),
        session_id=env.session.session_id,
        session_key=SESSION_KEY,
        name="bundle.html",
        mime="text/html",
        source="workbench-resource-bundle-test",
    )
    listed = await _dispatch(
        env,
        "workbench.resources.list",
        {"sessionKey": SESSION_KEY, "types": ["deliverable"]},
    )
    assert listed.error is None, listed.error
    deliverable = next(
        item for item in listed.payload["resources"] if item["resource"]["id"] == ref.id
    )
    assert deliverable["capabilities"]["preview"] is True
    assert deliverable["capabilities"]["edit"] is False

    imported = await _dispatch(
        env,
        "documents.import",
        {
            "sessionKey": SESSION_KEY,
            "source": {"type": "deliverable", "id": ref.id},
            "mode": "copy",
            "expectedSha256": ref.sha256,
            "idempotencyKey": "must-not-truncate-bundle",
        },
    )
    assert imported.error is not None
    assert imported.error.code == "DOCUMENT_BUNDLE_UNSUPPORTED"
    service = await ArtifactSessionService.from_session_storage(env.storage)
    assert await service.list_documents(
        session_key=SESSION_KEY,
        session_id=env.session.session_id,
        limit=10,
    ) == ()


@pytest.fixture
async def resource_env(tmp_path: Path):
    storage = SessionStorage(":memory:")
    await storage.connect()
    media_root = tmp_path / "media"
    manager = SessionManager(storage, inject_time_prefix=False, media_root=media_root)
    session = await manager.create(SESSION_KEY)
    config = SimpleNamespace(
        attachments=SimpleNamespace(
            media_root=str(media_root),
            persist_transcripts=True,
        ),
        state_dir=str(tmp_path / "state"),
        config_path=None,
    )
    ctx = RpcContext(
        conn_id="workbench-resource-test",
        session_manager=manager,
        config=config,
    )
    try:
        yield SimpleNamespace(
            storage=storage,
            manager=manager,
            session=session,
            store=ArtifactStore(media_root),
            config=config,
            ctx=ctx,
        )
    finally:
        await storage.close()


async def _dispatch(env, method: str, params: dict[str, object]):
    return await get_dispatcher().dispatch(f"test:{method}", method, params, env.ctx)


@pytest.mark.asyncio
async def test_resource_reads_do_not_change_sqlite_rows(resource_env) -> None:
    env = resource_env
    attachment = await _append_attachment(
        env,
        message_id="read-only-resource",
        name="read-only.html",
        payload=b"<h1>read only</h1>",
        staged=False,
    )
    # Warm the additive schema seam once, exactly as Gateway boot does.
    await ArtifactSessionService.from_session_storage(env.storage)
    changes_before = env.storage.conn.total_changes

    listed = await _dispatch(
        env,
        "workbench.resources.list",
        {"sessionKey": SESSION_KEY},
    )
    fetched = await _dispatch(
        env,
        "workbench.resources.get",
        {
            "sessionKey": SESSION_KEY,
            "resourceRef": {
                "type": "attachment",
                "attachmentId": attachment["attachment_id"],
            },
        },
    )
    legacy_fetched = await _dispatch(
        env,
        "workbench.resources.get",
        {
            "sessionKey": SESSION_KEY,
            "resource": {"type": "attachment", "id": attachment["attachment_id"]},
        },
    )
    previewed = await _dispatch(
        env,
        "workbench.previews.create",
        {
            "sessionKey": SESSION_KEY,
            "resourceRef": {
                "type": "attachment",
                "attachmentId": attachment["attachment_id"],
            },
            "mode": "isolated",
        },
    )

    assert listed.error is None, listed.error
    assert fetched.error is None, fetched.error
    assert fetched.payload["resource"]["resource"] == {
        "type": "attachment",
        "attachmentId": attachment["attachment_id"],
        "id": attachment["attachment_id"],
    }
    assert legacy_fetched.error is None, legacy_fetched.error
    assert legacy_fetched.payload == fetched.payload
    assert previewed.error is None, previewed.error
    assert env.storage.conn.total_changes == changes_before


@pytest.mark.asyncio
async def test_resource_list_does_not_reserve_sqlite_writer_slot(tmp_path: Path) -> None:
    db_path = tmp_path / "resource-reads.sqlite3"
    storage = SessionStorage(str(db_path))
    await storage.connect()
    media_root = tmp_path / "media"
    manager = SessionManager(storage, inject_time_prefix=False, media_root=media_root)
    session = await manager.create(SESSION_KEY)
    config = SimpleNamespace(
        attachments=SimpleNamespace(
            media_root=str(media_root),
            persist_transcripts=True,
        ),
        state_dir=str(tmp_path / "state"),
        config_path=None,
    )
    env = SimpleNamespace(
        storage=storage,
        manager=manager,
        session=session,
        store=ArtifactStore(media_root),
        config=config,
        ctx=RpcContext(
            conn_id="workbench-resource-read-lock-test",
            session_manager=manager,
            config=config,
        ),
    )
    blocker = sqlite3.connect(db_path, isolation_level=None)
    try:
        # Boot-time reconciliation is complete before the competing writer starts.
        await ArtifactSessionService.from_session_storage(storage)
        blocker.execute("PRAGMA journal_mode=WAL")
        blocker.execute("BEGIN IMMEDIATE")

        listed = await _dispatch(
            env,
            "workbench.resources.list",
            {"sessionKey": SESSION_KEY},
        )

        assert listed.error is None, listed.error
        assert listed.payload["resources"] == []
    finally:
        if blocker.in_transaction:
            blocker.rollback()
        blocker.close()
        await storage.close()


async def _append_attachment(
    env,
    *,
    message_id: str,
    name: str,
    payload: bytes,
    staged: bool,
    mime: str = "text/html",
) -> dict[str, object]:
    attachment: dict[str, object] = {
        "type": mime,
        "data": base64.b64encode(payload).decode("ascii"),
        "name": name,
    }
    if staged:
        attachment["_was_staged"] = True
    envelope, _writes = build_transcript_attachment_envelope(
        text=f"uploaded {name}",
        attachments=[attachment],
        session_id=env.session.session_id,
        media_root=Path(env.config.attachments.media_root),
        persist_enabled=True,
    )
    await env.storage.append_transcript_entry(
        TranscriptEntry(
            session_id=env.session.session_id,
            session_key=SESSION_KEY,
            message_id=message_id,
            role="user",
            content=envelope,
        )
    )
    return json.loads(envelope)["attachments"][0]


async def _import_attachment(env, attachment_id: str, *, key: str):
    resolved = await _dispatch(
        env,
        "workbench.resources.get",
        {
            "sessionKey": SESSION_KEY,
            "resource": {"type": "attachment", "id": attachment_id},
        },
    )
    assert resolved.error is None, resolved.error
    response = await _dispatch(
        env,
        "documents.import",
        {
            "sessionKey": SESSION_KEY,
            "source": {"type": "attachment", "attachmentId": attachment_id},
            "mode": "copy",
            "expectedSha256": resolved.payload["resource"]["sha256"],
            "idempotencyKey": key,
        },
    )
    assert response.error is None, response.error
    return response.payload


@pytest.mark.asyncio
async def test_attachment_preview_descriptor_is_read_only_and_content_free(resource_env) -> None:
    env = resource_env
    source = b"<!doctype html><h1>private preview heading</h1>"
    attachment = await _append_attachment(
        env,
        message_id="message-preview-only",
        name="preview.html",
        payload=source,
        staged=True,
    )

    response = await _dispatch(
        env,
        "workbench.previews.create",
        {
            "sessionKey": SESSION_KEY,
            "resourceRef": {
                "type": "attachment",
                "id": attachment["attachment_id"],
            },
            "mode": "isolated",
        },
    )

    assert response.error is None, response.error
    preview = response.payload["preview"]
    assert preview["sandboxProfile"] == "opaque-offline"
    assert preview["network"] is False
    assert preview["adapter"]["sourceSha256"] == hashlib.sha256(source).hexdigest()
    assert "private preview heading" not in repr(response.payload)
    service = await ArtifactSessionService.from_session_storage(env.storage)
    assert await service.list_documents(
        session_key=SESSION_KEY,
        session_id=env.session.session_id,
        limit=10,
    ) == ()


@pytest.mark.asyncio
async def test_invalid_html_attachments_fail_closed_without_read_side_writes(resource_env) -> None:
    env = resource_env
    invalid_encoding = await _append_attachment(
        env,
        message_id="message-invalid-encoding",
        name="invalid-encoding.html",
        payload=b"<h1>\xff</h1>",
        staged=True,
    )
    invalid_structure = await _append_attachment(
        env,
        message_id="message-invalid-structure",
        name="invalid-structure.html",
        payload=b"",
        staged=True,
    )
    changes_before_reads = env.storage.conn.total_changes

    expected_reasons = {
        str(invalid_encoding["attachment_id"]): "html_encoding_unsupported",
        str(invalid_structure["attachment_id"]): "html_validation_failed",
    }
    for attachment_id, expected_reason in expected_reasons.items():
        resolved = await _dispatch(
            env,
            "workbench.resources.get",
            {
                "sessionKey": SESSION_KEY,
                "resource": {"type": "attachment", "id": attachment_id},
            },
        )
        assert resolved.error is None, resolved.error
        capabilities = resolved.payload["resource"]["capabilities"]
        assert capabilities["preview"] is False
        assert capabilities["edit"] is False
        assert capabilities["editReasonCode"] == expected_reason

        preview = await _dispatch(
            env,
            "workbench.previews.create",
            {
                "sessionKey": SESSION_KEY,
                "resourceRef": {"type": "attachment", "id": attachment_id},
            },
        )
        assert preview.error is not None
        assert preview.error.code == "WORKBENCH_PREVIEW_UNSUPPORTED"
        assert preview.error.details == {"reasonCode": expected_reason}

    assert env.storage.conn.total_changes == changes_before_reads
    service = await ArtifactSessionService.from_session_storage(env.storage)
    assert await service.list_documents(
        session_key=SESSION_KEY,
        session_id=env.session.session_id,
        limit=10,
    ) == ()


@pytest.mark.asyncio
async def test_oversized_html_capabilities_fail_before_doomed_ui_actions(resource_env) -> None:
    env = resource_env
    edit_too_large = await _append_attachment(
        env,
        message_id="message-edit-too-large",
        name="edit-too-large.html",
        payload=b"<main>" + (b"x" * (2 * 1024 * 1024)) + b"</main>",
        staged=True,
    )
    preview_too_large = await _append_attachment(
        env,
        message_id="message-preview-too-large",
        name="preview-too-large.html",
        payload=b"<main>" + (b"x" * (5 * 1024 * 1024)) + b"</main>",
        staged=True,
    )

    editable_capabilities = (
        await _dispatch(
            env,
            "workbench.resources.get",
            {
                "sessionKey": SESSION_KEY,
                "resource": {
                    "type": "attachment",
                    "id": edit_too_large["attachment_id"],
                },
            },
        )
    ).payload["resource"]["capabilities"]
    assert editable_capabilities == {
        "preview": True,
        "download": True,
        "edit": False,
        "publish": False,
        "previewReasonCode": None,
        "editReasonCode": "html_edit_size_unsupported",
    }

    preview_capabilities = (
        await _dispatch(
            env,
            "workbench.resources.get",
            {
                "sessionKey": SESSION_KEY,
                "resource": {
                    "type": "attachment",
                    "id": preview_too_large["attachment_id"],
                },
            },
        )
    ).payload["resource"]["capabilities"]
    assert preview_capabilities == {
        "preview": False,
        "download": True,
        "edit": False,
        "publish": False,
        "previewReasonCode": "html_preview_size_unsupported",
        "editReasonCode": "html_edit_size_unsupported",
    }
    preview = await _dispatch(
        env,
        "workbench.previews.create",
        {
            "sessionKey": SESSION_KEY,
            "resourceRef": {
                "type": "attachment",
                "id": preview_too_large["attachment_id"],
            },
        },
    )
    assert preview.error is not None
    assert preview.error.code == "WORKBENCH_PREVIEW_UNSUPPORTED"
    assert preview.error.details == {"reasonCode": "html_preview_size_unsupported"}


@pytest.mark.asyncio
async def test_resource_inventory_preserves_inline_and_staged_attachment_occurrences(
    resource_env,
) -> None:
    env = resource_env
    html = b"<!doctype html><h1>same bytes</h1>"
    first = await _append_attachment(
        env,
        message_id="message-staged-one",
        name="first.html",
        payload=html,
        staged=True,
    )
    second = await _append_attachment(
        env,
        message_id="message-staged-two",
        name="second.html",
        payload=html,
        staged=True,
    )
    inline = await _append_attachment(
        env,
        message_id="message-inline",
        name="inline.html",
        payload=html,
        staged=False,
    )

    ids = {str(first["attachment_id"]), str(second["attachment_id"]), str(inline["attachment_id"])}
    assert len(ids) == 3
    sha = hashlib.sha256(html).hexdigest()
    transcript_dir = (
        Path(env.config.attachments.media_root) / "transcripts" / env.session.session_id
    )
    assert list(transcript_dir.iterdir()) == [transcript_dir / sha]

    changes_before_read = env.storage.conn.total_changes
    listed = await _dispatch(
        env,
        "workbench.resources.list",
        {"sessionKey": SESSION_KEY},
    )
    assert listed.error is None, listed.error
    attachments = [
        item for item in listed.payload["resources"] if item["resource"]["type"] == "attachment"
    ]
    assert len(attachments) == 3
    assert {item["resource"]["id"] for item in attachments} == ids
    assert {item["sha256"] for item in attachments} == {sha}
    assert {item["relations"]["messageId"] for item in attachments} == {
        "message-staged-one",
        "message-staged-two",
        "message-inline",
    }
    assert all(item["capabilities"]["edit"] is True for item in attachments)
    assert all(
        "downloadUrl" in item for item in attachments if item["name"] != "inline.html"
    )
    assert "downloadUrl" not in next(
        item for item in attachments if item["name"] == "inline.html"
    )

    inline_get = await _dispatch(
        env,
        "workbench.resources.get",
        {
            "sessionKey": SESSION_KEY,
            "resource": {"type": "attachment", "id": inline["attachment_id"]},
        },
    )
    assert inline_get.error is None, inline_get.error
    inline_url = inline_get.payload["resource"]["downloadUrl"]
    assert inline_url.startswith("data:text/html;base64,")
    assert base64.b64decode(inline_url.split(",", 1)[1], validate=True) == html
    assert env.storage.conn.total_changes == changes_before_read

    imported_first = await _import_attachment(
        env,
        str(first["attachment_id"]),
        key="import-occurrence-one",
    )
    imported_second = await _import_attachment(
        env,
        str(second["attachment_id"]),
        key="import-occurrence-two",
    )
    assert imported_first["document"]["id"] != imported_second["document"]["id"]
    assert imported_first["binding"]["source"]["id"] == first["attachment_id"]
    assert imported_first["binding"]["source"]["attachmentId"] == first["attachment_id"]
    assert imported_second["binding"]["source"]["id"] == second["attachment_id"]
    assert imported_second["binding"]["source"]["attachmentId"] == second["attachment_id"]
    assert imported_first["binding"]["bindingId"] == imported_first["binding"]["id"]

    replayed = await _import_attachment(
        env,
        str(first["attachment_id"]),
        key="import-occurrence-one",
    )
    assert replayed["document"]["id"] == imported_first["document"]["id"]
    assert replayed["receipt"]["replayed"] is True

    after = await _dispatch(
        env,
        "workbench.resources.list",
        {"sessionKey": SESSION_KEY, "types": ["document"]},
    )
    assert after.error is None, after.error
    assert len(after.payload["resources"]) == 2
    for document in after.payload["resources"]:
        assert document["relations"]["headArtifactId"]
        assert document["relations"]["headRevisionId"]
        source_ref = document["relations"]["source"]
        assert source_ref["type"] == "attachment"
        assert source_ref["attachmentId"] == source_ref["id"]
        assert "revisionId=" in document["downloadUrl"]


@pytest.mark.asyncio
async def test_historical_attachment_ids_are_stable_per_message_occurrence(
    resource_env,
) -> None:
    env = resource_env
    html = b"<h1>historical</h1>"
    for message_id, name in (("legacy-one", "one.html"), ("legacy-two", "two.html")):
        attachment = {
            "type": "text/html",
            "data": base64.b64encode(html).decode("ascii"),
            "name": name,
            "_was_staged": True,
        }
        envelope, _writes = build_transcript_attachment_envelope(
            text="historical upload",
            attachments=[attachment],
            session_id=env.session.session_id,
            media_root=Path(env.config.attachments.media_root),
            persist_enabled=True,
        )
        raw = json.loads(envelope)
        raw["attachments"][0].pop("attachment_id")
        await env.storage.append_transcript_entry(
            TranscriptEntry(
                session_id=env.session.session_id,
                session_key=SESSION_KEY,
                message_id=message_id,
                role="user",
                content=json.dumps(raw),
            )
        )

    params = {"sessionKey": SESSION_KEY, "types": ["attachment"]}
    first = await _dispatch(env, "workbench.resources.list", params)
    second = await _dispatch(env, "workbench.resources.list", params)
    assert first.error is None, first.error
    assert second.error is None, second.error
    first_ids = [item["resource"]["id"] for item in first.payload["resources"]]
    second_ids = [item["resource"]["id"] for item in second.payload["resources"]]
    assert first_ids == second_ids
    assert len(set(first_ids)) == 2
    assert all(item.startswith("att_legacy_") for item in first_ids)


@pytest.mark.asyncio
async def test_import_is_session_scoped_and_recovers_reserved_candidate_after_crash(
    resource_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = resource_env
    attachment = await _append_attachment(
        env,
        message_id="message-crash",
        name="crash.html",
        payload=b"<h1>recover</h1>",
        staged=True,
    )
    attachment_id = str(attachment["attachment_id"])
    original = resource_rpc._ensure_internal_candidate
    failed_once = False

    async def _write_then_crash(*args, **kwargs):
        nonlocal failed_once
        result = await original(*args, **kwargs)
        if not failed_once:
            failed_once = True
            raise RpcUnavailableError("synthetic crash after candidate write")
        return result

    monkeypatch.setattr(resource_rpc, "_ensure_internal_candidate", _write_then_crash)
    params = {
        "sessionKey": SESSION_KEY,
        "source": {"type": "attachment", "id": attachment_id},
        "mode": "copy",
        "expectedSha256": attachment["sha256_ref"],
        "idempotencyKey": "import-after-crash",
    }
    interrupted = await _dispatch(env, "documents.import", params)
    assert interrupted.error is not None
    assert interrupted.error.code == "UNAVAILABLE"

    service = await ArtifactSessionService.from_session_storage(env.storage)
    attempt = await service.get_document_import_attempt(
        session_id=env.session.session_id,
        idempotency_key="import-after-crash",
    )
    assert attempt.status is MutationAttemptStatus.RESERVED
    assert await service.list_documents(
        session_key=SESSION_KEY,
        session_id=env.session.session_id,
        limit=10,
    ) == ()
    assert env.store.list_refs(session_id=env.session.session_id, limit=10).refs == ()

    monkeypatch.setattr(resource_rpc, "_ensure_internal_candidate", original)
    recovered = await _dispatch(env, "documents.import", params)
    assert recovered.error is None, recovered.error
    assert recovered.payload["receipt"]["status"] == "applied"
    assert recovered.payload["receipt"]["replayed"] is True
    documents = await service.list_documents(
        session_key=SESSION_KEY,
        session_id=env.session.session_id,
        limit=10,
    )
    assert len(documents) == 1

    other_key = "agent:main:webchat:workbench-resources-other"
    await env.manager.create(other_key)
    cross_session = await _dispatch(
        env,
        "documents.import",
        {
            **params,
            "sessionKey": other_key,
            "idempotencyKey": "cross-session-import",
        },
    )
    assert cross_session.error is not None
    assert cross_session.error.code == "NOT_FOUND"


@pytest.mark.asyncio
async def test_import_expected_hash_is_validated_and_bound_to_idempotency(
    resource_env,
) -> None:
    env = resource_env
    payload = b"<h1>expected hash</h1>"
    attachment = await _append_attachment(
        env,
        message_id="message-expected-hash",
        name="expected.html",
        payload=payload,
        staged=True,
    )
    expected = hashlib.sha256(payload).hexdigest()
    service = await ArtifactSessionService.from_session_storage(env.storage)
    changes_before_invalid = env.storage.conn.total_changes
    base_params = {
        "sessionKey": SESSION_KEY,
        "source": {"type": "attachment", "id": attachment["attachment_id"]},
        "mode": "copy",
        "clientRequestId": "expected-hash-import",
    }
    for rejected_params in (
        base_params,
        {**base_params, "expectedSha256": "not-a-sha256"},
        {
            **base_params,
            "expectedSha256": expected,
            "idempotencyKey": "different-request-id",
        },
    ):
        rejected = await _dispatch(env, "documents.import", rejected_params)
        assert rejected.error is not None
        assert rejected.error.code == "INVALID_REQUEST"
    assert env.storage.conn.total_changes == changes_before_invalid
    assert await service.list_documents(
        session_key=SESSION_KEY,
        session_id=env.session.session_id,
        limit=10,
    ) == ()

    params = {
        **base_params,
        "expectedSha256": expected,
    }
    imported = await _dispatch(env, "documents.import", params)
    assert imported.error is None, imported.error
    assert imported.payload["document"]["documentId"] == imported.payload["document"]["id"]
    assert imported.payload["revision"]["revisionId"] == imported.payload["revision"]["id"]
    assert imported.payload["document"]["head"]["revisionId"] == imported.payload[
        "revision"
    ]["revisionId"]
    assert imported.payload["binding"]["sourceSha256"] == expected
    assert imported.payload["receipt"]["requestId"] == "expected-hash-import"
    assert imported.payload["receipt"]["idempotencyKey"] == "expected-hash-import"

    replay = await _dispatch(
        env,
        "documents.import",
        {**params, "idempotencyKey": "expected-hash-import"},
    )
    assert replay.error is None, replay.error
    assert replay.payload["receipt"]["replayed"] is True
    assert replay.payload["document"]["id"] == imported.payload["document"]["id"]

    mismatch = await _dispatch(
        env,
        "documents.import",
        {**params, "expectedSha256": "0" * 64},
    )
    assert mismatch.error is not None
    assert mismatch.error.code == "DOCUMENT_RESOURCE_CONFLICT"


@pytest.mark.asyncio
async def test_import_concurrent_same_request_creates_one_initial_revision(resource_env) -> None:
    env = resource_env
    payload = b"<h1>double click</h1>"
    attachment = await _append_attachment(
        env,
        message_id="message-concurrent-import",
        name="double-click.html",
        payload=payload,
        staged=True,
    )
    params = {
        "sessionKey": SESSION_KEY,
        "source": {
            "type": "attachment",
            "attachmentId": attachment["attachment_id"],
        },
        "mode": "copy",
        "expectedSha256": hashlib.sha256(payload).hexdigest(),
        "clientRequestId": "concurrent-double-click",
    }

    first, second = await asyncio.gather(
        _dispatch(env, "documents.import", params),
        _dispatch(env, "documents.import", params),
    )

    assert first.error is None, first.error
    assert second.error is None, second.error
    responses = (first.payload, second.payload)
    assert {response["document"]["documentId"] for response in responses} == {
        first.payload["document"]["documentId"]
    }
    assert {response["revision"]["revisionId"] for response in responses} == {
        first.payload["revision"]["revisionId"]
    }
    assert sorted(response["receipt"]["replayed"] for response in responses) == [False, True]

    service = await ArtifactSessionService.from_session_storage(env.storage)
    documents = await service.list_documents(
        session_key=SESSION_KEY,
        session_id=env.session.session_id,
        limit=10,
    )
    assert len(documents) == 1
    revisions = await service.list_revisions(documents[0].document_id, limit=10)
    assert len(revisions) == 1
    assert revisions[0].generation == 1
    assert revisions[0].source.value == "initial"


@pytest.mark.asyncio
async def test_resource_pagination_is_stable_and_url_type_is_reserved(
    resource_env,
) -> None:
    env = resource_env
    for index in range(3):
        await _append_attachment(
            env,
            message_id=f"message-page-{index}",
            name=f"page-{index}.html",
            payload=f"<h1>{index}</h1>".encode(),
            staged=True,
        )

    first = await _dispatch(
        env,
        "workbench.resources.list",
        {"sessionKey": SESSION_KEY, "types": ["attachment"], "limit": 1},
    )
    assert first.error is None, first.error
    assert first.payload["returnedCount"] == 1
    assert first.payload["hasMore"] is True
    assert isinstance(first.payload["nextCursor"], str)
    second = await _dispatch(
        env,
        "workbench.resources.list",
        {
            "sessionKey": SESSION_KEY,
            "types": ["attachment"],
            "limit": 1,
            "cursor": first.payload["nextCursor"],
        },
    )
    assert second.error is None, second.error
    assert second.payload["resources"][0]["resource"]["id"] != first.payload[
        "resources"
    ][0]["resource"]["id"]

    await _append_attachment(
        env,
        message_id="message-page-inventory-change",
        name="new.html",
        payload=b"<h1>new</h1>",
        staged=True,
    )
    stale = await _dispatch(
        env,
        "workbench.resources.list",
        {
            "sessionKey": SESSION_KEY,
            "types": ["attachment"],
            "limit": 1,
            "cursor": first.payload["nextCursor"],
        },
    )
    assert stale.error is not None
    assert stale.error.code == "WORKBENCH_CURSOR_STALE"

    urls = await _dispatch(
        env,
        "workbench.resources.list",
        {"sessionKey": SESSION_KEY, "types": ["url"]},
    )
    assert urls.error is None, urls.error
    assert urls.payload["resources"] == []


@pytest.mark.parametrize(
    ("filename", "mime"),
    (
        (
            "brief.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
        (
            "brief.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ),
        (
            "brief.pptx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ),
    ),
)
@pytest.mark.asyncio
async def test_office_resource_exposes_stable_edit_unavailable_reason(
    resource_env,
    filename: str,
    mime: str,
) -> None:
    env = resource_env
    attachment = await _append_attachment(
        env,
        message_id=f"message-office-{filename}",
        name=filename,
        payload=b"synthetic-office-bytes",
        staged=True,
        mime=mime,
    )
    listed = await _dispatch(
        env,
        "workbench.resources.get",
        {
            "sessionKey": SESSION_KEY,
            "resource": {"type": "attachment", "id": attachment["attachment_id"]},
        },
    )
    assert listed.error is None, listed.error
    capabilities = listed.payload["resource"]["capabilities"]
    assert capabilities["preview"] is False
    assert capabilities["edit"] is False
    assert capabilities["editReasonCode"] == "office_adapter_not_available"

    forged_import = await _dispatch(
        env,
        "documents.import",
        {
            "sessionKey": SESSION_KEY,
            "source": {"type": "attachment", "id": attachment["attachment_id"]},
            "mode": "copy",
            "expectedSha256": attachment["sha256_ref"],
            "idempotencyKey": f"office-import-must-fail-closed-{filename}",
        },
    )
    assert forged_import.error is not None
    assert forged_import.error.code == "DOCUMENT_IMPORT_FORMAT_UNSUPPORTED"
    service = await ArtifactSessionService.from_session_storage(env.storage)
    assert await service.list_documents(
        session_key=SESSION_KEY,
        session_id=env.session.session_id,
        limit=10,
    ) == ()


@pytest.mark.asyncio
async def test_publish_receipt_pins_immutable_revision_and_recovers_promotion(
    resource_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = resource_env
    emitted: list[tuple[str, str, dict[str, object]]] = []

    async def _capture_event(self, session_key, event_name, payload=None, **_kwargs):
        emitted.append((session_key, event_name, dict(payload or {})))

    monkeypatch.setattr(resource_rpc.EventBridge, "emit", _capture_event)
    source = b"<h1>published once</h1>"
    attachment = await _append_attachment(
        env,
        message_id="message-publish",
        name="publish.html",
        payload=source,
        staged=True,
    )
    imported = await _import_attachment(
        env,
        str(attachment["attachment_id"]),
        key="import-for-publish",
    )
    document_id = imported["document"]["id"]
    revision_id = imported["revision"]["id"]
    changes_before_missing_revision = env.storage.conn.total_changes
    missing_revision = await _dispatch(
        env,
        "documents.publish",
        {
            "sessionKey": SESSION_KEY,
            "documentId": document_id,
            "clientRequestId": "publish-missing-revision",
        },
    )
    assert missing_revision.error is not None
    assert missing_revision.error.code == "INVALID_REQUEST"
    mismatched_aliases = await _dispatch(
        env,
        "documents.publish",
        {
            "sessionKey": SESSION_KEY,
            "documentId": document_id,
            "revisionId": revision_id,
            "clientRequestId": "publish-alias-one",
            "idempotencyKey": "publish-alias-two",
        },
    )
    assert mismatched_aliases.error is not None
    assert mismatched_aliases.error.code == "INVALID_REQUEST"
    assert env.storage.conn.total_changes == changes_before_missing_revision

    original_promote = ArtifactStore.promote_internal_ref
    failed_once = False

    def _fail_first_promotion(self, **kwargs):
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise OSError("synthetic promotion interruption")
        return original_promote(self, **kwargs)

    monkeypatch.setattr(ArtifactStore, "promote_internal_ref", _fail_first_promotion)
    params = {
        "sessionKey": SESSION_KEY,
        "documentId": document_id,
        "revisionId": revision_id,
        "clientRequestId": "publish-after-crash",
    }
    interrupted = await _dispatch(env, "documents.publish", params)
    assert interrupted.error is not None
    assert interrupted.error.code == "UNAVAILABLE"
    assert env.store.list_refs(session_id=env.session.session_id, limit=10).refs == ()

    monkeypatch.setattr(ArtifactStore, "promote_internal_ref", original_promote)
    recovered = await _dispatch(env, "documents.publish", params)
    assert recovered.error is None, recovered.error
    assert recovered.payload["receipt"]["replayed"] is True
    assert recovered.payload["receipt"]["requestId"] == "publish-after-crash"
    assert recovered.payload["receipt"]["idempotencyKey"] == "publish-after-crash"
    publication = recovered.payload["publication"]
    assert publication["publicationId"] == publication["id"]
    assert publication["revisionId"] == revision_id
    assert publication["sha256"] == hashlib.sha256(source).hexdigest()
    deliverable_id = publication["deliverableId"]
    assert publication["artifactId"] == deliverable_id
    deliverable, path = env.store.resolve_for_download(
        deliverable_id,
        session_id=env.session.session_id,
    )
    assert Path(path).read_bytes() == source
    assert deliverable.sha256 == publication["sha256"]
    assert [event for _key, event, _payload in emitted] == [
        "session.event.artifact",
        "session.event.artifact_state",
        "document.state_changed",
    ]
    assert emitted[0][2]["id"] == deliverable_id
    assert emitted[1][2] == emitted[2][2]
    assert emitted[1][2]["action"] == "document.published"
    assert emitted[1][2]["documentId"] == document_id
    assert emitted[1][2]["revisionId"] == revision_id

    patched = await _dispatch(
        env,
        "artifacts.source.patch",
        {
            "sessionKey": SESSION_KEY,
            "documentId": document_id,
            "expectedHeadRevisionId": revision_id,
            "expectedStateRevision": imported["document"]["stateRevision"],
            "expectedSourceSha256": imported["revision"]["sha256"],
            "patches": [
                {"startOffset": 4, "endOffset": 18, "replacement": "changed"},
            ],
        },
    )
    assert patched.error is None, patched.error
    assert patched.payload["revision"]["sha256"] != publication["sha256"]

    emitted_before_replay = len(emitted)
    replay = await _dispatch(
        env,
        "documents.publish",
        {**params, "idempotencyKey": "publish-after-crash"},
    )
    assert replay.error is None, replay.error
    assert len(emitted) == emitted_before_replay
    assert replay.payload["publication"] == publication
    _same_ref, same_path = env.store.resolve_for_download(
        deliverable_id,
        session_id=env.session.session_id,
    )
    assert Path(same_path).read_bytes() == source


@pytest.mark.asyncio
async def test_publish_explicitly_pins_non_head_revision_and_replays(resource_env) -> None:
    env = resource_env
    source = b"<h1>original</h1>"
    attachment = await _append_attachment(
        env,
        message_id="message-publish-non-head",
        name="non-head.html",
        payload=source,
        staged=True,
    )
    imported = await _import_attachment(
        env,
        str(attachment["attachment_id"]),
        key="import-for-non-head-publish",
    )
    original_revision = imported["revision"]
    document_id = imported["document"]["id"]
    patched = await _dispatch(
        env,
        "artifacts.source.patch",
        {
            "sessionKey": SESSION_KEY,
            "documentId": document_id,
            "expectedHeadRevisionId": original_revision["id"],
            "expectedStateRevision": imported["document"]["stateRevision"],
            "expectedSourceSha256": original_revision["sha256"],
            "patches": [
                {"startOffset": 4, "endOffset": 12, "replacement": "new head"},
            ],
        },
    )
    assert patched.error is None, patched.error
    assert patched.payload["revision"]["id"] != original_revision["id"]

    params = {
        "sessionKey": SESSION_KEY,
        "documentId": document_id,
        "revisionId": original_revision["id"],
        "clientRequestId": "publish-explicit-non-head",
    }
    published = await _dispatch(env, "documents.publish", params)
    assert published.error is None, published.error
    assert published.payload["publication"]["revisionId"] == original_revision["id"]
    assert published.payload["publication"]["sha256"] == original_revision["sha256"]
    _ref, path = env.store.resolve_for_download(
        published.payload["publication"]["deliverableId"],
        session_id=env.session.session_id,
    )
    assert Path(path).read_bytes() == source

    replay = await _dispatch(env, "documents.publish", params)
    assert replay.error is None, replay.error
    assert replay.payload["receipt"]["replayed"] is True
    assert replay.payload["publication"] == published.payload["publication"]


@pytest.mark.asyncio
async def test_session_reset_atomically_retires_applied_and_reserved_import_journals(
    resource_env,
) -> None:
    env = resource_env
    attachment = await _append_attachment(
        env,
        message_id="message-reset",
        name="reset.html",
        payload=b"<h1>reset</h1>",
        staged=True,
    )
    await _import_attachment(
        env,
        str(attachment["attachment_id"]),
        key="applied-before-reset",
    )
    service = await ArtifactSessionService.from_session_storage(env.storage)
    await service.reserve_document_import_attempt(
        session_key=SESSION_KEY,
        session_id=env.session.session_id,
        idempotency_key="reserved-before-reset",
        source_type=DocumentSourceType.ATTACHMENT,
        source_resource_id="att_reserved_before_reset",
        source_sha256="a" * 64,
        source_name="reserved.html",
        source_mime="text/html",
        source_size=1,
        document_name="reserved.html",
        mode=DocumentImportMode.COPY,
        candidate_artifact_id=ArtifactStore.allocate_artifact_id(),
    )

    old_session_id = env.session.session_id
    reset = await _dispatch(env, "sessions.reset", {"key": SESSION_KEY})
    assert reset.error is None, reset.error
    assert reset.payload["session_id"] != old_session_id

    for table in (
        "artifact_documents",
        "document_source_bindings",
        "document_import_attempts",
        "document_publications",
        "document_publish_attempts",
    ):
        cursor = await env.storage.conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE session_id = ?",  # noqa: S608
            (old_session_id,),
        )
        try:
            row = await cursor.fetchone()
        finally:
            await cursor.close()
        assert row is not None and int(row[0]) == 0, table


@pytest.mark.asyncio
async def test_legacy_document_open_preserves_preview_identity_and_is_idempotent(
    resource_env,
) -> None:
    env = resource_env
    ref = env.store.publish_bytes(
        b"<h1>legacy open</h1>",
        session_id=env.session.session_id,
        session_key=SESSION_KEY,
        name="legacy.html",
        mime="text/html",
        source="legacy-open-test",
    )
    params = {"sessionKey": SESSION_KEY, "artifactId": ref.id}
    first = await _dispatch(env, "artifacts.documents.open", params)
    second = await _dispatch(env, "artifacts.documents.open", params)
    assert first.error is None, first.error
    assert second.error is None, second.error
    assert first.payload["adopted"] is True
    assert second.payload["adopted"] is False
    assert first.payload["document"]["id"] == second.payload["document"]["id"]
    assert first.payload["document"]["head"]["artifactId"] == ref.id
    assert first.payload["document"]["head"]["sha256"] == ref.sha256

    imported = await _dispatch(
        env,
        "documents.import",
        {
            "sessionKey": SESSION_KEY,
            "source": {"type": "deliverable", "id": ref.id},
            "mode": "copy",
            "expectedSha256": ref.sha256,
            "idempotencyKey": "explicit-copy-after-legacy-open",
        },
    )
    assert imported.error is None, imported.error
    assert imported.payload["document"]["id"] != first.payload["document"]["id"]
    assert imported.payload["revision"]["artifactId"] != ref.id
    assert imported.payload["revision"]["sha256"] == ref.sha256
