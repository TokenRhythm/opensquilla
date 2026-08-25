"""Prompt-annotation draft lifecycle and same-transaction send fencing."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from opensquilla.artifact_session import (
    Actor,
    ActorKind,
    AnchorKind,
    AnchorState,
    ArtifactConflictError,
    ArtifactKind,
    ArtifactSessionService,
    ArtifactValidationError,
    PreparedPromptAnnotationTarget,
    PromptAnnotationStatus,
    consume_prepared_prompt_annotations_on_conn,
    consume_prompt_annotations_on_conn,
)

from .test_repository import FakeClock, PredictableIds, blob

USER = Actor(ActorKind.USER, "user-1")
SESSION_KEY = "agent:main:webchat:annotations"
SESSION_ID = "session-annotations"


async def _document_and_anchor(service: ArtifactSessionService):
    created = await service.create_document(
        session_key=SESSION_KEY,
        session_id=SESSION_ID,
        name="page.html",
        kind=ArtifactKind.HTML,
        initial_artifact=blob("base"),
        actor=USER,
    )
    anchor = await service.create_anchor(
        document_id=created.document.document_id,
        revision_id=created.revision.revision_id,
        kind=AnchorKind.DOM_SOURCE,
        locator={"start_offset": 0, "start_tag_end_offset": 6, "tag_name": "main"},
        quote="<main>",
        actor=USER,
    )
    return created, anchor


async def _annotation_persistence_counts(
    service: ArtifactSessionService,
    *,
    document_id: str,
) -> tuple[int, int, int]:
    async with service.repository._transaction("test.annotation_counts") as conn:
        cursor = await conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM artifact_anchors WHERE document_id = ?),
                (SELECT COUNT(*) FROM artifact_prompt_annotations WHERE document_id = ?),
                (SELECT COUNT(*) FROM artifact_audit_events
                 WHERE document_id = ? AND event_type = 'anchor.created')
            """,
            (document_id, document_id, document_id),
        )
        try:
            row = await cursor.fetchone()
        finally:
            await cursor.close()
    assert row is not None
    return int(row[0]), int(row[1]), int(row[2])


def _atomic_annotation_kwargs(created, *, annotation_id: str) -> dict[str, object]:
    return {
        "annotation_id": annotation_id,
        "session_key": SESSION_KEY,
        "session_id": SESSION_ID,
        "session_epoch": 0,
        "document_id": created.document.document_id,
        "revision_id": created.revision.revision_id,
        "kind": AnchorKind.DOM_SOURCE,
        "locator": {
            "start_offset": 0,
            "start_tag_end_offset": 6,
            "tag_name": "main",
        },
        "quote": "<main>",
        "context": {"element_path": '[["","html",1],["","body",1],["","main",1]]'},
        "actor": USER,
        "body": "Make this concise.",
    }


@pytest.mark.asyncio
async def test_atomic_anchor_and_annotation_create_is_concurrently_idempotent(
    tmp_path: Path,
) -> None:
    service = await ArtifactSessionService.open(
        tmp_path / "artifacts.db",
        clock=FakeClock(),
        id_factory=PredictableIds(),
    )
    try:
        created = await service.create_document(
            session_key=SESSION_KEY,
            session_id=SESSION_ID,
            name="page.html",
            kind=ArtifactKind.HTML,
            initial_artifact=blob("base"),
            actor=USER,
        )
        kwargs = _atomic_annotation_kwargs(created, annotation_id="annotation-atomic")

        first, second = await asyncio.gather(
            service.create_prompt_annotation_with_anchor(**kwargs),
            service.create_prompt_annotation_with_anchor(**kwargs),
        )

        assert first == second
        assert first[1].anchor_id == first[0].anchor_id
        assert await _annotation_persistence_counts(
            service,
            document_id=created.document.document_id,
        ) == (1, 1, 1)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_atomic_anchor_and_annotation_rolls_back_injected_insert_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = await ArtifactSessionService.open(
        tmp_path / "artifacts.db",
        clock=FakeClock(),
        id_factory=PredictableIds(),
    )
    try:
        created = await service.create_document(
            session_key=SESSION_KEY,
            session_id=SESSION_ID,
            name="page.html",
            kind=ArtifactKind.HTML,
            initial_artifact=blob("base"),
            actor=USER,
        )

        async def fail_annotation_insert(_conn, **_kwargs) -> None:
            raise RuntimeError("injected annotation insert failure")

        monkeypatch.setattr(
            service.repository,
            "_insert_prompt_annotation_on_conn",
            fail_annotation_insert,
        )
        with pytest.raises(RuntimeError, match="injected annotation insert failure"):
            await service.create_prompt_annotation_with_anchor(
                **_atomic_annotation_kwargs(created, annotation_id="annotation-fault")
            )

        assert await _annotation_persistence_counts(
            service,
            document_id=created.document.document_id,
        ) == (0, 0, 0)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_atomic_anchor_and_annotation_rejects_stale_head_without_residue(
    tmp_path: Path,
) -> None:
    service = await ArtifactSessionService.open(tmp_path / "artifacts.db")
    try:
        created = await service.create_document(
            session_key=SESSION_KEY,
            session_id=SESSION_ID,
            name="page.html",
            kind=ArtifactKind.HTML,
            initial_artifact=blob("base"),
            actor=USER,
        )
        await service.commit_revision(
            document_id=created.document.document_id,
            expected_head_revision_id=created.document.head_revision_id,
            expected_state_revision=created.document.state_revision,
            artifact=blob("new-head"),
            actor=USER,
        )

        with pytest.raises(ArtifactConflictError, match="no longer current"):
            await service.create_prompt_annotation_with_anchor(
                **_atomic_annotation_kwargs(created, annotation_id="annotation-stale")
            )

        assert await _annotation_persistence_counts(
            service,
            document_id=created.document.document_id,
        ) == (0, 0, 0)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_atomic_anchor_and_annotation_limit_failure_creates_no_extra_rows(
    tmp_path: Path,
) -> None:
    service = await ArtifactSessionService.open(tmp_path / "artifacts.db")
    try:
        created = await service.create_document(
            session_key=SESSION_KEY,
            session_id=SESSION_ID,
            name="page.html",
            kind=ArtifactKind.HTML,
            initial_artifact=blob("base"),
            actor=USER,
        )
        for index in range(16):
            await service.create_prompt_annotation_with_anchor(
                **_atomic_annotation_kwargs(created, annotation_id=f"annotation-{index}")
            )

        with pytest.raises(ArtifactValidationError, match="at most 16"):
            await service.create_prompt_annotation_with_anchor(
                **_atomic_annotation_kwargs(created, annotation_id="annotation-over-limit")
            )

        assert await _annotation_persistence_counts(
            service,
            document_id=created.document.document_id,
        ) == (16, 16, 16)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_prompt_annotation_create_update_preflight_and_consume(tmp_path: Path) -> None:
    service = await ArtifactSessionService.open(
        tmp_path / "artifacts.db",
        clock=FakeClock(),
        id_factory=PredictableIds(),
    )
    try:
        created, anchor = await _document_and_anchor(service)
        draft = await service.create_prompt_annotation(
            annotation_id="annotation-1",
            session_key=SESSION_KEY,
            session_id=SESSION_ID,
            session_epoch=0,
            document_id=created.document.document_id,
            revision_id=created.revision.revision_id,
            anchor_id=anchor.anchor_id,
        )
        assert draft.status is PromptAnnotationStatus.DRAFT
        assert draft.body == ""
        assert (
            await service.create_prompt_annotation(
                annotation_id="annotation-1",
                session_key=SESSION_KEY,
                session_id=SESSION_ID,
                session_epoch=0,
                document_id=created.document.document_id,
                revision_id=created.revision.revision_id,
                anchor_id=anchor.anchor_id,
            )
            == draft
        )
        updated = await service.update_prompt_annotation(
            annotation_id=draft.annotation_id,
            expected_state_revision=draft.state_revision,
            body="Make this heading concise.",
        )
        preflighted = await service.preflight_prompt_annotations(
            annotation_ids=(updated.annotation_id,),
            session_key=SESSION_KEY,
            session_id=SESSION_ID,
            session_epoch=0,
        )
        assert preflighted == (updated,)

        async with service.repository._transaction("test.consume") as conn:
            sent = await consume_prompt_annotations_on_conn(
                conn,
                expected_annotations=preflighted,
                session_key=SESSION_KEY,
                session_id=SESSION_ID,
                session_epoch=0,
                message_id="message-1",
                turn_id="turn-1",
                updated_at=1234,
            )
        assert sent[0].status is PromptAnnotationStatus.SENT
        assert sent[0].sent_message_id == "message-1"
        assert sent[0].sent_turn_id == "turn-1"
        assert sent[0].sent_order == 0
        with pytest.raises(ArtifactConflictError, match="no longer a draft"):
            await service.preflight_prompt_annotations(
                annotation_ids=(updated.annotation_id,),
                session_key=SESSION_KEY,
                session_id=SESSION_ID,
                session_epoch=0,
            )
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_prompt_annotation_cas_scope_limits_and_stale_head(tmp_path: Path) -> None:
    service = await ArtifactSessionService.open(tmp_path / "artifacts.db")
    try:
        created, anchor = await _document_and_anchor(service)
        draft = await service.create_prompt_annotation(
            annotation_id="annotation-cas",
            session_key=SESSION_KEY,
            session_id=SESSION_ID,
            session_epoch=2,
            document_id=created.document.document_id,
            revision_id=created.revision.revision_id,
            anchor_id=anchor.anchor_id,
            body="Update this element.",
        )
        with pytest.raises(ArtifactConflictError, match="state_revision"):
            await service.update_prompt_annotation(
                annotation_id=draft.annotation_id,
                expected_state_revision=99,
                body="stale",
            )
        assert await service.list_prompt_annotations(
            session_key=SESSION_KEY,
            session_id=SESSION_ID,
            session_epoch=1,
            status=PromptAnnotationStatus.DRAFT,
        ) == ()
        with pytest.raises(ArtifactValidationError, match="16 KiB"):
            await service.update_prompt_annotation(
                annotation_id=draft.annotation_id,
                expected_state_revision=draft.state_revision,
                body="😀" * 4097,
            )

        for index in range(1, 16):
            await service.create_prompt_annotation(
                annotation_id=f"annotation-{index}",
                session_key=SESSION_KEY,
                session_id=SESSION_ID,
                session_epoch=2,
                document_id=created.document.document_id,
                revision_id=created.revision.revision_id,
                anchor_id=anchor.anchor_id,
                body=f"instruction {index}",
            )
        with pytest.raises(ArtifactValidationError, match="at most 16"):
            await service.create_prompt_annotation(
                annotation_id="annotation-17",
                session_key=SESSION_KEY,
                session_id=SESSION_ID,
                session_epoch=2,
                document_id=created.document.document_id,
                revision_id=created.revision.revision_id,
                anchor_id=anchor.anchor_id,
                body="one too many",
            )

        await service.commit_revision(
            document_id=created.document.document_id,
            expected_head_revision_id=created.document.head_revision_id,
            expected_state_revision=created.document.state_revision,
            artifact=blob("new-head"),
            actor=USER,
        )
        with pytest.raises(ArtifactConflictError, match="no longer current"):
            await service.preflight_prompt_annotations(
                annotation_ids=(draft.annotation_id,),
                session_key=SESSION_KEY,
                session_id=SESSION_ID,
                session_epoch=2,
            )
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_prepared_rebind_and_consume_is_atomic_for_contextual_target(
    tmp_path: Path,
) -> None:
    service = await ArtifactSessionService.open(
        tmp_path / "artifacts.db",
        clock=FakeClock(),
        id_factory=PredictableIds(),
    )
    try:
        created, anchor = await _document_and_anchor(service)
        draft = await service.create_prompt_annotation(
            annotation_id="annotation-remap",
            session_key=SESSION_KEY,
            session_id=SESSION_ID,
            session_epoch=3,
            document_id=created.document.document_id,
            revision_id=created.revision.revision_id,
            anchor_id=anchor.anchor_id,
            body="Update this region.",
        )
        current = await service.commit_revision(
            document_id=created.document.document_id,
            expected_head_revision_id=created.revision.revision_id,
            expected_state_revision=created.document.state_revision,
            artifact=blob("current"),
            actor=USER,
        )
        prepared = PreparedPromptAnnotationTarget(
            expected_annotation=draft,
            previous_anchor_id=anchor.anchor_id,
            anchor_id="anchor-remapped",
            audit_event_id="audit-remapped",
            revision_id=current.revision.revision_id,
            kind=AnchorKind.DOM_SOURCE,
            locator={
                "tag_name": "main",
                "source_sha256": current.revision.artifact_sha256,
                "offset_encoding": "unicode-code-point",
            },
            quote="<main>",
            context={
                "semantic_profile_v1": {"version": 1, "tag_name": "main"},
                "target_reason": "no_match",
            },
            state=AnchorState.ORPHANED,
            actor_kind=ActorKind.USER,
            actor_id=USER.actor_id,
        )

        async with service.repository._transaction("test.prepared_consume") as conn:
            sent = await consume_prepared_prompt_annotations_on_conn(
                conn,
                prepared_targets=(prepared,),
                session_key=SESSION_KEY,
                session_id=SESSION_ID,
                session_epoch=3,
                message_id="message-remap",
                turn_id="turn-remap",
                updated_at=9876,
            )

        assert sent[0].status is PromptAnnotationStatus.SENT
        assert sent[0].revision_id == current.revision.revision_id
        assert sent[0].anchor_id == "anchor-remapped"
        remapped = await service.get_anchor("anchor-remapped")
        assert remapped.state is AnchorState.ORPHANED
        assert remapped.remapped_from_anchor_id == anchor.anchor_id
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_prepared_rebind_head_race_rolls_back_new_anchor(tmp_path: Path) -> None:
    service = await ArtifactSessionService.open(tmp_path / "artifacts.db")
    try:
        created, anchor = await _document_and_anchor(service)
        draft = await service.create_prompt_annotation(
            annotation_id="annotation-race",
            session_key=SESSION_KEY,
            session_id=SESSION_ID,
            session_epoch=0,
            document_id=created.document.document_id,
            revision_id=created.revision.revision_id,
            anchor_id=anchor.anchor_id,
            body="Update this region.",
        )
        prepared = PreparedPromptAnnotationTarget(
            expected_annotation=draft,
            previous_anchor_id=anchor.anchor_id,
            anchor_id="anchor-never-committed",
            audit_event_id="audit-never-committed",
            revision_id=created.revision.revision_id,
            kind=AnchorKind.DOM_SOURCE,
            locator=anchor.locator,
            quote=anchor.quote,
            context={},
            state=AnchorState.RESOLVED,
            actor_kind=ActorKind.USER,
            actor_id=USER.actor_id,
        )
        await service.commit_revision(
            document_id=created.document.document_id,
            expected_head_revision_id=created.revision.revision_id,
            expected_state_revision=created.document.state_revision,
            artifact=blob("raced"),
            actor=USER,
        )

        with pytest.raises(ArtifactConflictError, match="changed"):
            async with service.repository._transaction("test.prepared_race") as conn:
                await consume_prepared_prompt_annotations_on_conn(
                    conn,
                    prepared_targets=(prepared,),
                    session_key=SESSION_KEY,
                    session_id=SESSION_ID,
                    session_epoch=0,
                    message_id="message-race",
                    turn_id="turn-race",
                    updated_at=1,
                )
        with pytest.raises(Exception, match="not found"):
            await service.get_anchor("anchor-never-committed")
        assert (await service.get_prompt_annotation(draft.annotation_id)).status is (
            PromptAnnotationStatus.DRAFT
        )
    finally:
        await service.close()
