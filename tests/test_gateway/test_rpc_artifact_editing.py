"""End-to-end contracts for the additive artifact editing RPC surface."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from lxml import html as lxml_html  # type: ignore[import-untyped]
from starlette.applications import Starlette

import opensquilla.gateway.desktop_artifact_bridge as desktop_artifact_bridge
import opensquilla.gateway.rpc_artifact_editing as artifact_editing_rpc
from opensquilla.artifact_session import (
    Actor,
    ActorKind,
    ArtifactBlobRef,
    ArtifactSessionService,
    MutationAttemptStatus,
)
from opensquilla.artifact_session.html_anchors import (
    canonical_browser_dom_digest,
    canonical_element_at_path,
    canonical_element_proof_sha256,
)
from opensquilla.artifacts import (
    ArtifactBundle,
    ArtifactBundleSourceFile,
    ArtifactError,
    ArtifactStore,
)
from opensquilla.artifacts import (
    ArtifactNotFoundError as ArtifactBlobNotFoundError,
)
from opensquilla.gateway.artifact_mutation_recovery import (
    reconcile_pending_artifact_mutations,
)
from opensquilla.gateway.artifacts import register_artifact_routes
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.gateway.scopes import METHOD_SCOPES, WRITE_SCOPE
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage

SESSION_KEY = "agent:main:webchat:artifact-editing"


def test_prompt_annotation_focus_rpc_requires_operator_write() -> None:
    assert METHOD_SCOPES["artifacts.prompt_annotations.focus"] == WRITE_SCOPE


def test_edit_session_rpcs_require_operator_write() -> None:
    assert METHOD_SCOPES["documents.editSessions.start"] == WRITE_SCOPE
    assert METHOD_SCOPES["documents.editSessions.heartbeat"] == WRITE_SCOPE
    assert METHOD_SCOPES["documents.editSessions.close"] == WRITE_SCOPE


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


def _annotation_proofs(html: str, element_path: str) -> tuple[str, str]:
    parser = lxml_html.HTMLParser(recover=True, no_network=True)
    root = lxml_html.document_fromstring(html, parser=parser)
    dom_sha256 = canonical_browser_dom_digest(root, source=html)
    selected = canonical_element_at_path(root, element_path)
    element_proof_sha256 = canonical_element_proof_sha256(
        root,
        selected=selected,
    )
    return dom_sha256, element_proof_sha256


async def _annotation_row_counts(env) -> tuple[int, int, int]:
    cursor = await env.storage.conn.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM artifact_anchors),
            (SELECT COUNT(*) FROM artifact_prompt_annotations),
            (SELECT COUNT(*) FROM artifact_audit_events
             WHERE event_type = 'anchor.created')
        """
    )
    try:
        row = await cursor.fetchone()
    finally:
        await cursor.close()
    assert row is not None
    return int(row[0]), int(row[1]), int(row[2])


async def _edit_session_row_counts(env) -> tuple[int, int]:
    cursor = await env.storage.conn.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM artifact_edit_sessions),
            (SELECT COUNT(*) FROM artifact_writer_leases)
        """
    )
    try:
        row = await cursor.fetchone()
    finally:
        await cursor.close()
    assert row is not None
    return int(row[0]), int(row[1])


@pytest.mark.asyncio
async def test_prompt_annotation_create_replay_does_not_reuse_native_candidate(
    artifact_editing_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = artifact_editing_env
    html = "<main><p id='target'>one</p></main>"
    _ref, document = await _adopt_html(env, html.encode())
    element_path = json.dumps(
        [["", "html", 1], ["", "body", 1], ["", "main", 1], ["", "p", 1]],
        separators=(",", ":"),
    )
    dom_sha256, element_proof_sha256 = _annotation_proofs(html, element_path)

    class OneShotBridge:
        def __init__(self) -> None:
            self.resolve_calls = 0

        async def resolve_annotation_selection(self, **kwargs):
            self.resolve_calls += 1
            return SimpleNamespace(
                active_preview_artifact_id=kwargs["active_preview_artifact_id"],
                selection_id=kwargs["selection_id"],
                tag_name=kwargs["tag_name"],
                element_path=kwargs["element_path"],
                dom_sha256=kwargs["dom_sha256"],
                element_proof_sha256=kwargs["element_proof_sha256"],
                scope_id=SESSION_KEY,
            )

    bridge = OneShotBridge()
    monkeypatch.setattr(
        artifact_editing_rpc,
        "get_desktop_artifact_bridge_client",
        lambda: bridge,
    )
    params = {
        "annotationId": "annotation-lost-create-response",
        "sessionKey": SESSION_KEY,
        "documentId": document["id"],
        "revisionId": document["headRevisionId"],
        "selection": {
            "selectionId": "selection-one-shot",
            "tagName": "p",
            "elementPath": element_path,
            "domSha256": dom_sha256,
            "elementProofSha256": element_proof_sha256,
        },
        "body": "Shorten this paragraph.",
    }
    created = await _dispatch(env, "artifacts.prompt_annotations.create", params)
    assert created.error is None, created.error
    assert bridge.resolve_calls == 1
    counts_after_commit = await _annotation_row_counts(env)

    # The response was lost and Desktop has already consumed the one-shot
    # candidate. Replaying the same client-owned ID is response recovery, not a
    # second selection or a second durable resource.
    monkeypatch.setattr(
        artifact_editing_rpc,
        "get_desktop_artifact_bridge_client",
        lambda: object(),
    )
    replayed = await _dispatch(env, "artifacts.prompt_annotations.create", params)
    assert replayed.error is None, replayed.error
    assert replayed.payload == created.payload
    assert bridge.resolve_calls == 1
    assert await _annotation_row_counts(env) == counts_after_commit

    for mutation in (
        {"body": "A different instruction."},
        {
            "selection": {
                **params["selection"],
                "elementPath": '[["","html",1],["","body",1],["","main",1]]',
            }
        },
        {
            "selection": {
                **params["selection"],
                "elementProofSha256": "0" * 64,
            }
        },
    ):
        mismatched = await _dispatch(
            env,
            "artifacts.prompt_annotations.create",
            {**params, **mutation},
        )
        assert mismatched.error is not None
        assert mismatched.error.code == "ANNOTATION_BUSY"
        assert bridge.resolve_calls == 1

    other_session_key = "agent:main:webchat:artifact-editing-other"
    await env.manager.create(other_session_key)
    cross_session = await _dispatch(
        env,
        "artifacts.prompt_annotations.create",
        {**params, "sessionKey": other_session_key},
    )
    assert cross_session.error is not None
    assert cross_session.error.code == "DOCUMENT_UNAVAILABLE"
    assert cross_session.error.details == {"reasonCode": "resource_unavailable"}
    assert bridge.resolve_calls == 1

    discarded = await _dispatch(
        env,
        "artifacts.prompt_annotations.discard",
        {
            "sessionKey": SESSION_KEY,
            "annotationId": params["annotationId"],
            "expectedStateRevision": created.payload["annotation"]["stateRevision"],
        },
    )
    assert discarded.error is None, discarded.error
    terminal_replay = await _dispatch(env, "artifacts.prompt_annotations.create", params)
    assert terminal_replay.error is not None
    assert terminal_replay.error.code == "ANNOTATION_BUSY"
    assert bridge.resolve_calls == 1
    assert await _annotation_row_counts(env) == counts_after_commit


class _EchoAnnotationBridge:
    def __init__(self) -> None:
        self.resolve_calls: list[dict[str, object]] = []

    async def resolve_annotation_selection(self, **kwargs):
        self.resolve_calls.append(kwargs)
        return SimpleNamespace(
            active_preview_artifact_id=kwargs["active_preview_artifact_id"],
            selection_id=kwargs["selection_id"],
            tag_name=kwargs["tag_name"],
            element_path=kwargs["element_path"],
            dom_sha256=kwargs["dom_sha256"],
            element_proof_sha256=kwargs["element_proof_sha256"],
            scope_id=SESSION_KEY,
        )


@pytest.mark.asyncio
async def test_preview_prompt_annotations_are_exposed_only_with_desktop_bridge(
    artifact_editing_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = artifact_editing_env
    monkeypatch.setattr(
        artifact_editing_rpc,
        "get_desktop_artifact_bridge_client",
        lambda: None,
    )
    _ref, document = await _adopt_html(env)
    assert document["capabilities"]["sourceEdit"] is True
    assert document["capabilities"]["selectionContext"] is False
    assert document["capabilities"]["promptAnnotations"] is False

    monkeypatch.setattr(
        artifact_editing_rpc,
        "get_desktop_artifact_bridge_client",
        lambda: object(),
    )
    refreshed = await _dispatch(
        env,
        "artifacts.documents.get",
        {"sessionKey": SESSION_KEY, "documentId": document["id"]},
    )
    assert refreshed.error is None, refreshed.error
    assert refreshed.payload["document"]["capabilities"]["selectionContext"] is True
    assert refreshed.payload["document"]["capabilities"]["promptAnnotations"] is True


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
async def test_invalid_utf8_html_never_advertises_source_or_preview_annotations(
    artifact_editing_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = artifact_editing_env
    monkeypatch.setattr(
        artifact_editing_rpc,
        "get_desktop_artifact_bridge_client",
        lambda: object(),
    )
    _ref, document = await _adopt_html(env, b"<h1>\xff</h1>")

    assert document["capabilities"] == {
        "download": True,
        "versionHistory": True,
        "publish": True,
        "promptAnnotations": False,
        "preview": True,
        "selectionContext": False,
        "manualEdit": False,
        "agentEdit": False,
        "sourceEdit": False,
        "browserUse": False,
        "selection": False,
        "engine": None,
        "unavailableReason": "html_source_encoding_unsupported",
    }
    source = await _dispatch(
        env,
        "artifacts.source.read",
        {"sessionKey": SESSION_KEY, "documentId": document["id"]},
    )
    assert source.error is not None
    assert source.error.code == "RESOURCE_UNSUPPORTED"
    assert source.error.details == {"reasonCode": "encoding_unsupported"}


@pytest.mark.asyncio
async def test_complete_single_entrypoint_bundle_supports_source_and_prompt_annotations(
    artifact_editing_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = artifact_editing_env
    html = '<main id="app"><h1>Bundled</h1></main>'
    ref = env.store.publish_bundle(
        ArtifactBundle(
            entrypoint="index.html",
            files=(
                ArtifactBundleSourceFile(
                    path="index.html",
                    mime="text/html",
                    data=html.encode(),
                ),
            ),
        ),
        session_id=env.session.session_id,
        session_key=SESSION_KEY,
        name="single-bundle.html",
        mime="text/html",
        source="artifact_rpc_single_bundle_test",
    )

    element_path = json.dumps(
        [["", "html", 1], ["", "body", 1], ["", "main", 1]],
        separators=(",", ":"),
    )
    _dom_sha256, element_proof_sha256 = _annotation_proofs(html, element_path)

    class FakeBridge:
        async def resolve_annotation_selection(self, **kwargs):
            assert kwargs["dom_sha256"] is None
            return SimpleNamespace(
                active_preview_artifact_id=kwargs["active_preview_artifact_id"],
                selection_id=kwargs["selection_id"],
                tag_name=kwargs["tag_name"],
                element_path=kwargs["element_path"],
                dom_sha256=kwargs["dom_sha256"],
                element_proof_sha256=kwargs["element_proof_sha256"],
                scope_id=SESSION_KEY,
            )

    monkeypatch.setattr(
        artifact_editing_rpc,
        "get_desktop_artifact_bridge_client",
        lambda: FakeBridge(),
    )
    opened = await _dispatch(
        env,
        "artifacts.documents.open",
        {"sessionKey": SESSION_KEY, "artifactId": ref.id},
    )
    assert opened.error is None, opened.error
    document = opened.payload["document"]
    assert document["capabilities"]["sourceEdit"] is True
    assert document["capabilities"]["agentEdit"] is True
    assert document["capabilities"]["selectionContext"] is True
    assert document["capabilities"]["selection"] is True
    assert document["capabilities"]["promptAnnotations"] is True
    assert "unavailableReason" not in document["capabilities"]

    source = await _dispatch(
        env,
        "artifacts.source.read",
        {"sessionKey": SESSION_KEY, "documentId": document["id"]},
    )
    assert source.error is None, source.error
    assert source.payload["source"]["text"] == html
    assert source.payload["source"]["sha256"] == ref.sha256

    created = await _dispatch(
        env,
        "artifacts.prompt_annotations.create",
        {
            "annotationId": "annotation-single-bundle",
            "sessionKey": SESSION_KEY,
            "documentId": document["id"],
            "revisionId": document["headRevisionId"],
            "selection": {
                "selectionId": "selection-single-bundle",
                "tagName": "main",
                "elementPath": element_path,
                "elementProofSha256": element_proof_sha256,
            },
            "body": "Make this section more concise.",
        },
    )
    assert created.error is None, created.error
    annotation = created.payload["annotation"]
    assert annotation["status"] == "draft"
    assert annotation["freshness"] == "current"
    assert annotation["anchor"]["quote"] == '<main id="app">'
    assert annotation["anchor"]["locator"]["source_sha256"] == ref.sha256


@pytest.mark.asyncio
async def test_prompt_annotation_accepts_unrelated_runtime_dom_mutation(
    artifact_editing_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = artifact_editing_env
    source_html = (
        '<main id="shell"><section class="target"><h1>Title</h1></section>'
        '<aside id="status"></aside></main>'
    )
    runtime_html = source_html.replace(
        '<aside id="status"></aside>',
        '<aside id="status"><div class="runtime-log">ready</div></aside>',
    )
    _ref, document = await _adopt_html(env, source_html.encode())
    element_path = json.dumps(
        [["", "html", 1], ["", "body", 1], ["", "main", 1], ["", "section", 1]],
        separators=(",", ":"),
    )
    source_dom_sha256, source_element_proof = _annotation_proofs(source_html, element_path)
    runtime_dom_sha256, runtime_element_proof = _annotation_proofs(runtime_html, element_path)
    assert runtime_dom_sha256 != source_dom_sha256
    assert runtime_element_proof == source_element_proof

    bridge = _EchoAnnotationBridge()
    monkeypatch.setattr(
        artifact_editing_rpc,
        "get_desktop_artifact_bridge_client",
        lambda: bridge,
    )
    created = await _dispatch(
        env,
        "artifacts.prompt_annotations.create",
        {
            "annotationId": "annotation-runtime-log",
            "sessionKey": SESSION_KEY,
            "documentId": document["id"],
            "revisionId": document["headRevisionId"],
            "selection": {
                "selectionId": "selection-runtime-log",
                "tagName": "section",
                "elementPath": element_path,
                # The whole runtime DOM digest deliberately differs. It is an
                # additive bridge echo, never source authorization.
                "domSha256": runtime_dom_sha256,
                "elementProofSha256": runtime_element_proof,
            },
            "body": "Make the title more prominent.",
        },
    )

    assert created.error is None, created.error
    anchor_context = created.payload["annotation"]["anchor"]["context"]
    assert anchor_context["element_path"] == element_path
    assert anchor_context["element_proof_sha256"] == source_element_proof
    assert anchor_context["opening_tag_sha256"] == hashlib.sha256(
        b'<section class="target">'
    ).hexdigest()
    assert anchor_context["semantic_profile_v1"]["tag_name"] == "section"
    assert anchor_context["semantic_profile_v1"]["normalized_text"] == "Title"
    assert "dom_sha256" not in anchor_context
    assert await _annotation_row_counts(env) == (1, 1, 1)


@pytest.mark.asyncio
async def test_prompt_annotation_rejects_wrong_active_artifact_before_persistence(
    artifact_editing_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = artifact_editing_env
    html = '<main id="target"><p>Target</p></main>'
    target_ref, document = await _adopt_html(env, html.encode())
    wrong_ref, _wrong_document = await _adopt_html(
        env,
        b'<main id="other"><p>Other artifact</p></main>',
    )
    element_path = json.dumps(
        [["", "html", 1], ["", "body", 1], ["", "main", 1]],
        separators=(",", ":"),
    )
    dom_sha256, element_proof_sha256 = _annotation_proofs(html, element_path)

    class WrongArtifactBridge(_EchoAnnotationBridge):
        async def resolve_annotation_selection(self, **kwargs):
            self.resolve_calls.append(kwargs)
            return SimpleNamespace(
                active_preview_artifact_id=wrong_ref.id,
                selection_id=kwargs["selection_id"],
                tag_name=kwargs["tag_name"],
                element_path=kwargs["element_path"],
                dom_sha256=kwargs["dom_sha256"],
                element_proof_sha256=kwargs["element_proof_sha256"],
                scope_id=SESSION_KEY,
            )

    bridge = WrongArtifactBridge()
    monkeypatch.setattr(
        artifact_editing_rpc,
        "get_desktop_artifact_bridge_client",
        lambda: bridge,
    )
    rejected = await _dispatch(
        env,
        "artifacts.prompt_annotations.create",
        {
            "annotationId": "annotation-wrong-active-artifact",
            "sessionKey": SESSION_KEY,
            "documentId": document["id"],
            "revisionId": document["headRevisionId"],
            "selection": {
                "selectionId": "selection-wrong-active-artifact",
                "tagName": "main",
                "elementPath": element_path,
                "domSha256": dom_sha256,
                "elementProofSha256": element_proof_sha256,
            },
            "body": "This must stay bound to the target artifact.",
        },
    )

    assert rejected.error is not None
    assert rejected.error.code == "ANNOTATION_UNAVAILABLE"
    assert rejected.error.details == {"reasonCode": "preview_changed"}
    assert rejected.error.accepted is False
    assert bridge.resolve_calls[0]["active_preview_artifact_id"] == target_ref.id
    assert wrong_ref.id != target_ref.id
    assert await _annotation_row_counts(env) == (0, 0, 0)


@pytest.mark.asyncio
async def test_prompt_annotation_head_race_rolls_back_anchor_draft_and_audit(
    artifact_editing_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = artifact_editing_env
    html = '<main id="shell"><section class="target">Title</section></main>'
    _ref, document = await _adopt_html(env, html.encode())
    element_path = json.dumps(
        [["", "html", 1], ["", "body", 1], ["", "main", 1], ["", "section", 1]],
        separators=(",", ":"),
    )
    dom_sha256, element_proof_sha256 = _annotation_proofs(html, element_path)
    bridge = _EchoAnnotationBridge()
    monkeypatch.setattr(
        artifact_editing_rpc,
        "get_desktop_artifact_bridge_client",
        lambda: bridge,
    )

    original_create = ArtifactSessionService.create_prompt_annotation_with_anchor
    raced = False

    async def create_after_head_advance(service, **kwargs):
        nonlocal raced
        if not raced:
            raced = True
            current = await service.get_document(kwargs["document_id"])
            revision = await service.get_revision(current.head_revision_id)
            await service.commit_revision(
                document_id=current.document_id,
                expected_head_revision_id=current.head_revision_id,
                expected_state_revision=current.state_revision,
                artifact=revision.artifact,
                actor=Actor(ActorKind.USER, "head-race"),
            )
        return await original_create(service, **kwargs)

    monkeypatch.setattr(
        ArtifactSessionService,
        "create_prompt_annotation_with_anchor",
        create_after_head_advance,
    )
    rejected = await _dispatch(
        env,
        "artifacts.prompt_annotations.create",
        {
            "annotationId": "annotation-head-race",
            "sessionKey": SESSION_KEY,
            "documentId": document["id"],
            "revisionId": document["headRevisionId"],
            "selection": {
                "selectionId": "selection-head-race",
                "tagName": "section",
                "elementPath": element_path,
                "domSha256": dom_sha256,
                "elementProofSha256": element_proof_sha256,
            },
            "body": "This raced request must not persist.",
        },
    )

    assert rejected.error is not None
    assert rejected.error.code == "ANNOTATION_BUSY"
    assert "revision" not in rejected.error.message.lower()
    assert rejected.error.accepted is False
    assert await _annotation_row_counts(env) == (0, 0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("runtime_html", "element_path", "tag_name"),
    [
        (
            '<main id="shell"><section class="target"><h1>Title</h1></section>'
            '<aside><div id="runtime-only"></div></aside></main>',
            json.dumps(
                [
                    ["", "html", 1],
                    ["", "body", 1],
                    ["", "main", 1],
                    ["", "aside", 1],
                    ["", "div", 1],
                ],
                separators=(",", ":"),
            ),
            "div",
        ),
        (
            '<main id="shell"><section class="changed"><h1>Title</h1></section>'
            "<aside></aside></main>",
            json.dumps(
                [
                    ["", "html", 1],
                    ["", "body", 1],
                    ["", "main", 1],
                    ["", "section", 1],
                ],
                separators=(",", ":"),
            ),
            "section",
        ),
        (
            '<main id="changed"><section class="target"><h1>Title</h1></section>'
            "<aside></aside></main>",
            json.dumps(
                [
                    ["", "html", 1],
                    ["", "body", 1],
                    ["", "main", 1],
                    ["", "section", 1],
                ],
                separators=(",", ":"),
            ),
            "section",
        ),
    ],
    ids=("runtime-only-path", "selected-attribute", "ancestor-attribute"),
)
async def test_prompt_annotation_changed_element_fails_before_persistence(
    artifact_editing_env,
    monkeypatch: pytest.MonkeyPatch,
    runtime_html: str,
    element_path: str,
    tag_name: str,
) -> None:
    env = artifact_editing_env
    source_html = (
        '<main id="shell"><section class="target"><h1>Title</h1></section>'
        "<aside></aside></main>"
    )
    _ref, document = await _adopt_html(env, source_html.encode())
    runtime_dom_sha256, runtime_element_proof = _annotation_proofs(runtime_html, element_path)
    bridge = _EchoAnnotationBridge()
    monkeypatch.setattr(
        artifact_editing_rpc,
        "get_desktop_artifact_bridge_client",
        lambda: bridge,
    )

    rejected = await _dispatch(
        env,
        "artifacts.prompt_annotations.create",
        {
            "annotationId": f"annotation-rejected-{tag_name}",
            "sessionKey": SESSION_KEY,
            "documentId": document["id"],
            "revisionId": document["headRevisionId"],
            "selection": {
                "selectionId": f"selection-rejected-{tag_name}",
                "tagName": tag_name,
                "elementPath": element_path,
                "domSha256": runtime_dom_sha256,
                "elementProofSha256": runtime_element_proof,
            },
            "body": "This must not persist.",
        },
    )

    assert rejected.error is not None
    assert rejected.error.code == "DOCUMENT_CHANGED"
    assert "element" not in rejected.error.message.lower()
    assert rejected.error.accepted is False
    assert len(bridge.resolve_calls) == 1
    assert await _annotation_row_counts(env) == (0, 0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("collection_status", "warning_codes"),
    [
        ("partial", ()),
        ("complete", ("dynamic_reference",)),
    ],
)
async def test_incomplete_single_entrypoint_bundle_remains_preview_only(
    artifact_editing_env,
    monkeypatch: pytest.MonkeyPatch,
    collection_status: str,
    warning_codes: tuple[str, ...],
) -> None:
    env = artifact_editing_env
    monkeypatch.setattr(
        artifact_editing_rpc,
        "get_desktop_artifact_bridge_client",
        lambda: object(),
    )
    ref = env.store.publish_bundle(
        ArtifactBundle(
            entrypoint="index.html",
            files=(
                ArtifactBundleSourceFile(
                    path="index.html",
                    mime="text/html",
                    data=b"<main><h1>Incomplete bundle</h1></main>",
                ),
            ),
            collection_status=collection_status,
            warning_codes=warning_codes,
        ),
        session_id=env.session.session_id,
        session_key=SESSION_KEY,
        name="incomplete-bundle.html",
        mime="text/html",
        source="artifact_rpc_incomplete_bundle_test",
    )
    opened = await _dispatch(
        env,
        "artifacts.documents.open",
        {"sessionKey": SESSION_KEY, "artifactId": ref.id},
    )
    assert opened.error is None, opened.error
    document = opened.payload["document"]
    capabilities = document["capabilities"]
    assert capabilities["preview"] is True
    assert capabilities["selectionContext"] is False
    assert capabilities["sourceEdit"] is False
    assert capabilities["agentEdit"] is False
    assert capabilities["selection"] is False
    assert capabilities["promptAnnotations"] is False
    assert capabilities["unavailableReason"] == "html_bundle_edit_not_supported"

    source = await _dispatch(
        env,
        "artifacts.source.read",
        {"sessionKey": SESSION_KEY, "documentId": document["id"]},
    )
    assert source.error is not None
    assert source.error.code == "RESOURCE_UNSUPPORTED"
    assert source.error.details == {"reasonCode": "bundle_unsupported"}


@pytest.mark.asyncio
async def test_html_preview_bundle_is_preview_only_and_source_tools_are_not_bound(
    artifact_editing_env,
) -> None:
    env = artifact_editing_env
    ref = env.store.publish_bundle(
        ArtifactBundle(
            entrypoint="index.html",
            files=(
                ArtifactBundleSourceFile(
                    path="index.html",
                    mime="text/html",
                    data=b"<link rel='stylesheet' href='styles.css'><h1>Bundled</h1>",
                ),
                ArtifactBundleSourceFile(
                    path="styles.css",
                    mime="text/css",
                    data=b"h1 { color: teal; }",
                ),
            ),
        ),
        session_id=env.session.session_id,
        session_key=SESSION_KEY,
        name="bundle.html",
        mime="text/html",
        source="artifact_rpc_bundle_test",
    )
    opened = await _dispatch(
        env,
        "artifacts.documents.open",
        {"sessionKey": SESSION_KEY, "artifactId": ref.id},
    )
    assert opened.error is None, opened.error
    document = opened.payload["document"]
    capabilities = document["capabilities"]
    assert document["editorState"] == "preview_ready"
    assert capabilities["preview"] is True
    assert capabilities["selectionContext"] is False
    assert capabilities["promptAnnotations"] is False
    assert capabilities["sourceEdit"] is False
    assert capabilities["manualEdit"] is False
    assert capabilities["agentEdit"] is False
    assert capabilities["selection"] is False
    assert capabilities["engine"] is None
    assert capabilities["unavailableReason"] == "html_bundle_edit_not_supported"

    described = await _dispatch(
        env,
        "artifacts.edit.capabilities",
        {"sessionKey": SESSION_KEY, "documentId": document["id"]},
    )
    assert described.error is None, described.error
    assert described.payload["capabilities"] == capabilities

    source = await _dispatch(
        env,
        "artifacts.source.read",
        {"sessionKey": SESSION_KEY, "documentId": document["id"]},
    )
    assert source.error is not None
    assert source.error.code == "RESOURCE_UNSUPPORTED"
    assert source.error.details == {"reasonCode": "bundle_unsupported"}

    revisions_before = await _dispatch(
        env,
        "artifacts.revisions.list",
        {"sessionKey": SESSION_KEY, "documentId": document["id"]},
    )
    changes_before = await _dispatch(
        env,
        "artifacts.changes.list",
        {"sessionKey": SESSION_KEY, "documentId": document["id"]},
    )
    metadata_before = tuple(sorted(Path(env.store.media_root).rglob("meta.json")))
    patched = await _dispatch(
        env,
        "artifacts.source.patch",
        {
            "sessionKey": SESSION_KEY,
            "documentId": document["id"],
            "expectedHeadRevisionId": document["headRevisionId"],
            "expectedStateRevision": document["stateRevision"],
            "expectedSourceSha256": ref.sha256,
            "offsetEncoding": "unicode-code-point",
            "patches": [{"startOffset": 0, "endOffset": 0, "replacement": "<!-- edit -->"}],
        },
    )
    assert patched.error is not None
    assert patched.error.code == "RESOURCE_UNSUPPORTED"
    assert patched.error.details == {"reasonCode": "bundle_unsupported"}
    current = await _dispatch(
        env,
        "artifacts.documents.get",
        {"sessionKey": SESSION_KEY, "documentId": document["id"]},
    )
    revisions_after = await _dispatch(
        env,
        "artifacts.revisions.list",
        {"sessionKey": SESSION_KEY, "documentId": document["id"]},
    )
    changes_after = await _dispatch(
        env,
        "artifacts.changes.list",
        {"sessionKey": SESSION_KEY, "documentId": document["id"]},
    )
    assert current.error is None, current.error
    assert current.payload["document"]["headRevisionId"] == document["headRevisionId"]
    assert current.payload["document"]["stateRevision"] == document["stateRevision"]
    assert revisions_after.payload == revisions_before.payload
    assert changes_after.payload == changes_before.payload
    assert tuple(sorted(Path(env.store.media_root).rglob("meta.json"))) == metadata_before
    assert env.store.describe_preview_bundle(
        ref.id,
        session_id=env.session.session_id,
    ) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_case", ["nul", "oversized"])
async def test_source_patch_rejects_invalid_html_before_durable_mutation(
    artifact_editing_env,
    invalid_case: str,
) -> None:
    env = artifact_editing_env
    _ref, document = await _adopt_html(env)
    source = await _dispatch(
        env,
        "artifacts.source.read",
        {"sessionKey": SESSION_KEY, "documentId": document["id"]},
    )
    assert source.error is None, source.error
    revisions_before = await _dispatch(
        env,
        "artifacts.revisions.list",
        {"sessionKey": SESSION_KEY, "documentId": document["id"]},
    )
    changes_before = await _dispatch(
        env,
        "artifacts.changes.list",
        {"sessionKey": SESSION_KEY, "documentId": document["id"]},
    )
    attempt_cursor = await env.storage.conn.execute(
        "SELECT COUNT(*) FROM artifact_mutation_attempts WHERE document_id = ?",
        (document["id"],),
    )
    attempt_row = await attempt_cursor.fetchone()
    await attempt_cursor.close()
    assert attempt_row is not None
    attempt_count = int(attempt_row[0])
    metadata_before = tuple(sorted(Path(env.store.media_root).rglob("meta.json")))
    replacement = "\x00" if invalid_case == "nul" else "x" * (2 * 1024 * 1024)

    rejected = await _dispatch(
        env,
        "artifacts.source.patch",
        {
            "sessionKey": SESSION_KEY,
            "documentId": document["id"],
            "clientRequestId": f"invalid-source-{invalid_case}",
            "expectedHeadRevisionId": document["headRevisionId"],
            "expectedStateRevision": document["stateRevision"],
            "expectedSourceSha256": source.payload["source"]["sha256"],
            "offsetEncoding": "unicode-code-point",
            "patches": [
                {
                    "startOffset": 4,
                    "endOffset": 10,
                    "replacement": replacement,
                }
            ],
        },
    )

    assert rejected.error is not None
    assert rejected.error.code == "INVALID_REQUEST"
    assert rejected.error.details == {"reasonCode": "invalid_source_edit"}
    current = await _dispatch(
        env,
        "artifacts.documents.get",
        {"sessionKey": SESSION_KEY, "documentId": document["id"]},
    )
    revisions_after = await _dispatch(
        env,
        "artifacts.revisions.list",
        {"sessionKey": SESSION_KEY, "documentId": document["id"]},
    )
    changes_after = await _dispatch(
        env,
        "artifacts.changes.list",
        {"sessionKey": SESSION_KEY, "documentId": document["id"]},
    )
    attempt_cursor = await env.storage.conn.execute(
        "SELECT COUNT(*) FROM artifact_mutation_attempts WHERE document_id = ?",
        (document["id"],),
    )
    attempt_row = await attempt_cursor.fetchone()
    await attempt_cursor.close()
    assert attempt_row is not None
    assert current.payload["document"]["headRevisionId"] == document["headRevisionId"]
    assert current.payload["document"]["stateRevision"] == document["stateRevision"]
    assert revisions_after.payload == revisions_before.payload
    assert changes_after.payload == changes_before.payload
    assert int(attempt_row[0]) == attempt_count
    assert tuple(sorted(Path(env.store.media_root).rglob("meta.json"))) == metadata_before


@pytest.mark.asyncio
async def test_legacy_ref_is_lazily_adopted_and_source_patch_is_cas_safe(
    artifact_editing_env,
    unavailable_git_runtime: SimpleNamespace,
) -> None:
    env = artifact_editing_env
    ref, document = await _adopt_html(env)

    reopened = await _dispatch(
        env,
        "artifacts.documents.open",
        {"sessionKey": SESSION_KEY, "artifactId": ref.id},
    )
    assert reopened.error is None
    assert reopened.payload["adopted"] is False
    assert reopened.payload["document"]["id"] == document["id"]

    source = await _dispatch(
        env,
        "artifacts.source.read",
        {"sessionKey": SESSION_KEY, "documentId": document["id"]},
    )
    assert source.error is None, source.error
    source_payload = source.payload["source"]
    assert source_payload["text"] == "<h1>before</h1>"

    ambiguous_patches = await _dispatch(
        env,
        "artifacts.source.patch",
        {
            "sessionKey": SESSION_KEY,
            "documentId": document["id"],
            "expectedHeadRevisionId": document["headRevisionId"],
            "expectedStateRevision": document["stateRevision"],
            "expectedSourceSha256": source_payload["sha256"],
            "patches": [
                {"startOffset": 0, "endOffset": 0, "replacement": "x"},
                {"startOffset": 0, "endOffset": 1, "replacement": "<"},
            ],
        },
    )
    assert ambiguous_patches.error is not None
    assert ambiguous_patches.error.code == "INVALID_REQUEST"
    assert ambiguous_patches.error.details == {"reasonCode": "invalid_source_edit"}

    patch_params = {
        "sessionKey": SESSION_KEY,
        "documentId": document["id"],
        "expectedHeadRevisionId": document["headRevisionId"],
        "expectedStateRevision": document["stateRevision"],
        "expectedSourceSha256": source_payload["sha256"],
        "patches": [
            {"startOffset": 4, "endOffset": 10, "replacement": "after"},
        ],
    }
    patched = await _dispatch(env, "artifacts.source.patch", patch_params)
    assert patched.error is None, patched.error
    updated_document = patched.payload["document"]
    assert updated_document["headRevisionId"] != document["headRevisionId"]
    assert patched.payload["revision"]["parentRevisionId"] == document["headRevisionId"]
    assert patched.payload["revision"]["source"] == "manual"
    assert patched.payload["revision"]["changeSetId"] == patched.payload["changeSet"]["id"]
    assert patched.payload["changeSet"]["state"] == "applied"
    assert patched.payload["changeSet"]["operations"][0]["origin"] == "manual"
    assert "after" not in json.dumps(patched.payload["changeSet"]["operations"])
    assert patched.payload["receipt"] == {
        "requestId": patched.payload["receipt"]["requestId"],
        "documentId": document["id"],
        "baseRevisionId": document["headRevisionId"],
        "resultRevisionId": patched.payload["revision"]["id"],
        "changeSetId": patched.payload["changeSet"]["id"],
        "stateRevision": updated_document["stateRevision"],
        "status": "applied",
    }
    assert patched.payload["receipt"]["requestId"].startswith("legacy-")
    replayed = await _dispatch(env, "artifacts.source.patch", patch_params)
    assert replayed.error is None, replayed.error
    assert replayed.payload == patched.payload
    mismatched_replay = await _dispatch(
        env,
        "artifacts.source.patch",
        {
            **patch_params,
            "patches": [
                {"startOffset": 4, "endOffset": 10, "replacement": "different"},
            ],
        },
    )
    assert mismatched_replay.error is not None
    assert mismatched_replay.error.code == "DOCUMENT_CHANGED"

    # Internal revision blobs stay addressable through revision history but do
    # not become duplicate chat artifacts in the legacy list.
    listed = await _dispatch(env, "artifacts.list", {"sessionKey": SESSION_KEY})
    assert listed.error is None
    assert [item["id"] for item in listed.payload["artifacts"]] == [ref.id]
    attempt_cursor = await env.storage.conn.execute(
        "SELECT COUNT(*) FROM artifact_mutation_attempts WHERE document_id = ?",
        (document["id"],),
    )
    attempt_row = await attempt_cursor.fetchone()
    await attempt_cursor.close()
    assert attempt_row is not None
    attempt_count_before_admission_failures = int(attempt_row[0])

    stale_head = await _dispatch(
        env,
        "artifacts.source.patch",
        {
            "sessionKey": SESSION_KEY,
            "documentId": document["id"],
            "expectedHeadRevisionId": document["headRevisionId"],
            "expectedStateRevision": document["stateRevision"],
            "expectedSourceSha256": source_payload["sha256"],
            "patches": [{"startOffset": 0, "endOffset": 0, "replacement": "x"}],
        },
    )
    assert stale_head.error is not None
    assert stale_head.error.code == "DOCUMENT_CHANGED"

    current_source = await _dispatch(
        env,
        "artifacts.source.read",
        {"sessionKey": SESSION_KEY, "documentId": document["id"]},
    )
    assert current_source.error is None
    wrong_source_hash = await _dispatch(
        env,
        "artifacts.source.patch",
        {
            "sessionKey": SESSION_KEY,
            "documentId": document["id"],
            "expectedHeadRevisionId": updated_document["headRevisionId"],
            "expectedStateRevision": updated_document["stateRevision"],
            "expectedSourceSha256": "0" * 64,
            "patches": [{"startOffset": 4, "endOffset": 9, "replacement": "again"}],
        },
    )
    assert wrong_source_hash.error is not None
    assert wrong_source_hash.error.code == "DOCUMENT_CHANGED"
    assert "sha256" not in wrong_source_hash.error.message.lower()

    metadata_before = list((Path(env.store.media_root)).rglob("meta.json"))
    stale_state = await _dispatch(
        env,
        "artifacts.source.patch",
        {
            "sessionKey": SESSION_KEY,
            "documentId": document["id"],
            "expectedHeadRevisionId": updated_document["headRevisionId"],
            "expectedStateRevision": document["stateRevision"],
            "expectedSourceSha256": current_source.payload["source"]["sha256"],
            "patches": [{"startOffset": 4, "endOffset": 9, "replacement": "again"}],
        },
    )
    assert stale_state.error is not None
    assert stale_state.error.code == "DOCUMENT_CHANGED"
    assert list((Path(env.store.media_root)).rglob("meta.json")) == metadata_before
    attempt_cursor = await env.storage.conn.execute(
        "SELECT COUNT(*) FROM artifact_mutation_attempts WHERE document_id = ?",
        (document["id"],),
    )
    attempt_row = await attempt_cursor.fetchone()
    await attempt_cursor.close()
    assert attempt_row is not None
    assert int(attempt_row[0]) == attempt_count_before_admission_failures

    invalid_state = await _dispatch(
        env,
        "artifacts.source.patch",
        {
            "sessionKey": SESSION_KEY,
            "documentId": document["id"],
            "expectedHeadRevisionId": updated_document["headRevisionId"],
            "expectedSourceSha256": current_source.payload["source"]["sha256"],
            "patches": [{"startOffset": 4, "endOffset": 9, "replacement": "again"}],
        },
    )
    assert invalid_state.error is not None
    assert list((Path(env.store.media_root)).rglob("meta.json")) == metadata_before

    revisions = await _dispatch(
        env,
        "artifacts.revisions.list",
        {"sessionKey": SESSION_KEY, "documentId": document["id"]},
    )
    assert revisions.error is None
    assert [item["generation"] for item in revisions.payload["revisions"]] == [2, 1]
    changes = await _dispatch(
        env,
        "artifacts.changes.list",
        {"sessionKey": SESSION_KEY, "documentId": document["id"]},
    )
    assert changes.error is None
    assert [item["id"] for item in changes.payload["changeSets"]] == [
        patched.payload["changeSet"]["id"]
    ]
    service = await ArtifactSessionService.from_session_storage(env.storage)
    turn_id = f"manual-source-patch:{patched.payload['receipt']['requestId']}"
    tool_use_id = f"rpc-source-patch:{hashlib.sha256(turn_id.encode()).hexdigest()[:32]}"
    attempt = await service.reconcile_mutation_attempt(
        document_id=document["id"],
        turn_id=turn_id,
        tool_use_id=tool_use_id,
    )
    assert attempt.status is MutationAttemptStatus.APPLIED
    assert attempt.change_set_id == patched.payload["changeSet"]["id"]
    assert attempt.revision_id == patched.payload["revision"]["id"]
    assert unavailable_git_runtime.resolution_calls == []


@pytest.mark.asyncio
async def test_source_patch_replays_applied_receipt_after_later_head(
    artifact_editing_env,
) -> None:
    env = artifact_editing_env
    _ref, document = await _adopt_html(env)
    original_source = await _dispatch(
        env,
        "artifacts.source.read",
        {"sessionKey": SESSION_KEY, "documentId": document["id"]},
    )
    assert original_source.error is None
    first_params = {
        "sessionKey": SESSION_KEY,
        "documentId": document["id"],
        "expectedHeadRevisionId": document["headRevisionId"],
        "expectedStateRevision": document["stateRevision"],
        "expectedSourceSha256": original_source.payload["source"]["sha256"],
        "clientRequestId": "manual-replay-after-later-head",
        "patches": [{"startOffset": 4, "endOffset": 10, "replacement": "first"}],
    }
    first = await _dispatch(env, "artifacts.source.patch", first_params)
    assert first.error is None, first.error

    second = await _dispatch(
        env,
        "artifacts.source.patch",
        {
            "sessionKey": SESSION_KEY,
            "documentId": document["id"],
            "expectedHeadRevisionId": first.payload["revision"]["id"],
            "expectedStateRevision": first.payload["document"]["stateRevision"],
            "expectedSourceSha256": first.payload["source"]["sha256"],
            "clientRequestId": "manual-later-head",
            "patches": [{"startOffset": 4, "endOffset": 9, "replacement": "second"}],
        },
    )
    assert second.error is None, second.error
    service = await ArtifactSessionService.from_session_storage(env.storage)
    revision_count = len(await service.list_revisions(document["id"]))
    change_count = len(await service.list_change_sets(document["id"]))

    replay = await _dispatch(env, "artifacts.source.patch", first_params)
    assert replay.error is None, replay.error
    assert replay.payload["receipt"] == first.payload["receipt"]
    assert replay.payload["revision"] == first.payload["revision"]
    assert replay.payload["changeSet"] == first.payload["changeSet"]
    assert replay.payload["source"] == first.payload["source"]
    assert replay.payload["document"]["headRevisionId"] == second.payload["revision"]["id"]
    assert (
        replay.payload["document"]["stateRevision"]
        == second.payload["document"]["stateRevision"]
    )
    assert len(await service.list_revisions(document["id"])) == revision_count
    assert len(await service.list_change_sets(document["id"])) == change_count


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
    actions = ("source.patched", "revision.restored", "change.reverted")
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
async def test_edit_session_lifecycle_is_idempotent_and_heartbeat_is_atomic(
    artifact_editing_env,
) -> None:
    env = artifact_editing_env
    _ref, document = await _adopt_html(env)

    # Read/preview-shaped operations must not silently create editor state.
    viewed = await _dispatch(
        env,
        "artifacts.documents.get",
        {"sessionKey": SESSION_KEY, "documentId": document["id"]},
    )
    assert viewed.error is None
    assert await _edit_session_row_counts(env) == (0, 0)

    start_params = {
        "sessionKey": SESSION_KEY,
        "documentId": document["id"],
        "mode": "edit",
        "clientRequestId": "edit-session-lifecycle",
    }
    started = await _dispatch(env, "documents.editSessions.start", start_params)
    replayed_start = await _dispatch(env, "documents.editSessions.start", start_params)
    assert started.error is None, started.error
    assert replayed_start.error is None, replayed_start.error
    assert replayed_start.payload == started.payload
    edit_session = started.payload["editSession"]
    assert edit_session["mode"] == "edit"
    assert edit_session["status"] == "active"
    assert edit_session["stateRevision"] == 1
    assert edit_session["lastSavedRevisionId"] == document["headRevisionId"]
    assert "writerLeaseId" not in edit_session
    assert "fencingToken" not in edit_session
    assert await _edit_session_row_counts(env) == (1, 0)

    heartbeats = await asyncio.gather(
        *(
            _dispatch(
                env,
                "documents.editSessions.heartbeat",
                {
                    "sessionKey": SESSION_KEY,
                    "editSessionId": edit_session["id"],
                    "expectedStateRevision": edit_session["stateRevision"],
                },
            )
            for _ in range(2)
        )
    )
    assert sum(response.error is None for response in heartbeats) == 1
    rejected = next(response for response in heartbeats if response.error is not None)
    assert rejected.error is not None
    assert rejected.error.code == "EDIT_SESSION_RENEWAL_REQUIRED"
    heartbeat = next(response for response in heartbeats if response.error is None)
    touched = heartbeat.payload["editSession"]
    assert touched["stateRevision"] == 2

    service = await ArtifactSessionService.from_session_storage(env.storage)
    audit = await service.list_audit_events(document["id"])
    assert sum(event.event_type == "writer_lease.renewed" for event in audit) == 0
    assert sum(event.event_type == "edit_session.touched" for event in audit) == 1
    live_lease = await service.get_writer_lease(document["id"])
    assert live_lease is None

    close_params = {
        "sessionKey": SESSION_KEY,
        "editSessionId": touched["id"],
        "expectedStateRevision": touched["stateRevision"],
    }
    closed = await _dispatch(env, "documents.editSessions.close", close_params)
    replayed_close = await _dispatch(env, "documents.editSessions.close", close_params)
    assert closed.error is None, closed.error
    assert replayed_close.error is None, replayed_close.error
    assert replayed_close.payload == closed.payload
    assert closed.payload["editSession"]["status"] == "closed"
    assert closed.payload["editSession"]["stateRevision"] == 3
    assert await service.get_writer_lease(document["id"]) is None
    assert await _edit_session_row_counts(env) == (1, 0)


@pytest.mark.asyncio
async def test_active_edit_session_does_not_block_short_agent_writer_lease(
    artifact_editing_env,
) -> None:
    env = artifact_editing_env
    _ref, document = await _adopt_html(env)
    started = await _dispatch(
        env,
        "documents.editSessions.start",
        {
            "sessionKey": SESSION_KEY,
            "documentId": document["id"],
            "clientRequestId": "editor-without-long-writer-lease",
        },
    )
    assert started.error is None, started.error
    assert await _edit_session_row_counts(env) == (1, 0)

    service = await ArtifactSessionService.from_session_storage(env.storage)
    actor = Actor(ActorKind.AGENT, "annotation-agent")
    lease = await service.acquire_writer_lease(
        document_id=document["id"],
        holder_id="annotation-turn:test",
        ttl_ms=60_000,
        actor=actor,
    )
    assert lease.document_id == document["id"]
    await service.release_writer_lease(lease=lease, actor=actor)
    assert await service.get_writer_lease(document["id"]) is None
    edit_session = await service.get_edit_session(started.payload["editSession"]["id"])
    assert edit_session.status.value == "active"


@pytest.mark.asyncio
async def test_session_bound_source_patch_advances_session_and_fails_closed(
    artifact_editing_env,
) -> None:
    env = artifact_editing_env
    _ref, document = await _adopt_html(env)
    started = await _dispatch(
        env,
        "documents.editSessions.start",
        {
            "sessionKey": SESSION_KEY,
            "documentId": document["id"],
            "clientRequestId": "bound-source-patch-session",
        },
    )
    assert started.error is None, started.error
    edit_session = started.payload["editSession"]
    source = await _dispatch(
        env,
        "artifacts.source.read",
        {"sessionKey": SESSION_KEY, "documentId": document["id"]},
    )
    assert source.error is None
    patch_params = {
        "sessionKey": SESSION_KEY,
        "documentId": document["id"],
        "expectedHeadRevisionId": document["headRevisionId"],
        "expectedStateRevision": document["stateRevision"],
        "expectedSourceSha256": source.payload["source"]["sha256"],
        "clientRequestId": "bound-source-patch",
        "editSessionId": edit_session["id"],
        "expectedEditSessionStateRevision": edit_session["stateRevision"],
        "expectedLastSavedRevisionId": edit_session["lastSavedRevisionId"],
        "patches": [{"startOffset": 4, "endOffset": 10, "replacement": "after"}],
    }
    patched = await _dispatch(env, "artifacts.source.patch", patch_params)
    replayed = await _dispatch(env, "artifacts.source.patch", patch_params)
    assert patched.error is None, patched.error
    assert replayed.error is None, replayed.error
    assert replayed.payload["revision"]["id"] == patched.payload["revision"]["id"]
    assert replayed.payload["changeSet"]["id"] == patched.payload["changeSet"]["id"]
    saved_session = patched.payload["editSession"]
    assert saved_session["stateRevision"] == edit_session["stateRevision"] + 1
    assert saved_session["lastSavedRevisionId"] == patched.payload["revision"]["id"]
    assert replayed.payload["editSession"] == saved_session

    service = await ArtifactSessionService.from_session_storage(env.storage)
    assert len(await service.list_revisions(document["id"])) == 2
    assert len(await service.list_change_sets(document["id"])) == 1
    assert await service.get_writer_lease(document["id"]) is None

    latest_source = await _dispatch(
        env,
        "artifacts.source.read",
        {"sessionKey": SESSION_KEY, "documentId": document["id"]},
    )
    assert latest_source.error is None
    stale = await _dispatch(
        env,
        "artifacts.source.patch",
        {
            "sessionKey": SESSION_KEY,
            "documentId": document["id"],
            "expectedHeadRevisionId": patched.payload["revision"]["id"],
            "expectedStateRevision": patched.payload["document"]["stateRevision"],
            "expectedSourceSha256": latest_source.payload["source"]["sha256"],
            "clientRequestId": "bound-source-patch-stale-session",
            "editSessionId": edit_session["id"],
            "expectedEditSessionStateRevision": edit_session["stateRevision"],
            "expectedLastSavedRevisionId": edit_session["lastSavedRevisionId"],
            "patches": [{"startOffset": 4, "endOffset": 9, "replacement": "later"}],
        },
    )
    assert stale.error is not None
    assert stale.error.code == "EDIT_SESSION_RENEWAL_REQUIRED"
    assert len(await service.list_revisions(document["id"])) == 2
    assert len(await service.list_change_sets(document["id"])) == 1
    assert await service.list_mutation_attempts_by_turn_ids(
        session_key=SESSION_KEY,
        turn_ids=("manual-source-patch:bound-source-patch-stale-session",),
    ) == ()

    closed = await _dispatch(
        env,
        "documents.editSessions.close",
        {
            "sessionKey": SESSION_KEY,
            "editSessionId": saved_session["id"],
            "expectedStateRevision": saved_session["stateRevision"],
        },
    )
    assert closed.error is None, closed.error
    closed_save = await _dispatch(
        env,
        "artifacts.source.patch",
        {
            "sessionKey": SESSION_KEY,
            "documentId": document["id"],
            "expectedHeadRevisionId": patched.payload["revision"]["id"],
            "expectedStateRevision": patched.payload["document"]["stateRevision"],
            "expectedSourceSha256": latest_source.payload["source"]["sha256"],
            "clientRequestId": "bound-source-patch-closed-session",
            "editSessionId": saved_session["id"],
            "expectedEditSessionStateRevision": closed.payload["editSession"][
                "stateRevision"
            ],
            "expectedLastSavedRevisionId": saved_session["lastSavedRevisionId"],
            "patches": [{"startOffset": 4, "endOffset": 9, "replacement": "later"}],
        },
    )
    assert closed_save.error is not None
    assert closed_save.error.code == "EDIT_SESSION_RENEWAL_REQUIRED"
    assert len(await service.list_revisions(document["id"])) == 2
    assert len(await service.list_change_sets(document["id"])) == 1
    assert await service.list_mutation_attempts_by_turn_ids(
        session_key=SESSION_KEY,
        turn_ids=("manual-source-patch:bound-source-patch-closed-session",),
    ) == ()

    expired_started = await _dispatch(
        env,
        "documents.editSessions.start",
        {
            "sessionKey": SESSION_KEY,
            "documentId": document["id"],
            "clientRequestId": "bound-source-patch-expired-session-start",
        },
    )
    assert expired_started.error is None, expired_started.error
    expired_session = expired_started.payload["editSession"]
    await env.storage.conn.execute(
        "UPDATE artifact_edit_sessions SET expires_at = 0 WHERE edit_session_id = ?",
        (expired_session["id"],),
    )
    await env.storage.conn.commit()
    expired_save = await _dispatch(
        env,
        "artifacts.source.patch",
        {
            "sessionKey": SESSION_KEY,
            "documentId": document["id"],
            "expectedHeadRevisionId": patched.payload["revision"]["id"],
            "expectedStateRevision": patched.payload["document"]["stateRevision"],
            "expectedSourceSha256": latest_source.payload["source"]["sha256"],
            "clientRequestId": "bound-source-patch-expired-session",
            "editSessionId": expired_session["id"],
            "expectedEditSessionStateRevision": expired_session["stateRevision"],
            "expectedLastSavedRevisionId": expired_session["lastSavedRevisionId"],
            "patches": [{"startOffset": 4, "endOffset": 9, "replacement": "later"}],
        },
    )
    assert expired_save.error is not None
    assert expired_save.error.code == "EDIT_SESSION_RENEWAL_REQUIRED"
    assert len(await service.list_revisions(document["id"])) == 2
    assert len(await service.list_change_sets(document["id"])) == 1
    assert await service.list_mutation_attempts_by_turn_ids(
        session_key=SESSION_KEY,
        turn_ids=("manual-source-patch:bound-source-patch-expired-session",),
    ) == ()


@pytest.mark.asyncio
async def test_session_bound_source_patch_reconciles_response_loss_once(
    artifact_editing_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = artifact_editing_env
    _ref, document = await _adopt_html(env)
    started = await _dispatch(
        env,
        "documents.editSessions.start",
        {
            "sessionKey": SESSION_KEY,
            "documentId": document["id"],
            "clientRequestId": "bound-response-loss-session",
        },
    )
    assert started.error is None, started.error
    edit_session = started.payload["editSession"]
    source = await _dispatch(
        env,
        "artifacts.source.read",
        {"sessionKey": SESSION_KEY, "documentId": document["id"]},
    )
    assert source.error is None
    original_commit = ArtifactSessionService.commit_change_set_atomically

    async def commit_then_lose_response(self, **kwargs):
        await original_commit(self, **kwargs)
        raise RuntimeError("synthetic edit-session commit response loss")

    monkeypatch.setattr(
        ArtifactSessionService,
        "commit_change_set_atomically",
        commit_then_lose_response,
    )
    params = {
        "sessionKey": SESSION_KEY,
        "documentId": document["id"],
        "expectedHeadRevisionId": document["headRevisionId"],
        "expectedStateRevision": document["stateRevision"],
        "expectedSourceSha256": source.payload["source"]["sha256"],
        "clientRequestId": "bound-response-loss-save",
        "editSessionId": edit_session["id"],
        "expectedEditSessionStateRevision": edit_session["stateRevision"],
        "expectedLastSavedRevisionId": edit_session["lastSavedRevisionId"],
        "patches": [{"startOffset": 4, "endOffset": 10, "replacement": "after"}],
    }
    patched = await _dispatch(env, "artifacts.source.patch", params)
    assert patched.error is None, patched.error
    assert patched.payload["editSession"]["stateRevision"] == 2
    assert (
        patched.payload["editSession"]["lastSavedRevisionId"]
        == patched.payload["revision"]["id"]
    )
    service = await ArtifactSessionService.from_session_storage(env.storage)
    assert len(await service.list_revisions(document["id"])) == 2
    assert len(await service.list_change_sets(document["id"])) == 1


@pytest.mark.asyncio
async def test_edit_session_start_key_is_scoped_and_expired_heartbeat_does_not_renew(
    artifact_editing_env,
) -> None:
    env = artifact_editing_env
    _ref, first_document = await _adopt_html(env)
    _other_ref, second_document = await _adopt_html(env, b"<h1>other</h1>")
    request_id = "edit-session-reuse-guard"
    started = await _dispatch(
        env,
        "documents.editSessions.start",
        {
            "sessionKey": SESSION_KEY,
            "documentId": first_document["id"],
            "clientRequestId": request_id,
        },
    )
    assert started.error is None, started.error
    reused = await _dispatch(
        env,
        "documents.editSessions.start",
        {
            "sessionKey": SESSION_KEY,
            "documentId": second_document["id"],
            "clientRequestId": request_id,
        },
    )
    assert reused.error is not None
    assert reused.error.code == "WRITE_BUSY"

    edit_session = started.payload["editSession"]
    service = await ArtifactSessionService.from_session_storage(env.storage)
    lease_before = await service.get_writer_lease(first_document["id"])
    assert lease_before is None
    await env.storage.conn.execute(
        "UPDATE artifact_edit_sessions SET expires_at = 0 WHERE edit_session_id = ?",
        (edit_session["id"],),
    )
    expired = await _dispatch(
        env,
        "documents.editSessions.heartbeat",
        {
            "sessionKey": SESSION_KEY,
            "editSessionId": edit_session["id"],
            "expectedStateRevision": edit_session["stateRevision"],
        },
    )
    assert expired.error is not None
    assert expired.error.code == "EDIT_SESSION_RENEWAL_REQUIRED"
    lease_after = await service.get_writer_lease(first_document["id"])
    assert lease_after is None
    source = await _dispatch(
        env,
        "artifacts.source.read",
        {"sessionKey": SESSION_KEY, "documentId": first_document["id"]},
    )
    assert source.error is None
    expired_save = await _dispatch(
        env,
        "artifacts.source.patch",
        {
            "sessionKey": SESSION_KEY,
            "documentId": first_document["id"],
            "expectedHeadRevisionId": first_document["headRevisionId"],
            "expectedStateRevision": first_document["stateRevision"],
            "expectedSourceSha256": source.payload["source"]["sha256"],
            "clientRequestId": "expired-edit-session-save",
            "editSessionId": edit_session["id"],
            "expectedEditSessionStateRevision": edit_session["stateRevision"],
            "expectedLastSavedRevisionId": edit_session["lastSavedRevisionId"],
            "patches": [{"startOffset": 4, "endOffset": 10, "replacement": "after"}],
        },
    )
    assert expired_save.error is not None
    assert expired_save.error.code == "EDIT_SESSION_RENEWAL_REQUIRED"
    assert len(await service.list_revisions(first_document["id"])) == 1
    assert await service.list_change_sets(first_document["id"]) == ()


@pytest.mark.asyncio
async def test_concurrent_manual_source_patches_commit_one_revision_and_change_set(
    artifact_editing_env,
) -> None:
    env = artifact_editing_env
    _ref, document = await _adopt_html(env)
    source_sha256 = hashlib.sha256(b"<h1>before</h1>").hexdigest()

    responses = await asyncio.gather(
        *(
            _dispatch(
                env,
                "artifacts.source.patch",
                {
                    "sessionKey": SESSION_KEY,
                    "documentId": document["id"],
                    "expectedHeadRevisionId": document["headRevisionId"],
                    "expectedStateRevision": document["stateRevision"],
                    "expectedSourceSha256": source_sha256,
                    "clientRequestId": f"manual-concurrent-{index}",
                    "patches": [
                        {
                            "startOffset": 4,
                            "endOffset": 10,
                            "replacement": f"after-{index}",
                        }
                    ],
                },
            )
            for index in range(2)
        )
    )

    assert sum(response.error is None for response in responses) == 1
    rejected = next(response for response in responses if response.error is not None)
    assert rejected.error is not None
    assert rejected.error.code == "DOCUMENT_CHANGED"
    service = await ArtifactSessionService.from_session_storage(env.storage)
    assert len(await service.list_revisions(document["id"])) == 2
    assert len(await service.list_change_sets(document["id"])) == 1
    assert await service.get_writer_lease(document["id"]) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("commit_before_error", [False, True])
async def test_source_patch_reconciles_candidate_cleanup_across_commit_errors(
    artifact_editing_env,
    monkeypatch: pytest.MonkeyPatch,
    commit_before_error: bool,
) -> None:
    env = artifact_editing_env
    _ref, document = await _adopt_html(env)
    source = await _dispatch(
        env,
        "artifacts.source.read",
        {"sessionKey": SESSION_KEY, "documentId": document["id"]},
    )
    assert source.error is None
    metadata_before = list((Path(env.store.media_root)).rglob("meta.json"))
    original_commit = ArtifactSessionService.commit_change_set_atomically

    async def _commit_then_error(self, **kwargs):
        if commit_before_error:
            await original_commit(self, **kwargs)
        raise RuntimeError("synthetic commit response failure")

    monkeypatch.setattr(
        ArtifactSessionService,
        "commit_change_set_atomically",
        _commit_then_error,
    )
    client_request_id = f"manual-response-failure-{commit_before_error}"
    patched = await _dispatch(
        env,
        "artifacts.source.patch",
        {
            "sessionKey": SESSION_KEY,
            "documentId": document["id"],
            "expectedHeadRevisionId": document["headRevisionId"],
            "expectedStateRevision": document["stateRevision"],
            "expectedSourceSha256": source.payload["source"]["sha256"],
            "clientRequestId": client_request_id,
            "patches": [{"startOffset": 4, "endOffset": 10, "replacement": "after"}],
        },
    )

    if commit_before_error:
        assert patched.error is None, patched.error
        assert patched.payload["document"]["generation"] == 2
        assert (
            len(list((Path(env.store.media_root)).rglob("meta.json"))) == len(metadata_before) + 1
        )
        service = await ArtifactSessionService.from_session_storage(env.storage)
        assert len(await service.list_revisions(document["id"])) == 2
        assert len(await service.list_change_sets(document["id"])) == 1
        expected_status = MutationAttemptStatus.APPLIED
    else:
        assert patched.error is not None
        assert patched.error.code == "MUTATION_OUTCOME_PENDING"
        assert patched.error.accepted is None
        assert patched.error.details is not None
        assert patched.error.details["correlationId"]
        assert "synthetic" not in patched.error.message
        assert list((Path(env.store.media_root)).rglob("meta.json")) == metadata_before
        current = await ArtifactSessionService.from_session_storage(env.storage)
        assert (await current.get_document(document["id"])).head_revision_id == document[
            "headRevisionId"
        ]
        assert len(await current.list_revisions(document["id"])) == 1
        assert await current.list_change_sets(document["id"]) == ()
        service = current
        expected_status = MutationAttemptStatus.FAILED
    turn_id = f"manual-source-patch:{client_request_id}"
    tool_use_id = f"rpc-source-patch:{hashlib.sha256(turn_id.encode()).hexdigest()[:32]}"
    attempt = await service.reconcile_mutation_attempt(
        document_id=document["id"],
        turn_id=turn_id,
        tool_use_id=tool_use_id,
    )
    assert attempt.status is expected_status


@pytest.mark.asyncio
async def test_manual_source_patch_cleanup_ambiguity_is_restart_recoverable(
    artifact_editing_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = artifact_editing_env
    _ref, document = await _adopt_html(env)
    source = await _dispatch(
        env,
        "artifacts.source.read",
        {"sessionKey": SESSION_KEY, "documentId": document["id"]},
    )
    assert source.error is None

    async def fail_commit(self, **_kwargs):
        raise RuntimeError("synthetic pre-commit failure")

    original_delete = ArtifactStore.delete_reserved_bucket

    def fail_cleanup(self, **_kwargs):
        raise ArtifactError("synthetic cleanup failure")

    monkeypatch.setattr(ArtifactSessionService, "commit_change_set_atomically", fail_commit)
    monkeypatch.setattr(ArtifactStore, "delete_reserved_bucket", fail_cleanup)
    client_request_id = "manual-cleanup-recovery"
    patched = await _dispatch(
        env,
        "artifacts.source.patch",
        {
            "sessionKey": SESSION_KEY,
            "documentId": document["id"],
            "expectedHeadRevisionId": document["headRevisionId"],
            "expectedStateRevision": document["stateRevision"],
            "expectedSourceSha256": source.payload["source"]["sha256"],
            "clientRequestId": client_request_id,
            "patches": [{"startOffset": 4, "endOffset": 10, "replacement": "after"}],
        },
    )
    assert patched.error is not None
    assert patched.error.code == "MUTATION_OUTCOME_PENDING"
    assert patched.error.details == {"reasonCode": "cleanup_pending"}

    service = await ArtifactSessionService.from_session_storage(env.storage)
    turn_id = f"manual-source-patch:{client_request_id}"
    tool_use_id = f"rpc-source-patch:{hashlib.sha256(turn_id.encode()).hexdigest()[:32]}"
    ambiguous = await service.reconcile_mutation_attempt(
        document_id=document["id"],
        turn_id=turn_id,
        tool_use_id=tool_use_id,
    )
    assert ambiguous.status is MutationAttemptStatus.AMBIGUOUS
    assert ambiguous.candidate_artifact_id is not None

    monkeypatch.setattr(ArtifactStore, "delete_reserved_bucket", original_delete)
    summary = await reconcile_pending_artifact_mutations(service, env.store)
    assert summary.failed == 1
    assert summary.deleted_candidates == 1
    recovered = await service.reconcile_mutation_attempt(
        document_id=document["id"],
        turn_id=turn_id,
        tool_use_id=tool_use_id,
    )
    assert recovered.status is MutationAttemptStatus.FAILED
    with pytest.raises(ArtifactBlobNotFoundError):
        env.store.resolve_for_download(
            ambiguous.candidate_artifact_id,
            session_id=env.session.session_id,
        )
    assert len(await service.list_revisions(document["id"])) == 1
    assert await service.list_change_sets(document["id"]) == ()


@pytest.mark.asyncio
async def test_source_patch_cancellation_during_publish_removes_the_candidate(
    artifact_editing_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = artifact_editing_env
    _ref, document = await _adopt_html(env)
    source = await _dispatch(
        env,
        "artifacts.source.read",
        {"sessionKey": SESSION_KEY, "documentId": document["id"]},
    )
    assert source.error is None
    before_meta = list((Path(env.store.media_root)).rglob("meta.json"))
    publish_started = threading.Event()
    allow_publish = threading.Event()
    original_publish = ArtifactStore.publish_bytes

    def _delayed_publish(self, payload: bytes, **kwargs: object):
        if kwargs.get("source") == "artifact_source_patch":
            publish_started.set()
            assert allow_publish.wait(timeout=5)
        return original_publish(self, payload, **kwargs)

    monkeypatch.setattr(ArtifactStore, "publish_bytes", _delayed_publish)
    patch_task = asyncio.create_task(
        _dispatch(
            env,
            "artifacts.source.patch",
            {
                "sessionKey": SESSION_KEY,
                "documentId": document["id"],
                "expectedHeadRevisionId": document["headRevisionId"],
                "expectedStateRevision": document["stateRevision"],
                "expectedSourceSha256": source.payload["source"]["sha256"],
                "patches": [{"startOffset": 4, "endOffset": 10, "replacement": "after"}],
            },
        )
    )
    assert await asyncio.to_thread(publish_started.wait, 5)
    patch_task.cancel()
    allow_publish.set()

    with pytest.raises(asyncio.CancelledError):
        await patch_task
    assert list((Path(env.store.media_root)).rglob("meta.json")) == before_meta
    current = await ArtifactSessionService.from_session_storage(env.storage)
    assert (await current.get_document(document["id"])).head_revision_id == document[
        "headRevisionId"
    ]


@pytest.mark.asyncio
async def test_restore_and_applied_change_revert_preserve_history(
    artifact_editing_env,
) -> None:
    env = artifact_editing_env
    _ref, initial_document = await _adopt_html(env)
    initial_revision_id = initial_document["headRevisionId"]
    source_sha = hashlib.sha256(b"<h1>before</h1>").hexdigest()
    patched = await _dispatch(
        env,
        "artifacts.source.patch",
        {
            "sessionKey": SESSION_KEY,
            "documentId": initial_document["id"],
            "expectedHeadRevisionId": initial_revision_id,
            "expectedStateRevision": initial_document["stateRevision"],
            "expectedSourceSha256": source_sha,
            "patches": [{"startOffset": 4, "endOffset": 10, "replacement": "after"}],
        },
    )
    assert patched.error is None, patched.error
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
    assert restored.payload["revision"]["source"] == "restore"
    assert restored.payload["revision"]["copiedFromRevisionId"] == initial_revision_id
    assert restored.payload["revision"]["changeSetId"] == restored.payload["changeSet"]["id"]
    assert restored.payload["receipt"]["changeSetId"] == restored.payload["changeSet"]["id"]
    restore_replay = await _dispatch(env, "artifacts.revisions.restore", restore_params)
    assert restore_replay.error is None, restore_replay.error
    assert restore_replay.payload == restored.payload
    service = await ArtifactSessionService.from_session_storage(env.storage)
    restore_revision_count = len(await service.list_revisions(initial_document["id"]))
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
    assert (
        restore_replay_after_later_head.payload["revision"]
        == restored.payload["revision"]
    )
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
    assert reverted.payload["revision"]["changeSetId"] == reverted.payload[
        "mutationChangeSet"
    ]["id"]
    assert reverted.payload["receipt"]["changeSetId"] == reverted.payload[
        "mutationChangeSet"
    ]["id"]
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
    later_patch = await _dispatch(
        env,
        "artifacts.source.patch",
        {
            "sessionKey": SESSION_KEY,
            "documentId": base_document.document_id,
            "expectedHeadRevisionId": reverted.payload["revision"]["id"],
            "expectedStateRevision": reverted.payload["document"]["stateRevision"],
            "expectedSourceSha256": reverted_source.payload["source"]["sha256"],
            "clientRequestId": "later-head-after-revert",
            "patches": [{"startOffset": 4, "endOffset": 10, "replacement": "later"}],
        },
    )
    assert later_patch.error is None, later_patch.error
    revision_count = len(await service.list_revisions(base_document.document_id))
    change_count = len(await service.list_change_sets(base_document.document_id))
    revert_replay_after_later_head = await _dispatch(
        env,
        "artifacts.changes.revert",
        revert_params,
    )
    assert revert_replay_after_later_head.error is None
    assert revert_replay_after_later_head.payload["receipt"] == reverted.payload["receipt"]
    assert (
        revert_replay_after_later_head.payload["revision"]
        == reverted.payload["revision"]
    )
    assert (
        revert_replay_after_later_head.payload["document"]["headRevisionId"]
        == later_patch.payload["revision"]["id"]
    )
    assert len(await service.list_revisions(base_document.document_id)) == revision_count
    assert len(await service.list_change_sets(base_document.document_id)) == change_count

    revisions = await service.list_revisions(base_document.document_id)
    assert [item.generation for item in revisions] == [6, 5, 4, 3, 2, 1]
    assert revisions[0].parent_revision_id == revisions[1].revision_id
    assert len(await service.list_change_sets(base_document.document_id)) == 5


@pytest.mark.asyncio
async def test_prompt_annotation_selection_is_revalidated_and_persisted_as_anchor(
    artifact_editing_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = artifact_editing_env
    html = '<main><section id="hero"><h1>Hello</h1></section></main>'
    _ref, document = await _adopt_html(env, html.encode())
    source = await _dispatch(
        env,
        "artifacts.source.read",
        {"sessionKey": SESSION_KEY, "documentId": document["id"]},
    )
    snapshot = source.payload["source"]
    start = html.index("<section")

    element_path = json.dumps(
        [["", "html", 1], ["", "body", 1], ["", "main", 1], ["", "section", 1]],
        separators=(",", ":"),
    )
    dom_sha256, element_proof_sha256 = _annotation_proofs(html, element_path)
    assert dom_sha256 == "e26409f825b86847aac8205ba6fdeee2d8f1cc6e89f1a45511cf26a003d65dc3"

    class FakeBridge:
        def __init__(self) -> None:
            self.focus_calls: list[dict[str, object]] = []

        async def resolve_annotation_selection(self, **kwargs):
            return SimpleNamespace(
                active_preview_artifact_id=kwargs["active_preview_artifact_id"],
                selection_id=kwargs["selection_id"],
                tag_name=kwargs["tag_name"],
                element_path=kwargs["element_path"],
                dom_sha256=kwargs["dom_sha256"],
                element_proof_sha256=kwargs["element_proof_sha256"],
                scope_id=SESSION_KEY,
            )

        async def focus_annotation(self, **kwargs):
            self.focus_calls.append(kwargs)
            return True

    fake_bridge = FakeBridge()
    monkeypatch.setattr(
        artifact_editing_rpc,
        "get_desktop_artifact_bridge_client",
        lambda: fake_bridge,
    )
    created_annotation = await _dispatch(
        env,
        "artifacts.prompt_annotations.create",
        {
            "annotationId": "annotation-hero",
            "sessionKey": SESSION_KEY,
            "documentId": document["id"],
            "revisionId": document["headRevisionId"],
            "selection": {
                "selectionId": "selection-hero",
                "tagName": "section",
                "elementPath": element_path,
                "domSha256": dom_sha256,
                "elementProofSha256": element_proof_sha256,
            },
            "body": "Keep this heading concise.",
        },
    )
    assert created_annotation.error is None, created_annotation.error
    annotation = created_annotation.payload["annotation"]
    assert annotation["status"] == "draft"
    assert annotation["freshness"] == "current"
    assert annotation["anchor"]["quote"] == '<section id="hero">'
    assert annotation["anchor"]["locator"] == {
        "start_offset": start,
        "start_tag_end_offset": html.index(">", start) + 1,
        "tag_name": "section",
        "source_sha256": snapshot["sha256"],
        "offset_encoding": "unicode-code-point",
    }
    focused = await _dispatch(
        env,
        "artifacts.prompt_annotations.focus",
        {"sessionKey": SESSION_KEY, "annotationId": annotation["id"]},
    )
    assert focused.error is None, focused.error
    assert focused.payload == {
        "focused": True,
        "annotationId": annotation["id"],
        "documentId": document["id"],
    }
    assert fake_bridge.focus_calls == [
        {
            "annotation_id": annotation["id"],
            "scope_id": SESSION_KEY,
            "active_preview_artifact_id": _ref.id,
            "tag_name": "section",
            "element_path": element_path,
            "element_proof_sha256": element_proof_sha256,
            "deadline_ms": 2_000,
        }
    ]
    listed = await _dispatch(
        env,
        "artifacts.prompt_annotations.list",
        {"sessionKey": SESSION_KEY, "documentId": document["id"]},
    )
    assert listed.error is None, listed.error
    assert listed.payload["annotations"] == [annotation]
    updated = await _dispatch(
        env,
        "artifacts.prompt_annotations.update",
        {
            "sessionKey": SESSION_KEY,
            "annotationId": annotation["id"],
            "expectedStateRevision": annotation["stateRevision"],
            "body": "Use a shorter hero heading.",
        },
    )
    assert updated.error is None, updated.error
    discarded = await _dispatch(
        env,
        "artifacts.prompt_annotations.discard",
        {
            "sessionKey": SESSION_KEY,
            "annotationId": annotation["id"],
            "expectedStateRevision": updated.payload["annotation"]["stateRevision"],
        },
    )
    assert discarded.error is None, discarded.error
    assert discarded.payload["annotation"]["status"] == "discarded"
    discarded_focus = await _dispatch(
        env,
        "artifacts.prompt_annotations.focus",
        {"sessionKey": SESSION_KEY, "annotationId": annotation["id"]},
    )
    assert discarded_focus.error is not None
    assert discarded_focus.error.code == "ANNOTATION_UNAVAILABLE"
    assert discarded_focus.error.details == {"reasonCode": "not_draft"}
    assert len(fake_bridge.focus_calls) == 1


@pytest.mark.asyncio
async def test_prompt_annotation_focus_handles_bridge_loss_and_remaps_current_head(
    artifact_editing_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = artifact_editing_env
    html = "<main><p>one</p></main>"
    _ref, document = await _adopt_html(env, html.encode())
    element_path = json.dumps(
        [["", "html", 1], ["", "body", 1], ["", "main", 1], ["", "p", 1]],
        separators=(",", ":"),
    )
    dom_sha256, element_proof_sha256 = _annotation_proofs(html, element_path)

    class FakeBridge:
        def __init__(self) -> None:
            self.focus_calls = 0
            self.last_focus_kwargs: dict[str, object] | None = None

        async def resolve_annotation_selection(self, **kwargs):
            return SimpleNamespace(
                active_preview_artifact_id=kwargs["active_preview_artifact_id"],
                selection_id=kwargs["selection_id"],
                tag_name=kwargs["tag_name"],
                element_path=kwargs["element_path"],
                dom_sha256=kwargs["dom_sha256"],
                element_proof_sha256=kwargs["element_proof_sha256"],
                scope_id=SESSION_KEY,
            )

        async def focus_annotation(self, **kwargs):
            self.focus_calls += 1
            self.last_focus_kwargs = kwargs
            return True

    fake_bridge = FakeBridge()
    monkeypatch.setattr(
        artifact_editing_rpc,
        "get_desktop_artifact_bridge_client",
        lambda: fake_bridge,
    )
    created = await _dispatch(
        env,
        "artifacts.prompt_annotations.create",
        {
            "annotationId": "annotation-focus-fail-closed",
            "sessionKey": SESSION_KEY,
            "documentId": document["id"],
            "revisionId": document["headRevisionId"],
            "selection": {
                "selectionId": "selection-focus-fail-closed",
                "tagName": "p",
                "elementPath": element_path,
                "domSha256": dom_sha256,
                "elementProofSha256": element_proof_sha256,
            },
            "body": "Tighten this sentence.",
        },
    )
    assert created.error is None, created.error
    annotation_id = created.payload["annotation"]["id"]

    # A Desktop-managed Gateway may still be alive while its active surface
    # disappears. That is an availability failure, not permission to trust a
    # renderer locator or select another surface.
    monkeypatch.setattr(
        artifact_editing_rpc,
        "get_desktop_artifact_bridge_client",
        lambda: object(),
    )
    unavailable = await _dispatch(
        env,
        "artifacts.prompt_annotations.focus",
        {"sessionKey": SESSION_KEY, "annotationId": annotation_id},
    )
    assert unavailable.error is not None
    assert unavailable.error.code == "ANNOTATION_UNAVAILABLE"
    assert unavailable.error.details == {"reasonCode": "preview_unavailable"}
    assert fake_bridge.focus_calls == 0

    monkeypatch.setattr(
        artifact_editing_rpc,
        "get_desktop_artifact_bridge_client",
        lambda: fake_bridge,
    )
    current = await _dispatch(
        env,
        "artifacts.documents.get",
        {"sessionKey": SESSION_KEY, "documentId": document["id"]},
    )
    source = await _dispatch(
        env,
        "artifacts.source.read",
        {"sessionKey": SESSION_KEY, "documentId": document["id"]},
    )
    updated_html = '<main><section><p style="color:blue">one</p></section></main>'
    updated_element_path = json.dumps(
        [
            ["", "html", 1],
            ["", "body", 1],
            ["", "main", 1],
            ["", "section", 1],
            ["", "p", 1],
        ],
        separators=(",", ":"),
    )
    _updated_dom_sha256, updated_element_proof_sha256 = _annotation_proofs(
        updated_html,
        updated_element_path,
    )
    patched = await _dispatch(
        env,
        "artifacts.source.patch",
        {
            "sessionKey": SESSION_KEY,
            "documentId": document["id"],
            "expectedHeadRevisionId": current.payload["document"]["headRevisionId"],
            "expectedStateRevision": current.payload["document"]["stateRevision"],
            "expectedSourceSha256": source.payload["source"]["sha256"],
            "patches": [
                {
                    "startOffset": 0,
                    "endOffset": len(html),
                    "replacement": updated_html,
                }
            ],
        },
    )
    assert patched.error is None, patched.error
    remapped = await _dispatch(
        env,
        "artifacts.prompt_annotations.focus",
        {"sessionKey": SESSION_KEY, "annotationId": annotation_id},
    )
    assert remapped.error is None, remapped.error
    assert fake_bridge.focus_calls == 1
    assert fake_bridge.last_focus_kwargs is not None
    assert (
        fake_bridge.last_focus_kwargs["active_preview_artifact_id"]
        == patched.payload["revision"]["artifactId"]
    )
    assert fake_bridge.last_focus_kwargs["element_path"] == updated_element_path
    assert (
        fake_bridge.last_focus_kwargs["element_proof_sha256"]
        == updated_element_proof_sha256
    )


@pytest.mark.asyncio
async def test_prompt_annotation_focus_rejects_another_active_artifact_in_same_session(
    artifact_editing_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = artifact_editing_env
    html = "<main><p>one</p></main>"
    target_ref, document = await _adopt_html(env, html.encode())
    wrong_ref, _wrong_document = await _adopt_html(env, b"<main><p>two</p></main>")
    element_path = json.dumps(
        [["", "html", 1], ["", "body", 1], ["", "main", 1], ["", "p", 1]],
        separators=(",", ":"),
    )
    dom_sha256, element_proof_sha256 = _annotation_proofs(html, element_path)

    class SwitchableBridge:
        def __init__(self) -> None:
            self.active_artifact_id = target_ref.id
            self.focus_calls: list[dict[str, object]] = []

        async def resolve_annotation_selection(self, **kwargs):
            return SimpleNamespace(
                active_preview_artifact_id=self.active_artifact_id,
                selection_id=kwargs["selection_id"],
                tag_name=kwargs["tag_name"],
                element_path=kwargs["element_path"],
                dom_sha256=kwargs["dom_sha256"],
                element_proof_sha256=kwargs["element_proof_sha256"],
                scope_id=SESSION_KEY,
            )

        async def focus_annotation(self, **kwargs):
            self.focus_calls.append(kwargs)
            if kwargs["active_preview_artifact_id"] != self.active_artifact_id:
                raise desktop_artifact_bridge.DesktopArtifactBridgeError(
                    "operation-failed",
                    "The active Desktop preview artifact changed.",
                )
            return True

    bridge = SwitchableBridge()
    monkeypatch.setattr(
        artifact_editing_rpc,
        "get_desktop_artifact_bridge_client",
        lambda: bridge,
    )
    created = await _dispatch(
        env,
        "artifacts.prompt_annotations.create",
        {
            "annotationId": "annotation-focus-wrong-artifact",
            "sessionKey": SESSION_KEY,
            "documentId": document["id"],
            "revisionId": document["headRevisionId"],
            "selection": {
                "selectionId": "selection-focus-wrong-artifact",
                "tagName": "p",
                "elementPath": element_path,
                "domSha256": dom_sha256,
                "elementProofSha256": element_proof_sha256,
            },
            "body": "Keep this annotation on the first artifact.",
        },
    )
    assert created.error is None, created.error

    bridge.active_artifact_id = wrong_ref.id
    rejected = await _dispatch(
        env,
        "artifacts.prompt_annotations.focus",
        {
            "sessionKey": SESSION_KEY,
            "annotationId": created.payload["annotation"]["id"],
        },
    )

    assert rejected.error is not None
    assert rejected.error.code == "ANNOTATION_UNAVAILABLE"
    assert rejected.error.details is not None
    assert rejected.error.details["reasonCode"] == "preview_unavailable"
    assert rejected.error.details["correlationId"]
    assert rejected.error.accepted is False
    assert bridge.focus_calls == [
        {
            "annotation_id": created.payload["annotation"]["id"],
            "scope_id": SESSION_KEY,
            "active_preview_artifact_id": target_ref.id,
            "tag_name": "p",
            "element_path": element_path,
            "element_proof_sha256": element_proof_sha256,
            "deadline_ms": 2_000,
        }
    ]
    assert wrong_ref.id != target_ref.id
    assert await _annotation_row_counts(env) == (1, 1, 1)


@pytest.mark.parametrize(
    ("html", "path", "tag_name", "expected_opening", "expected_dom_sha256"),
    [
        (
            "<ul><li>one<li class='chosen'>two</ul>",
            [["", "html", 1], ["", "body", 1], ["", "ul", 1], ["", "li", 2]],
            "li",
            "<li class='chosen'>",
            "7487fbb6148391e3b99765a978566eea5b177963182b6306b0bce364eb179594",
        ),
        (
            "<main><img alt='hero'><p>after</main>",
            [["", "html", 1], ["", "body", 1], ["", "main", 1], ["", "img", 1]],
            "img",
            "<img alt='hero'>",
            "7b3ca0b12f82ac686281dd4019baeb3039e16d2f5279eefd4a3d9621f99a40bc",
        ),
        (
            '<main><svg width="10"><path d="M0 0"></path></svg></main>',
            [
                ["", "html", 1],
                ["", "body", 1],
                ["", "main", 1],
                ["http://www.w3.org/2000/svg", "svg", 1],
                ["http://www.w3.org/2000/svg", "path", 1],
            ],
            "path",
            '<path d="M0 0">',
            "9cf21d875f2ea12071d92501c74d56056c1743c5926be9ff23e800d3f9e28fbc",
        ),
    ],
)
def test_opening_tag_anchor_supports_optional_close_and_void_elements(
    html: str,
    path: list[list[object]],
    tag_name: str,
    expected_opening: str,
    expected_dom_sha256: str,
) -> None:
    element_path = json.dumps(path, separators=(",", ":"))
    dom_sha256, element_proof_sha256 = _annotation_proofs(html, element_path)
    assert dom_sha256 == expected_dom_sha256
    locator, quote, _context = artifact_editing_rpc._canonical_opening_anchor(
        html,
        element_path=element_path,
        expected_element_proof_sha256=element_proof_sha256,
        expected_tag_name=tag_name,
    )
    assert quote == expected_opening
    assert html[locator["start_offset"] : locator["start_tag_end_offset"]] == expected_opening


def test_element_proof_sha256_cross_runtime_golden() -> None:
    html = (
        '<main data-label="😀"><svg viewBox="0 0 10 10">'
        '<path aria-label="星😀" d="M0 0"></path></svg></main>'
    )
    element_path = json.dumps(
        [
            ["", "html", 1],
            ["", "body", 1],
            ["", "main", 1],
            ["http://www.w3.org/2000/svg", "svg", 1],
            ["http://www.w3.org/2000/svg", "path", 1],
        ],
        separators=(",", ":"),
    )

    _dom_sha256, element_proof_sha256 = _annotation_proofs(html, element_path)

    # Shared with the Electron implementation: compact JSON per ancestor,
    # literal LF separators, UTF-8 SHA-256. Covers SVG adjustment, attribute
    # ordering, and non-BMP values.
    assert element_proof_sha256 == (
        "26992606963b33b7d475a826bf0a48ae802e9ac7bfe43ed5cab3aa97b7f0c5c8"
    )


@pytest.mark.asyncio
async def test_html_source_offsets_are_unicode_code_points_across_non_bmp_text(
    artifact_editing_env,
) -> None:
    env = artifact_editing_env
    html = "<main>😀<section><h1>Hello</h1></section></main>"
    _ref, document = await _adopt_html(env, html.encode())
    source = await _dispatch(
        env,
        "artifacts.source.read",
        {"sessionKey": SESSION_KEY, "documentId": document["id"]},
    )
    snapshot = source.payload["source"]
    assert snapshot["offsetEncoding"] == "unicode-code-point"

    start = html.index("Hello")
    patched = await _dispatch(
        env,
        "artifacts.source.patch",
        {
            "sessionKey": SESSION_KEY,
            "documentId": document["id"],
            "expectedHeadRevisionId": document["headRevisionId"],
            "expectedStateRevision": document["stateRevision"],
            "expectedSourceSha256": snapshot["sha256"],
            "offsetEncoding": "unicode-code-point",
            "patches": [
                {"startOffset": start, "endOffset": start + 5, "replacement": "World"},
            ],
        },
    )
    assert patched.error is None, patched.error
    assert patched.payload["source"]["offsetEncoding"] == "unicode-code-point"
    current = await _dispatch(
        env,
        "artifacts.source.read",
        {"sessionKey": SESSION_KEY, "documentId": document["id"]},
    )
    assert current.payload["source"]["text"] == html.replace("Hello", "World")

    unsupported = await _dispatch(
        env,
        "artifacts.source.patch",
        {
            "sessionKey": SESSION_KEY,
            "documentId": document["id"],
            "expectedHeadRevisionId": patched.payload["document"]["headRevisionId"],
            "expectedStateRevision": patched.payload["document"]["stateRevision"],
            "expectedSourceSha256": current.payload["source"]["sha256"],
            "offsetEncoding": "utf-16-code-unit",
            "patches": [{"startOffset": 0, "endOffset": 0, "replacement": "x"}],
        },
    )
    assert unsupported.error is not None
    assert unsupported.error.code == "INVALID_REQUEST"
    assert unsupported.error.details == {"reasonCode": "invalid_source_edit"}


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
    source = await _dispatch(
        env,
        "artifacts.source.read",
        {"sessionKey": SESSION_KEY, "documentId": document["id"]},
    )
    patched = await _dispatch(
        env,
        "artifacts.source.patch",
        {
            "sessionKey": SESSION_KEY,
            "documentId": document["id"],
            "expectedHeadRevisionId": document["headRevisionId"],
            "expectedStateRevision": document["stateRevision"],
            "expectedSourceSha256": source.payload["source"]["sha256"],
            "patches": [{"startOffset": 4, "endOffset": 10, "replacement": "fork head"}],
        },
    )
    assert patched.error is None, patched.error
    parent_head_id = patched.payload["document"]["headRevisionId"]
    service = await ArtifactSessionService.from_session_storage(env.storage)
    parent_anchor = await service.create_anchor(
        document_id=document["id"],
        revision_id=parent_head_id,
        kind=artifact_editing_rpc.AnchorKind.DOM_SOURCE,
        locator={"start_offset": 0, "start_tag_end_offset": 4, "tag_name": "h1"},
        actor=Actor(ActorKind.USER, "reviewer"),
    )
    await service.create_prompt_annotation(
        annotation_id="parent-only-annotation",
        session_key=SESSION_KEY,
        session_id=env.session.session_id,
        session_epoch=await env.storage.get_epoch(SESSION_KEY),
        document_id=document["id"],
        revision_id=parent_head_id,
        anchor_id=parent_anchor.anchor_id,
        body="parent-only instruction",
    )
    parent_head_artifact_id = (await service.get_revision(parent_head_id)).artifact_id
    await service.start_edit_session(
        document_id=document["id"],
        user_id="reviewer",
        ttl_ms=60_000,
        actor=Actor(ActorKind.USER, "reviewer"),
        edit_session_id="edit-parent-review",
    )

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
    assert await service.list_prompt_annotations(
        session_key=child_key,
        session_id=child_session.session_id,
        session_epoch=await env.storage.get_epoch(child_key),
    ) == ()

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
    source = await _dispatch(
        env,
        "artifacts.source.read",
        {"sessionKey": SESSION_KEY, "documentId": document["id"]},
    )
    patched = await _dispatch(
        env,
        "artifacts.source.patch",
        {
            "sessionKey": SESSION_KEY,
            "documentId": document["id"],
            "expectedHeadRevisionId": document["headRevisionId"],
            "expectedStateRevision": document["stateRevision"],
            "expectedSourceSha256": source.payload["source"]["sha256"],
            "patches": [{"startOffset": 4, "endOffset": 10, "replacement": "after"}],
        },
    )
    assert patched.error is None
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
