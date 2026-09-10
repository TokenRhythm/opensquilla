"""End-to-end contracts for the additive artifact editing RPC surface."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from starlette.applications import Starlette

import opensquilla.gateway.rpc_artifact_editing as artifact_editing_rpc
from opensquilla.artifact_session import (
    Actor,
    ActorKind,
    ArtifactBlobRef,
    ArtifactSessionService,
)
from opensquilla.artifacts import (
    ArtifactBundle,
    ArtifactBundleSourceFile,
    ArtifactStore,
)
from opensquilla.gateway.artifacts import register_artifact_routes
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage

SESSION_KEY = "agent:main:webchat:artifact-editing"






@pytest.fixture
async def artifact_editing_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    storage = SessionStorage(":memory:")
    await storage.connect()
    media_root = tmp_path / "media"
    manager = SessionManager(
        storage,
        inject_time_prefix=False,
        media_root=media_root,
    )
    session = await manager.create(SESSION_KEY)
    config = SimpleNamespace(
        attachments=SimpleNamespace(
            media_root=str(media_root),
            persist_transcripts=True,
        ),
        state_dir=str(tmp_path / "state"),
        workspace_dir=str(tmp_path / "workspace"),
        config_path=None,
    )
    ctx = RpcContext(
        conn_id="artifact-editing-test",
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


async def _adopt_html(env, source: bytes = b"<h1>before</h1>"):
    ref = env.store.publish_bytes(
        source,
        session_id=env.session.session_id,
        session_key=SESSION_KEY,
        name="page.html",
        mime="text/html",
        source="publish_artifact",
    )
    opened = await _dispatch(
        env,
        "artifacts.documents.open",
        {"sessionKey": SESSION_KEY, "artifactId": ref.id},
    )
    assert opened.error is None, opened.error
    return ref, opened.payload["document"]
















@pytest.mark.asyncio
async def test_office_format_capabilities_are_independently_fail_closed(
    artifact_editing_env,
) -> None:
    described = await _dispatch(
        artifact_editing_env,
        "artifacts.edit.capabilities",
        {},
    )
    assert described.error is None, described.error

    for artifact_format in ("docx", "xlsx", "pptx"):
        capabilities = described.payload["formats"][artifact_format]
        assert capabilities["download"] is True
        assert capabilities["preview"] is False
        assert capabilities["selectionContext"] is False
        assert capabilities["manualEdit"] is False
        assert capabilities["agentEdit"] is False
        assert capabilities["publish"] is False
        assert capabilities["unavailableReason"] == "office_adapter_not_available"






























@pytest.mark.asyncio
async def test_concurrent_document_open_adopts_one_stable_document(
    artifact_editing_env,
) -> None:
    env = artifact_editing_env
    ref = env.store.publish_bytes(
        b"<h1>concurrent</h1>",
        session_id=env.session.session_id,
        session_key=SESSION_KEY,
        name="concurrent.html",
        mime="text/html",
        source="concurrent_adoption_test",
    )

    opened = await asyncio.gather(
        *(
            _dispatch(
                env,
                "artifacts.documents.open",
                {"sessionKey": SESSION_KEY, "artifactId": ref.id},
            )
            for _ in range(12)
        )
    )

    assert all(response.error is None for response in opened)
    assert sum(bool(response.payload["adopted"]) for response in opened) == 1
    assert len({response.payload["document"]["id"] for response in opened}) == 1
    listed = await _dispatch(
        env,
        "artifacts.documents.list",
        {"sessionKey": SESSION_KEY},
    )
    assert listed.error is None
    assert len(listed.payload["documents"]) == 1


@pytest.mark.asyncio
async def test_document_mutation_notifications_dual_publish_legacy_and_new_names(
    artifact_editing_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = artifact_editing_env
    _ref, document = await _adopt_html(env)
    service = await ArtifactSessionService.from_session_storage(env.storage)
    emitted: list[tuple[str, str, dict[str, object]]] = []

    class RecordingBridge:
        def __init__(self, _subscription_manager, _connection_registry) -> None:
            pass

        async def emit(
            self,
            session_key: str,
            event_name: str,
            payload: dict[str, object],
        ) -> None:
            emitted.append((session_key, event_name, payload))

    monkeypatch.setattr(artifact_editing_rpc, "EventBridge", RecordingBridge)
    monkeypatch.setattr(artifact_editing_rpc, "get_registry", lambda: object())
    actions = ("revision.restored", "change.reverted")
    for action in actions:
        await artifact_editing_rpc._emit_artifact_state(
            env.ctx,
            session_key=SESSION_KEY,
            service=service,
            document_id=document["id"],
            revision_id=document["headRevisionId"],
            action=action,
        )

    assert [(event_name, payload["action"]) for _, event_name, payload in emitted] == [
        pair
        for action in actions
        for pair in (
            ("session.event.artifact_state", action),
            ("document.state_changed", action),
        )
    ]




















@pytest.mark.asyncio
async def test_restore_and_applied_change_revert_preserve_history(
    artifact_editing_env,
) -> None:
    env = artifact_editing_env
    _ref, initial_document = await _adopt_html(env)
    initial_revision_id = initial_document["headRevisionId"]
    patched = await _save_version(env, initial_document, b"<h1>after</h1>")
    patched_document = patched.payload["document"]

    restore_params = {
        "sessionKey": SESSION_KEY,
        "documentId": initial_document["id"],
        "revisionId": initial_revision_id,
        "expectedHeadRevisionId": patched_document["headRevisionId"],
        "expectedStateRevision": patched_document["stateRevision"],
        "clientRequestId": "restore-response-replay",
    }
    restored = await _dispatch(env, "artifacts.revisions.restore", restore_params)
    assert restored.error is None, restored.error
    assert restored.payload["revision"]["id"] == initial_revision_id
    assert restored.payload["revision"]["source"] != "restore"
    assert restored.payload["document"]["headRevisionId"] == initial_revision_id
    assert restored.payload["document"]["generation"] == patched_document["generation"]
    assert restored.payload["document"]["stateRevision"] == patched_document["stateRevision"] + 1
    assert restored.payload["changeSet"]["validation"]["restore_mode"] == "head_pointer"
    assert restored.payload["changeSet"]["validation"]["no_op"] is False
    assert restored.payload["receipt"]["changeSetId"] == restored.payload["changeSet"]["id"]
    restore_replay = await _dispatch(env, "artifacts.revisions.restore", restore_params)
    assert restore_replay.error is None, restore_replay.error
    assert restore_replay.payload == restored.payload
    service = await ArtifactSessionService.from_session_storage(env.storage)
    restore_revision_count = len(await service.list_revisions(initial_document["id"]))
    assert restore_revision_count == 2
    restore_change_count = len(await service.list_change_sets(initial_document["id"]))
    stale_restore = await _dispatch(
        env,
        "artifacts.revisions.restore",
        {**restore_params, "clientRequestId": "stale-restore"},
    )
    assert stale_restore.error is not None
    assert stale_restore.error.code == "DOCUMENT_CHANGED"
    assert len(await service.list_revisions(initial_document["id"])) == restore_revision_count
    assert len(await service.list_change_sets(initial_document["id"])) == restore_change_count

    base_document = await service.get_document(initial_document["id"])
    change_set = await service.create_change_set(
        document_id=base_document.document_id,
        base_revision_id=base_document.head_revision_id,
        operations=({"op": "replace_text", "text": "agent version"},),
        actor=Actor(ActorKind.AGENT, "test-agent"),
    )
    candidate_ref = env.store.publish_bytes(
        b"<h1>agent version</h1>",
        session_id=env.session.session_id,
        session_key=SESSION_KEY,
        name="page.html",
        mime="text/html",
        source="test_agent_edit",
        visibility="internal",
    )
    candidate = ArtifactBlobRef(
        artifact_id=candidate_ref.id,
        sha256=candidate_ref.sha256,
        filename=candidate_ref.name,
        media_type=candidate_ref.mime,
        byte_size=candidate_ref.size,
    )
    ready = await service.ready_change_set(
        change_set_id=change_set.change_set_id,
        expected_state_revision=change_set.state_revision,
        candidate_artifact=candidate,
        validation={"ok": True},
        actor=Actor(ActorKind.AGENT, "test-agent"),
    )
    applied = await service.apply_change_set(
        change_set_id=ready.change_set_id,
        expected_change_set_state_revision=ready.state_revision,
        expected_head_revision_id=base_document.head_revision_id,
        expected_document_state_revision=base_document.state_revision,
        actor=Actor(ActorKind.AGENT, "test-agent"),
    )
    restore_replay_after_later_head = await _dispatch(
        env,
        "artifacts.revisions.restore",
        restore_params,
    )
    assert restore_replay_after_later_head.error is None
    assert restore_replay_after_later_head.payload["receipt"] == restored.payload["receipt"]
    assert restore_replay_after_later_head.payload["revision"] == restored.payload["revision"]
    assert (
        restore_replay_after_later_head.payload["document"]["headRevisionId"]
        == applied.revision.revision_id
    )
    fetched_change = await _dispatch(
        env,
        "artifacts.changes.get",
        {
            "sessionKey": SESSION_KEY,
            "documentId": base_document.document_id,
            "changeSetId": ready.change_set_id,
        },
    )
    assert fetched_change.error is None
    assert fetched_change.payload["changeSet"]["turnId"] is None
    assert fetched_change.payload["changeSet"]["summary"] == ""
    assert fetched_change.payload["changeSet"]["candidateArtifact"] == {
        "id": candidate_ref.id,
        "sha256": candidate_ref.sha256,
        "name": candidate_ref.name,
        "mime": candidate_ref.mime,
        "size": candidate_ref.size,
    }

    revert_params = {
        "sessionKey": SESSION_KEY,
        "documentId": base_document.document_id,
        "changeSetId": ready.change_set_id,
        "expectedHeadRevisionId": applied.revision.revision_id,
        "expectedStateRevision": applied.document.state_revision,
        "clientRequestId": "revert-response-replay",
    }
    reverted = await _dispatch(env, "artifacts.changes.revert", revert_params)
    assert reverted.error is None, reverted.error
    assert reverted.payload["revision"]["source"] == "revert"
    assert reverted.payload["revision"]["copiedFromRevisionId"] == base_document.head_revision_id
    assert (
        reverted.payload["revision"]["changeSetId"] == reverted.payload["mutationChangeSet"]["id"]
    )
    assert reverted.payload["receipt"]["changeSetId"] == reverted.payload["mutationChangeSet"]["id"]
    revert_replay = await _dispatch(env, "artifacts.changes.revert", revert_params)
    assert revert_replay.error is None, revert_replay.error
    assert revert_replay.payload == reverted.payload

    stale_revert = await _dispatch(
        env,
        "artifacts.changes.revert",
        {
            "sessionKey": SESSION_KEY,
            "documentId": base_document.document_id,
            "changeSetId": ready.change_set_id,
            "expectedHeadRevisionId": reverted.payload["revision"]["id"],
            "expectedStateRevision": reverted.payload["document"]["stateRevision"],
        },
    )
    assert stale_revert.error is not None
    assert stale_revert.error.code == "DOCUMENT_CHANGED"
    assert stale_revert.error.details == {"reasonCode": "change_not_current"}

    reverted_source = await _dispatch(
        env,
        "artifacts.source.read",
        {"sessionKey": SESSION_KEY, "documentId": base_document.document_id},
    )
    assert reverted_source.error is None
    later_patch = await _save_version(env, reverted.payload["document"], b"<h1>later</h1>")
    revision_count = len(await service.list_revisions(base_document.document_id))
    change_count = len(await service.list_change_sets(base_document.document_id))
    revert_replay_after_later_head = await _dispatch(
        env,
        "artifacts.changes.revert",
        revert_params,
    )
    assert revert_replay_after_later_head.error is None
    assert revert_replay_after_later_head.payload["receipt"] == reverted.payload["receipt"]
    assert revert_replay_after_later_head.payload["revision"] == reverted.payload["revision"]
    assert (
        revert_replay_after_later_head.payload["document"]["headRevisionId"]
        == later_patch.payload["revision"]["id"]
    )
    assert len(await service.list_revisions(base_document.document_id)) == revision_count
    assert len(await service.list_change_sets(base_document.document_id)) == change_count

    revisions = await service.list_revisions(base_document.document_id)
    assert [item.generation for item in revisions] == [5, 4, 3, 2, 1]
    assert applied.revision.generation == 3
    assert applied.revision.parent_revision_id == initial_revision_id
    assert reverted.payload["revision"]["generation"] == 4
    assert revisions[0].parent_revision_id == revisions[1].revision_id
    assert len(await service.list_change_sets(base_document.document_id)) == 5
















@pytest.mark.asyncio
async def test_session_reset_and_delete_fence_documents(
    artifact_editing_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = artifact_editing_env
    await _adopt_html(env)

    reset = await _dispatch(env, "sessions.reset", {"key": SESSION_KEY})
    assert reset.error is None, reset.error
    listed_after_reset = await _dispatch(
        env,
        "artifacts.documents.list",
        {"sessionKey": SESSION_KEY},
    )
    assert listed_after_reset.error is None
    assert listed_after_reset.payload["documents"] == []

    current = await env.manager.get_session(SESSION_KEY)
    assert current is not None
    new_ref = env.store.publish_bytes(
        b"<h1>new epoch</h1>",
        session_id=current.session_id,
        session_key=SESSION_KEY,
        name="new-epoch.html",
        mime="text/html",
        source="publish_artifact",
    )
    opened = await _dispatch(
        env,
        "artifacts.documents.open",
        {"sessionKey": SESSION_KEY, "artifactId": new_ref.id},
    )
    assert opened.error is None

    approval_queue = SimpleNamespace(expire_pending_for_session=lambda _key: None)
    monkeypatch.setattr(
        "opensquilla.gateway.approval_queue.get_approval_queue",
        lambda: approval_queue,
    )
    deleted = await _dispatch(env, "sessions.delete", {"key": SESSION_KEY})
    assert deleted.error is None, deleted.error
    assert deleted.payload["deleted"] == [SESSION_KEY]

    recreated = await env.manager.create(SESSION_KEY)
    assert recreated.session_id not in {
        reset.payload["previous_session_id"],
        reset.payload["session_id"],
    }
    listed_after_recreate = await _dispatch(
        env,
        "artifacts.documents.list",
        {"sessionKey": SESSION_KEY},
    )
    assert listed_after_recreate.error is None
    assert listed_after_recreate.payload["documents"] == []


@pytest.mark.asyncio
async def test_session_fork_copies_only_document_heads_without_review_state(
    artifact_editing_env,
) -> None:
    env = artifact_editing_env
    _ref, document = await _adopt_html(env)
    patched = await _save_version(env, document, b"<h1>fork head</h1>")
    parent_head_id = patched.payload["document"]["headRevisionId"]
    service = await ArtifactSessionService.from_session_storage(env.storage)
    parent_head_artifact_id = (await service.get_revision(parent_head_id)).artifact_id

    forked = await _dispatch(env, "sessions.fork", {"key": SESSION_KEY})
    assert forked.error is None, forked.error
    child_key = forked.payload["key"]
    child_session = await env.manager.get_session(child_key)
    assert child_session is not None
    child_documents = await service.list_documents(
        session_key=child_key,
        session_id=child_session.session_id,
    )
    assert len(child_documents) == 1
    child_document = child_documents[0]
    child_revisions = await service.list_revisions(child_document.document_id)
    assert len(child_revisions) == 1
    assert child_revisions[0].generation == 1
    assert child_revisions[0].parent_revision_id is None
    assert child_revisions[0].copied_from_revision_id == parent_head_id
    assert (
        await service.list_prompt_annotations(
            session_key=child_key,
            session_id=child_session.session_id,
            session_epoch=await env.storage.get_epoch(child_key),
        )
        == ()
    )

    cursor = await env.storage.conn.execute(
        "SELECT COUNT(*) FROM artifact_edit_sessions WHERE document_id = ?",
        (child_document.document_id,),
    )
    try:
        row = await cursor.fetchone()
        assert row is not None and row[0] == 0
    finally:
        await cursor.close()

    child_legacy_artifacts = await _dispatch(
        env,
        "artifacts.list",
        {"sessionKey": child_key},
    )
    assert child_legacy_artifacts.error is None
    assert parent_head_artifact_id not in {
        item["id"] for item in child_legacy_artifacts.payload["artifacts"]
    }

    app = Starlette(debug=False)
    register_artifact_routes(app, config=env.config, session_manager=env.manager)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        downloaded = await client.get(
            f"/api/v1/artifact-documents/{child_document.document_id}",
            params={"sessionKey": child_key},
        )
    assert downloaded.status_code == 200
    assert downloaded.content == b"<h1>fork head</h1>"


@pytest.mark.asyncio
async def test_stable_document_download_serves_latest_and_historical_revisions(
    artifact_editing_env,
) -> None:
    env = artifact_editing_env
    _ref, document = await _adopt_html(env)
    await _save_version(env, document, b"<h1>after</h1>")
    _other_ref, other_document = await _adopt_html(env, b"<h1>other</h1>")

    app = Starlette(debug=False)
    register_artifact_routes(app, config=env.config, session_manager=env.manager)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        latest = await client.get(
            f"/api/v1/artifact-documents/{document['id']}",
            params={"sessionKey": SESSION_KEY},
        )
        historical = await client.get(
            f"/api/v1/artifact-documents/{document['id']}",
            params={
                "sessionKey": SESSION_KEY,
                "revisionId": document["headRevisionId"],
            },
        )
        missing = await client.get(
            f"/api/v1/artifact-documents/{document['id']}",
            params={"sessionKey": "agent:main:webchat:other"},
        )
        cross_document_history = await client.get(
            f"/api/v1/artifact-documents/{document['id']}",
            params={
                "sessionKey": SESSION_KEY,
                "revisionId": other_document["headRevisionId"],
            },
        )

    assert latest.status_code == 200
    assert latest.content == b"<h1>after</h1>"
    assert historical.status_code == 200
    assert historical.content == b"<h1>before</h1>"
    assert missing.status_code == 404
    assert cross_document_history.status_code == 404


@pytest.mark.asyncio
async def test_chat_send_without_context_keeps_instant_accept() -> None:
    ctx = RpcContext(conn_id="no-session-manager", session_manager=None, config=None)
    response = await get_dispatcher().dispatch(
        "chat",
        "chat.send",
        {"sessionKey": SESSION_KEY, "message": "hello"},
        ctx,
    )

    assert response.error is None
    assert response.payload["instant_accept"] is True


async def test_unscoped_legacy_document_never_materializes_session_working_files(
    artifact_editing_env,
    monkeypatch,
):
    from dataclasses import replace
    from unittest.mock import AsyncMock

    env = artifact_editing_env
    _ref, opened = await _adopt_html(env)
    service = await ArtifactSessionService.from_session_storage(env.storage)
    document = await service.get_document(opened["id"])
    get_working_files = AsyncMock()
    monkeypatch.setattr(artifact_editing_rpc, "get_working_files", get_working_files)

    await artifact_editing_rpc._sync_restored_working_files(
        env.ctx, service, replace(document, session_id=None)
    )

    get_working_files.assert_not_awaited()


async def test_bundle_source_read_resolves_html_entry_and_preserves_unicode(artifact_editing_env):

    env = artifact_editing_env
    ref = env.store.publish_bundle(
        ArtifactBundle(
            entrypoint="index.html",
            files=(
                ArtifactBundleSourceFile(
                    path="index.html",
                    mime="text/html",
                    data=(
                        '<!doctype html><meta charset="utf-8">'
                        '<link rel="stylesheet" href="style.css">'
                        "<h1>你好 🌏</h1>"
                    ).encode(),
                ),
                ArtifactBundleSourceFile(
                    path="style.css", mime="text/css", data=b"h1 { color: navy; }"
                ),
            ),
        ),
        session_id=env.session.session_id,
        session_key=SESSION_KEY,
        name="index.html",
        mime="text/html",
        source="test",
    )
    opened = await _dispatch(
        env,
        "artifacts.documents.open",
        {
            "sessionKey": SESSION_KEY,
            "artifactId": ref.id,
        },
    )
    assert opened.error is None, opened.error
    source = await _dispatch(
        env,
        "artifacts.source.read",
        {
            "sessionKey": SESSION_KEY,
            "documentId": opened.payload["document"]["id"],
        },
    )
    assert source.error is None, source.error
    assert "你好 🌏" in source.payload["source"]["text"]
    assert opened.payload["document"]["capabilities"]["source"] is True


@pytest.mark.parametrize(
    "method",
    [
        "documents.editSessions.start",
        "documents.editSessions.heartbeat",
        "documents.editSessions.close",
        "artifacts.prompt_annotations.create",
        "artifacts.prompt_annotations.focus",
        "artifacts.prompt_annotations.update",
        "artifacts.prompt_annotations.discard",
        "artifacts.source.patch",
    ],
)
async def test_retired_editor_methods_return_actionable_upgrade_errors(
    artifact_editing_env, method
):
    env = artifact_editing_env
    response = await _dispatch(env, method, {"sessionKey": SESSION_KEY})
    assert response.error is not None
    assert response.error.code == "DOCUMENT_EDITING_RETIRED"
    assert response.error.details == {"action": "update_client_and_reopen_page"}
    assert (
        await (await ArtifactSessionService.from_session_storage(env.storage)).list_documents(
            session_key=SESSION_KEY, session_id=env.session.session_id
        )
        == ()
    )


async def test_html_exposes_read_only_source_without_editor_capabilities(artifact_editing_env):
    _ref, document = await _adopt_html(artifact_editing_env)
    capabilities = document["capabilities"]
    assert capabilities["preview"] is True
    assert capabilities["source"] is True
    for name in ("manualEdit", "agentEdit", "sourceEdit", "promptAnnotations", "selection"):
        assert capabilities[name] is False


async def _save_version(env, document, data: bytes):
    ref = env.store.publish_bytes(
        data,
        session_id=env.session.session_id,
        session_key=SESSION_KEY,
        name="page.html",
        mime="text/html",
        source="test-version",
        visibility="internal",
    )
    service = await ArtifactSessionService.from_session_storage(env.storage)
    result, change = await service.commit_change_set_atomically(
        document_id=document["id"],
        base_revision_id=document["headRevisionId"],
        expected_document_state_revision=document["stateRevision"],
        operations=({"op": "save_files"},),
        candidate_artifact=ArtifactBlobRef(
            artifact_id=ref.id,
            sha256=ref.sha256,
            filename=ref.name,
            media_type=ref.mime,
            byte_size=ref.size,
        ),
        validation=None,
        actor=Actor(ActorKind.AGENT, "test-agent"),
        turn_id=f"test-version:{document['stateRevision']}",
    )
    return SimpleNamespace(
        payload={
            "document": await artifact_editing_rpc._mutation_document_payload(
                env.ctx, service, result
            ),
            "revision": artifact_editing_rpc._revision_payload(result.revision),
            "changeSet": artifact_editing_rpc._change_set_payload(change),
        }
    )


async def test_source_read_prefers_working_file_and_restore_reconciles_it(artifact_editing_env):
    from opensquilla.artifact_session.working_files import ensure_working_files

    env = artifact_editing_env
    _ref, document = await _adopt_html(env)
    service = await ArtifactSessionService.from_session_storage(env.storage)
    binding = await ensure_working_files(
        service,
        env.store,
        document_id=document["id"],
        session_key=SESSION_KEY,
        session_id=env.session.session_id,
        workspace=env.config.workspace_dir,
    )
    binding.entry.write_text("<h1>ordinary file edit</h1>", encoding="utf-8")
    source = await _dispatch(
        env,
        "artifacts.source.read",
        {
            "sessionKey": SESSION_KEY,
            "documentId": document["id"],
        },
    )
    assert source.error is None, source.error
    assert source.payload["source"]["text"] == "<h1>ordinary file edit</h1>"
    restored = await _dispatch(
        env,
        "artifacts.revisions.restore",
        {
            "sessionKey": SESSION_KEY,
            "documentId": document["id"],
            "revisionId": document["headRevisionId"],
            "expectedHeadRevisionId": document["headRevisionId"],
            "expectedStateRevision": document["stateRevision"],
            "clientRequestId": "restore-working-file",
        },
    )
    assert restored.error is None, restored.error
    assert binding.entry.read_text(encoding="utf-8") == "<h1>before</h1>"
    assert restored.payload["revision"]["id"] == document["headRevisionId"]
    assert restored.payload["document"]["generation"] == document["generation"]
    assert restored.payload["changeSet"]["validation"]["no_op"] is False
    assert len(await service.list_revisions(document["id"])) == 1


async def test_source_read_rejects_another_session_and_invalid_encoding(artifact_editing_env):
    env = artifact_editing_env
    _ref, document = await _adopt_html(env, b"<h1>\xff</h1>")
    response = await _dispatch(
        env,
        "artifacts.source.read",
        {
            "sessionKey": SESSION_KEY,
            "documentId": document["id"],
        },
    )
    assert response.error is not None
    assert response.error.code == "RESOURCE_UNSUPPORTED"
    other_key = "agent:main:webchat:other"
    await env.manager.create(other_key)
    denied = await _dispatch(
        env,
        "artifacts.source.read",
        {
            "sessionKey": other_key,
            "documentId": document["id"],
        },
    )
    assert denied.error is not None
    assert denied.error.code == "DOCUMENT_UNAVAILABLE"


async def test_restore_current_revision_has_no_visible_history(artifact_editing_env, monkeypatch):
    from unittest.mock import AsyncMock

    from opensquilla.artifact_session.working_files import ensure_working_files

    env = artifact_editing_env
    _ref, opened = await _adopt_html(env)
    service = await ArtifactSessionService.from_session_storage(env.storage)
    binding = await ensure_working_files(
        service,
        env.store,
        document_id=opened["id"],
        session_key=SESSION_KEY,
        session_id=env.session.session_id,
        workspace=env.config.workspace_dir,
    )
    before = await service.get_document(opened["id"])
    revisions = await service.list_revisions(opened["id"])
    changes = await service.list_change_sets(opened["id"])
    audits = await service.list_audit_events(opened["id"])
    emitted = AsyncMock()
    monkeypatch.setattr(artifact_editing_rpc.EventBridge, "emit", emitted)
    params = {
        "sessionKey": SESSION_KEY,
        "documentId": opened["id"],
        "revisionId": opened["headRevisionId"],
        "expectedHeadRevisionId": opened["headRevisionId"],
        "expectedStateRevision": opened["stateRevision"],
        "clientRequestId": "restore-current-no-change",
    }
    response = await _dispatch(env, "artifacts.revisions.restore", params)
    assert response.error is None, response.error
    assert response.payload["revision"]["id"] == before.head_revision_id
    assert response.payload["receipt"]["resultRevisionId"] == before.head_revision_id
    assert response.payload["receipt"]["stateRevision"] == before.state_revision
    assert response.payload["changeSet"]["validation"] == {
        "restore_mode": "head_pointer",
        "result_state_revision": before.state_revision,
        "no_op": True,
    }
    assert await service.get_document(opened["id"]) == before
    assert await service.list_revisions(opened["id"]) == revisions
    assert await service.list_change_sets(opened["id"]) == changes
    assert await service.list_audit_events(opened["id"]) == audits
    emitted.assert_not_awaited()
    assert binding.entry.read_bytes() == b"<h1>before</h1>"

    listed = await _dispatch(
        env, "artifacts.changes.list", {"sessionKey": SESSION_KEY, "documentId": opened["id"]}
    )
    assert listed.error is None, listed.error
    assert listed.payload["changeSets"] == []
    replay = await _dispatch(env, "artifacts.revisions.restore", params)
    assert replay.error is None, replay.error
    assert replay.payload == response.payload
    rejected = await _dispatch(
        env,
        "artifacts.changes.revert",
        {
            "sessionKey": SESSION_KEY,
            "documentId": opened["id"],
            "changeSetId": response.payload["changeSet"]["id"],
            "expectedHeadRevisionId": before.head_revision_id,
            "expectedStateRevision": before.state_revision,
            "clientRequestId": "revert-no-change-receipt",
        },
    )
    assert rejected.error is not None
    assert await service.get_document(opened["id"]) == before
    assert await service.list_revisions(opened["id"]) == revisions
    assert await service.list_change_sets(opened["id"]) == changes
    assert await service.list_audit_events(opened["id"]) == audits
    emitted.assert_not_awaited()


@pytest.mark.parametrize("no_op", [False, True], ids=["moved-head", "same-head"])
async def test_restore_request_replay_preserves_later_unsaved_files(artifact_editing_env, no_op):
    from opensquilla.artifact_session.working_files import ensure_working_files

    env = artifact_editing_env
    _ref, initial = await _adopt_html(env)
    current = initial if no_op else (await _save_version(env, initial, b"<h1>v2</h1>")).payload[
        "document"
    ]
    service = await ArtifactSessionService.from_session_storage(env.storage)
    params = {
        "sessionKey": SESSION_KEY,
        "documentId": initial["id"],
        "revisionId": initial["headRevisionId"],
        "expectedHeadRevisionId": current["headRevisionId"],
        "expectedStateRevision": current["stateRevision"],
        "clientRequestId": "restore-before-later-edit",
    }
    restored = await _dispatch(env, "artifacts.revisions.restore", params)
    assert restored.error is None, restored.error
    later = await _save_version(env, restored.payload["document"], b"<h1>Later saved version</h1>")
    binding = await ensure_working_files(
        service,
        env.store,
        document_id=initial["id"],
        session_key=SESSION_KEY,
        session_id=env.session.session_id,
        workspace=env.config.workspace_dir,
    )
    binding.entry.write_bytes(b"<h1>Later unsaved edit must survive</h1>")
    before = await service.get_document(initial["id"])
    revisions = await service.list_revisions(initial["id"])
    audits = await service.list_audit_events(initial["id"])
    replay = await _dispatch(env, "artifacts.revisions.restore", params)
    assert replay.error is None, replay.error
    assert replay.payload["receipt"] == restored.payload["receipt"]
    assert replay.payload["revision"] == restored.payload["revision"]
    assert replay.payload["document"]["headRevisionId"] == later.payload["revision"]["id"]
    assert await service.get_document(initial["id"]) == before
    assert await service.list_revisions(initial["id"]) == revisions
    assert await service.list_audit_events(initial["id"]) == audits
    assert binding.entry.read_bytes() == b"<h1>Later unsaved edit must survive</h1>"
    mismatched = await _dispatch(
        env,
        "artifacts.revisions.restore",
        {**params, "revisionId": later.payload["revision"]["id"]},
    )
    assert mismatched.error is not None
    assert mismatched.error.code == "DOCUMENT_CHANGED"
    assert await service.get_document(initial["id"]) == before
    assert binding.entry.read_bytes() == b"<h1>Later unsaved edit must survive</h1>"


async def test_restore_legacy_copy_receipt_remains_replayable(artifact_editing_env):
    from opensquilla.artifact_session import RevisionSource
    from opensquilla.artifact_session.working_files import ensure_working_files

    env = artifact_editing_env
    _ref, initial = await _adopt_html(env)
    saved = await _save_version(env, initial, b"<h1>v2</h1>")
    current = saved.payload["document"]
    service = await ArtifactSessionService.from_session_storage(env.storage)
    target = await service.get_revision(initial["headRevisionId"])
    request_id = "historical-copy-restore"
    params = {
        "sessionKey": SESSION_KEY,
        "documentId": initial["id"],
        "revisionId": target.revision_id,
        "expectedHeadRevisionId": current["headRevisionId"],
        "expectedStateRevision": current["stateRevision"],
        "clientRequestId": request_id,
    }
    old_result, old_change = await service.commit_change_set_atomically(
        document_id=initial["id"],
        base_revision_id=current["headRevisionId"],
        expected_document_state_revision=current["stateRevision"],
        operations=({
            "op": "restore_revision",
            "target_revision_id": target.revision_id,
            "target_sha256": target.artifact_sha256,
            "expected_document_state_revision": current["stateRevision"],
        },),
        candidate_artifact=target.artifact,
        validation={
            "target_revision_id": target.revision_id,
            "target_sha256": target.artifact_sha256,
            "status": "passed",
        },
        actor=Actor(ActorKind.USER, "historical-user"),
        turn_id=f"revision-restore:{request_id}",
        summary="Restore document revision",
        source=RevisionSource.RESTORE,
        copied_from_revision_id=target.revision_id,
        revision_event_type="document.restored",
    )
    assert old_result.revision.revision_id != target.revision_id
    later = await _save_version(
        env,
        await artifact_editing_rpc._mutation_document_payload(env.ctx, service, old_result),
        b"<h1>Newer version</h1>",
    )
    binding = await ensure_working_files(
        service,
        env.store,
        document_id=initial["id"],
        session_key=SESSION_KEY,
        session_id=env.session.session_id,
        workspace=env.config.workspace_dir,
    )
    binding.entry.write_bytes(b"<h1>Unsaved after upgrade</h1>")
    before = await service.get_document(initial["id"])
    revisions = await service.list_revisions(initial["id"])
    changes = await service.list_change_sets(initial["id"])
    replay = await _dispatch(env, "artifacts.revisions.restore", params)
    assert replay.error is None, replay.error
    assert replay.payload["revision"] == artifact_editing_rpc._revision_payload(old_result.revision)
    assert replay.payload["receipt"]["changeSetId"] == old_change.change_set_id
    assert replay.payload["receipt"]["resultRevisionId"] == old_result.revision.revision_id
    assert replay.payload["receipt"]["stateRevision"] == old_result.document.state_revision
    assert replay.payload["document"]["headRevisionId"] == later.payload["revision"]["id"]
    assert await service.get_document(initial["id"]) == before
    assert await service.list_revisions(initial["id"]) == revisions
    assert await service.list_change_sets(initial["id"]) == changes
    assert binding.entry.read_bytes() == b"<h1>Unsaved after upgrade</h1>"


async def test_revision_list_includes_restored_head_outside_recent_window(artifact_editing_env):
    env = artifact_editing_env
    _ref, original = await _adopt_html(env)
    current = original
    for generation in range(2, 106):
        current = (await _save_version(
            env, current, f"<h1>Version {generation}</h1>".encode()
        )).payload["document"]
    restored = await _dispatch(
        env,
        "artifacts.revisions.restore",
        {
            "sessionKey": SESSION_KEY,
            "documentId": original["id"],
            "revisionId": original["headRevisionId"],
            "expectedHeadRevisionId": current["headRevisionId"],
            "expectedStateRevision": current["stateRevision"],
            "clientRequestId": "restore-first-after-105-versions",
        },
    )
    assert restored.error is None, restored.error
    for options in ({}, {"limit": 3}):
        listed = await _dispatch(
            env,
            "artifacts.revisions.list",
            {"sessionKey": SESSION_KEY, "documentId": original["id"], **options},
        )
        assert listed.error is None, listed.error
        revisions = listed.payload["revisions"]
        ids = [item["id"] for item in revisions]
        assert len(ids) == len(set(ids))
        assert original["headRevisionId"] in ids
        assert current["headRevisionId"] in ids
        assert next(item for item in revisions if item["id"] == original["headRevisionId"])[
            "generation"
        ] == 1
    service = await ArtifactSessionService.from_session_storage(env.storage)
    before = await service.get_document(original["id"])
    assert before.generation == 105
    assert len(await service.list_revisions(original["id"], limit=200)) == 105
    later = await _save_version(env, restored.payload["document"], b"<h1>After restore</h1>")
    assert later.payload["revision"]["generation"] == 106
    assert later.payload["revision"]["parentRevisionId"] == original["headRevisionId"]


async def test_restore_same_head_reconciles_changed_bundle_resource(artifact_editing_env):
    env = artifact_editing_env
    html = b'<link rel="stylesheet" href="style.css"><h1>Same HTML</h1>'
    ref = env.store.publish_bundle(
        ArtifactBundle(
            entrypoint="index.html",
            files=(
                ArtifactBundleSourceFile(path="index.html", mime="text/html", data=html),
                ArtifactBundleSourceFile(
                    path="style.css", mime="text/css", data=b"h1{color:navy}"
                ),
            ),
        ),
        session_id=env.session.session_id,
        session_key=SESSION_KEY,
        name="index.html",
        mime="text/html",
        source="test",
    )
    opened = await _dispatch(
        env,
        "workbench.resources.open",
        {"sessionKey": SESSION_KEY, "resource": {"type": "deliverable", "id": ref.id}},
    )
    assert opened.error is None, opened.error
    document = opened.payload["document"]
    working = Path(opened.payload["workingFile"])
    style = working.parent / "style.css"
    style.write_bytes(b"h1{color:orange}")
    assert working.read_bytes() == html
    restored = await _dispatch(
        env,
        "artifacts.revisions.restore",
        {
            "sessionKey": SESSION_KEY,
            "documentId": document["id"],
            "revisionId": document["headRevisionId"],
            "expectedHeadRevisionId": document["headRevisionId"],
            "expectedStateRevision": document["stateRevision"],
            "clientRequestId": "restore-css-only-working-change",
        },
    )
    assert restored.error is None, restored.error
    assert restored.payload["changeSet"]["validation"]["no_op"] is False
    assert restored.payload["revision"]["id"] == document["headRevisionId"]
    assert restored.payload["document"]["generation"] == document["generation"]
    assert restored.payload["document"]["stateRevision"] == document["stateRevision"] + 1
    assert working.read_bytes() == html
    assert style.read_bytes() == b"h1{color:navy}"
    service = await ArtifactSessionService.from_session_storage(env.storage)
    assert len(await service.list_revisions(document["id"])) == 1
    assert any(
        path.read_bytes() == b"h1{color:orange}"
        for path in (Path(env.config.workspace_dir) / "artifacts").glob("recovered-*/style.css")
    )


@pytest.mark.parametrize("interruption", ["write-error", "cancel"])
async def test_restore_file_interruption_does_not_commit_head_or_receipt(
    artifact_editing_env, monkeypatch, interruption,
):
    import threading
    from unittest.mock import AsyncMock

    from opensquilla.artifact_session import working_files as module

    env = artifact_editing_env
    _ref, initial = await _adopt_html(env)
    changed = await _save_version(env, initial, b"<h1>Saved current version</h1>")
    service = await ArtifactSessionService.from_session_storage(env.storage)
    binding = await module.ensure_working_files(
        service,
        env.store,
        document_id=initial["id"],
        session_key=SESSION_KEY,
        session_id=env.session.session_id,
        workspace=env.config.workspace_dir,
    )
    binding.entry.write_bytes(b"<h1>Unsaved current bytes</h1>")
    before = await service.get_document(initial["id"])
    revisions = await service.list_revisions(initial["id"])
    changes = await service.list_change_sets(initial["id"])
    audits = await service.list_audit_events(initial["id"])
    original_replace = module.os.replace
    entered, release = threading.Event(), threading.Event()
    intercepted = False

    def replace(source, destination):
        nonlocal intercepted
        if Path(destination) == binding.root and Path(source).name.startswith(".materialize-"):
            intercepted = True
            if interruption == "write-error":
                raise OSError("synthetic version replacement failure")
            result = original_replace(source, destination)
            entered.set()
            assert release.wait(5), "test did not release file replacement"
            return result
        return original_replace(source, destination)

    monkeypatch.setattr(module.os, "replace", replace)
    emitted = AsyncMock()
    monkeypatch.setattr(artifact_editing_rpc.EventBridge, "emit", emitted)
    params = {
        "sessionKey": SESSION_KEY,
        "documentId": initial["id"],
        "revisionId": initial["headRevisionId"],
        "expectedHeadRevisionId": changed.payload["revision"]["id"],
        "expectedStateRevision": changed.payload["document"]["stateRevision"],
        "clientRequestId": "restore-interrupted-before-commit",
    }
    if interruption == "write-error":
        response = await _dispatch(env, "artifacts.revisions.restore", params)
        assert response.error is not None
    else:
        task = asyncio.create_task(_dispatch(env, "artifacts.revisions.restore", params))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            assert not task.done()
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert intercepted
    assert await service.get_document(initial["id"]) == before
    assert await service.list_revisions(initial["id"]) == revisions
    assert await service.list_change_sets(initial["id"]) == changes
    assert await service.list_audit_events(initial["id"]) == audits
    assert await service.get_change_set_by_turn(
        document_id=initial["id"], turn_id="revision-restore:restore-interrupted-before-commit"
    ) is None
    assert await module.get_working_files(service, initial["id"]) == binding
    assert binding.entry.read_bytes() == b"<h1>Unsaved current bytes</h1>"
    emitted.assert_not_awaited()


async def test_restore_commit_cancellation_keeps_committed_files_and_receipt(
    artifact_editing_env, monkeypatch,
):
    from opensquilla.artifact_session.working_files import ensure_working_files, get_working_files

    env = artifact_editing_env
    _ref, initial = await _adopt_html(env)
    changed = await _save_version(env, initial, b"<h1>Second saved version</h1>")
    service = await ArtifactSessionService.from_session_storage(env.storage)
    binding = await ensure_working_files(
        service, env.store, document_id=initial["id"], session_key=SESSION_KEY,
        session_id=env.session.session_id, workspace=env.config.workspace_dir,
    )
    binding.entry.write_bytes(b"<h1>Unsaved before restore</h1>")
    before = await service.get_document(initial["id"])
    revisions = await service.list_revisions(initial["id"])
    original_commit = env.storage._commit_transaction
    injected = False

    async def commit_then_cancel(conn, operation, deadline, started):
        nonlocal injected
        await original_commit(conn, operation, deadline, started)
        if operation == "artifact_session.working_files.restore" and not injected:
            injected = True
            raise asyncio.CancelledError

    monkeypatch.setattr(env.storage, "_commit_transaction", commit_then_cancel)
    params = {
        "sessionKey": SESSION_KEY,
        "documentId": initial["id"],
        "revisionId": initial["headRevisionId"],
        "expectedHeadRevisionId": changed.payload["revision"]["id"],
        "expectedStateRevision": changed.payload["document"]["stateRevision"],
        "clientRequestId": "restore-commit-settled-before-cancel",
    }
    with pytest.raises(asyncio.CancelledError):
        await _dispatch(env, "artifacts.revisions.restore", params)
    assert injected
    after = await service.get_document(initial["id"])
    assert after.head_revision_id == initial["headRevisionId"]
    assert after.state_revision == before.state_revision + 1
    assert after.generation == before.generation
    assert await service.list_revisions(initial["id"]) == revisions
    change = await service.get_change_set_by_turn(
        document_id=initial["id"], turn_id="revision-restore:restore-commit-settled-before-cancel"
    )
    assert change is not None
    assert change.applied_revision_id == initial["headRevisionId"]
    assert binding.entry.read_bytes() == b"<h1>before</h1>"
    updated = await get_working_files(service, initial["id"])
    assert updated.base_revision_id == initial["headRevisionId"]
    replay = await _dispatch(env, "artifacts.revisions.restore", params)
    assert replay.error is None, replay.error
    assert replay.payload["receipt"]["changeSetId"] == change.change_set_id
    assert replay.payload["receipt"]["resultRevisionId"] == initial["headRevisionId"]
    assert await service.get_document(initial["id"]) == after
    assert binding.entry.read_bytes() == b"<h1>before</h1>"


@pytest.mark.parametrize("missing", ["entry", "managed-root"])
async def test_restore_same_head_recovers_missing_managed_files(artifact_editing_env, missing):
    import shutil

    from opensquilla.artifact_session.working_files import ensure_working_files

    env = artifact_editing_env
    _ref, initial = await _adopt_html(env)
    service = await ArtifactSessionService.from_session_storage(env.storage)
    binding = await ensure_working_files(
        service, env.store, document_id=initial["id"], session_key=SESSION_KEY,
        session_id=env.session.session_id, workspace=env.config.workspace_dir,
    )
    before = await service.get_document(initial["id"])
    if missing == "entry":
        binding.entry.unlink()
    else:
        shutil.rmtree(binding.root)
    restored = await _dispatch(
        env,
        "artifacts.revisions.restore",
        {
            "sessionKey": SESSION_KEY,
            "documentId": initial["id"],
            "revisionId": initial["headRevisionId"],
            "expectedHeadRevisionId": initial["headRevisionId"],
            "expectedStateRevision": initial["stateRevision"],
            "clientRequestId": "restore-missing-working-files",
        },
    )
    assert restored.error is None, restored.error
    assert restored.payload["revision"]["id"] == initial["headRevisionId"]
    assert restored.payload["document"]["generation"] == before.generation
    assert restored.payload["document"]["stateRevision"] == before.state_revision + 1
    assert restored.payload["changeSet"]["validation"]["no_op"] is False
    assert binding.entry.read_bytes() == b"<h1>before</h1>"
    assert len(await service.list_revisions(initial["id"])) == 1
