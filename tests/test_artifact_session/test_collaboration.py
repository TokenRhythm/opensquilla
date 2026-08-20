"""Change-set, anchor, and editor-session integration tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from opensquilla.artifact_session import (
    Actor,
    ActorKind,
    AnchorKind,
    ArtifactBlobRef,
    ArtifactConflictError,
    ArtifactKind,
    ArtifactSessionService,
    ArtifactValidationError,
    ChangeSetStatus,
    EditSessionStatus,
    WriterLeaseExpiredError,
)

from .test_repository import FakeClock, PredictableIds, blob

USER = Actor(ActorKind.USER, "reviewer-1")
AGENT = Actor(ActorKind.AGENT, "agent-1")


async def service_at(path: Path, clock: FakeClock) -> ArtifactSessionService:
    return await ArtifactSessionService.open(
        path,
        clock=clock,
        id_factory=PredictableIds(),
    )


@pytest.mark.asyncio
async def test_ready_change_set_applies_candidate_atomically(tmp_path: Path) -> None:
    service = await service_at(tmp_path / "artifacts.db", FakeClock())
    try:
        created = await service.create_document(
            session_key="agent:main:webchat:proposal",
            name="Proposal",
            kind=ArtifactKind.DOCUMENT,
            initial_artifact=blob("base"),
            actor=USER,
        )
        lease = await service.acquire_writer_lease(
            document_id=created.document.document_id,
            holder_id="agent-worker",
            ttl_ms=60_000,
            actor=AGENT,
        )
        proposal = await service.create_change_set(
            document_id=created.document.document_id,
            base_revision_id=created.revision.revision_id,
            operations=({"op": "replace_text", "anchor": "p1", "text": "Updated"},),
            actor=AGENT,
            turn_id="turn-agent-1",
            summary="Update the selected paragraph",
        )
        assert proposal.turn_id == "turn-agent-1"
        assert proposal.summary == "Update the selected paragraph"
        with pytest.raises(ArtifactConflictError, match="already has"):
            await service.create_change_set(
                document_id=created.document.document_id,
                base_revision_id=created.revision.revision_id,
                operations=({"op": "replace_text", "text": "Second write"},),
                actor=AGENT,
                turn_id="turn-agent-1",
            )
        ready = await service.ready_change_set(
            change_set_id=proposal.change_set_id,
            expected_state_revision=proposal.state_revision,
            candidate_artifact=blob("candidate"),
            validation={"rendered": True, "errors": []},
            actor=AGENT,
        )
        applied = await service.apply_change_set(
            change_set_id=ready.change_set_id,
            expected_change_set_state_revision=ready.state_revision,
            expected_head_revision_id=created.revision.revision_id,
            expected_document_state_revision=created.document.state_revision,
            actor=AGENT,
            lease=lease,
            require_lease=True,
        )

        stored = await service.get_change_set(ready.change_set_id)
        assert stored.status is ChangeSetStatus.APPLIED
        assert stored.applied_revision_id == applied.revision.revision_id
        assert applied.revision.change_set_id == stored.change_set_id
        assert applied.revision.artifact_id == "artifact-candidate"
        assert applied.document.head_revision_id == applied.revision.revision_id
        assert [
            event.event_type
            for event in await service.list_audit_events(created.document.document_id)
        ][-2:] == ["revision.change_set_applied", "change_set.applied"]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_agent_turn_has_at_most_one_change_set_across_documents(tmp_path: Path) -> None:
    service = await service_at(tmp_path / "global-turn.db", FakeClock())
    try:
        first = await service.create_document(
            session_key="agent:main:webchat:global-turn",
            name="First",
            kind=ArtifactKind.DOCUMENT,
            initial_artifact=blob("first"),
            actor=USER,
        )
        second = await service.create_document(
            session_key="agent:main:webchat:global-turn",
            name="Second",
            kind=ArtifactKind.DOCUMENT,
            initial_artifact=blob("second"),
            actor=USER,
        )

        await service.create_change_set(
            document_id=first.document.document_id,
            base_revision_id=first.revision.revision_id,
            operations=({"op": "replace_text", "text": "First write"},),
            actor=AGENT,
            turn_id="turn-global-change-set",
        )
        with pytest.raises(ArtifactConflictError, match="already has"):
            await service.create_change_set(
                document_id=second.document.document_id,
                base_revision_id=second.revision.revision_id,
                operations=({"op": "replace_text", "text": "Second write"},),
                actor=AGENT,
                turn_id="turn-global-change-set",
            )

        first_unkeyed = await service.create_change_set(
            document_id=first.document.document_id,
            base_revision_id=first.revision.revision_id,
            operations=({"op": "replace_text", "text": "Unkeyed first"},),
            actor=USER,
        )
        second_unkeyed = await service.create_change_set(
            document_id=second.document.document_id,
            base_revision_id=second.revision.revision_id,
            operations=({"op": "replace_text", "text": "Unkeyed second"},),
            actor=USER,
        )
        assert first_unkeyed.turn_id is None
        assert second_unkeyed.turn_id is None
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_atomic_change_set_commit_persists_only_applied_state(tmp_path: Path) -> None:
    service = await service_at(tmp_path / "atomic.db", FakeClock())
    try:
        created = await service.create_document(
            session_key="agent:main:webchat:atomic",
            name="Atomic",
            kind=ArtifactKind.DOCUMENT,
            initial_artifact=blob("base"),
            actor=USER,
        )
        lease = await service.acquire_writer_lease(
            document_id=created.document.document_id,
            holder_id="agent-worker",
            ttl_ms=60_000,
            actor=AGENT,
        )

        applied, change_set = await service.commit_change_set_atomically(
            document_id=created.document.document_id,
            base_revision_id=created.revision.revision_id,
            expected_document_state_revision=created.document.state_revision,
            operations=({"op": "replace_text", "text": "Updated"},),
            candidate_artifact=blob("candidate"),
            validation={"status": "passed"},
            actor=AGENT,
            turn_id="turn-atomic",
            summary="Update atomically",
            lease=lease,
            require_lease=True,
        )

        assert change_set.status is ChangeSetStatus.APPLIED
        assert change_set.applied_revision_id == applied.revision.revision_id
        assert applied.revision.change_set_id == change_set.change_set_id
        assert await service.get_change_set_by_turn(
            document_id=created.document.document_id,
            turn_id="turn-atomic",
        ) == change_set
        assert [
            revision.generation
            for revision in await service.list_revisions(created.document.document_id)
        ] == [2, 1]
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["cas", "lease", "validation"])
async def test_atomic_change_set_failure_leaves_no_revision_or_change_set(
    tmp_path: Path,
    failure: str,
) -> None:
    clock = FakeClock()
    service = await service_at(tmp_path / f"atomic-{failure}.db", clock)
    try:
        created = await service.create_document(
            session_key=f"agent:main:webchat:atomic-{failure}",
            name="Atomic failure",
            kind=ArtifactKind.DOCUMENT,
            initial_artifact=blob("base"),
            actor=USER,
        )
        lease = await service.acquire_writer_lease(
            document_id=created.document.document_id,
            holder_id="agent-worker",
            ttl_ms=10,
            actor=AGENT,
        )
        if failure == "lease":
            clock.advance(11)
        kwargs = {
            "document_id": created.document.document_id,
            "base_revision_id": created.revision.revision_id,
            "expected_document_state_revision": (
                created.document.state_revision + 1
                if failure == "cas"
                else created.document.state_revision
            ),
            "operations": ({"op": "replace_text", "text": "Updated"},),
            "candidate_artifact": blob("candidate"),
            "validation": ({"bad": float("nan")} if failure == "validation" else {}),
            "actor": AGENT,
            "turn_id": f"turn-{failure}",
            "lease": lease,
            "require_lease": True,
        }
        expected_error = {
            "cas": ArtifactConflictError,
            "lease": WriterLeaseExpiredError,
            "validation": ArtifactValidationError,
        }[failure]

        with pytest.raises(expected_error):
            await service.commit_change_set_atomically(**kwargs)

        assert await service.list_change_sets(created.document.document_id) == ()
        assert [
            revision.revision_id
            for revision in await service.list_revisions(created.document.document_id)
        ] == [created.revision.revision_id]
        assert (
            await service.get_document(created.document.document_id)
        ).head_revision_id == created.revision.revision_id
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_atomic_change_set_rolls_back_a_fault_after_revision_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = await service_at(tmp_path / "atomic-fault.db", FakeClock())
    try:
        created = await service.create_document(
            session_key="agent:main:webchat:atomic-fault",
            name="Atomic fault",
            kind=ArtifactKind.DOCUMENT,
            initial_artifact=blob("base"),
            actor=USER,
        )
        lease = await service.acquire_writer_lease(
            document_id=created.document.document_id,
            holder_id="agent-worker",
            ttl_ms=60_000,
            actor=AGENT,
        )
        original_append_audit = service.repository._append_audit

        async def fail_after_revision(conn, **kwargs):
            if kwargs.get("event_type") == "change_set.applied":
                raise RuntimeError("synthetic crash before transaction commit")
            return await original_append_audit(conn, **kwargs)

        monkeypatch.setattr(service.repository, "_append_audit", fail_after_revision)
        with pytest.raises(RuntimeError, match="synthetic crash"):
            await service.commit_change_set_atomically(
                document_id=created.document.document_id,
                base_revision_id=created.revision.revision_id,
                expected_document_state_revision=created.document.state_revision,
                operations=({"op": "replace_text", "text": "Updated"},),
                candidate_artifact=blob("candidate"),
                validation={"status": "passed"},
                actor=AGENT,
                turn_id="turn-crash",
                lease=lease,
                require_lease=True,
            )

        assert await service.list_change_sets(created.document.document_id) == ()
        assert [
            revision.revision_id
            for revision in await service.list_revisions(created.document.document_id)
        ] == [created.revision.revision_id]
        document = await service.get_document(created.document.document_id)
        assert document.head_revision_id == created.revision.revision_id
        event_types = [
            event.event_type
            for event in await service.list_audit_events(created.document.document_id)
        ]
        assert "change_set.created" not in event_types
        assert "revision.change_set_applied" not in event_types
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_atomic_change_set_is_reconcilable_after_response_loss_and_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "atomic-restart.db"
    clock = FakeClock()
    service = await service_at(path, clock)
    created = await service.create_document(
        session_key="agent:main:webchat:atomic-restart",
        name="Atomic restart",
        kind=ArtifactKind.DOCUMENT,
        initial_artifact=blob("base"),
        actor=USER,
    )
    lease = await service.acquire_writer_lease(
        document_id=created.document.document_id,
        holder_id="agent-worker",
        ttl_ms=60_000,
        actor=AGENT,
    )
    applied, _change_set = await service.commit_change_set_atomically(
        document_id=created.document.document_id,
        base_revision_id=created.revision.revision_id,
        expected_document_state_revision=created.document.state_revision,
        operations=({"op": "replace_text", "text": "Updated"},),
        candidate_artifact=blob("candidate"),
        validation={"status": "passed"},
        actor=AGENT,
        turn_id="turn-response-lost",
        lease=lease,
        require_lease=True,
    )
    document_id = created.document.document_id
    revision_id = applied.revision.revision_id
    await service.close()

    recovered = await service_at(path, clock)
    try:
        durable_change = await recovered.get_change_set_by_turn(
            document_id=document_id,
            turn_id="turn-response-lost",
        )
        assert durable_change is not None
        assert durable_change.status is ChangeSetStatus.APPLIED
        assert durable_change.applied_revision_id == revision_id
        assert (await recovered.get_document(document_id)).head_revision_id == revision_id
    finally:
        await recovered.close()


@pytest.mark.asyncio
async def test_anchor_and_lifecycle_edit_session_keep_revision_provenance(
    tmp_path: Path,
) -> None:
    service = await service_at(tmp_path / "artifacts.db", FakeClock())
    try:
        created = await service.create_document(
            session_key="agent:main:webchat:review",
            name="Review",
            kind=ArtifactKind.DOCUMENT,
            initial_artifact=blob("base"),
            actor=USER,
        )
        anchor = await service.create_anchor(
            document_id=created.document.document_id,
            revision_id=created.revision.revision_id,
            kind=AnchorKind.TEXT_RANGE,
            locator={"paragraph_id": "p-7", "start": 2, "end": 8},
            quote="review",
            actor=AGENT,
        )
        editing = await service.start_edit_session(
            document_id=created.document.document_id,
            user_id="user-1",
            ttl_ms=30_000,
            actor=USER,
            edit_session_id="edit-review",
        )
        touched = await service.heartbeat_edit_session(
            edit_session_id=editing.edit_session_id,
            user_id="user-1",
            expected_state_revision=editing.state_revision,
            ttl_ms=30_000,
            actor=USER,
        )
        closed = await service.close_edit_session(
            edit_session_id=touched.edit_session_id,
            user_id="user-1",
            expected_state_revision=touched.state_revision,
            actor=USER,
        )

        assert editing.base_revision_id == created.revision.revision_id
        assert editing.last_saved_revision_id == created.revision.revision_id
        assert closed.status is EditSessionStatus.CLOSED
        assert await service.get_anchor(anchor.anchor_id) == anchor
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_edit_session_save_admission_rejects_expired_lifecycle_session(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    service = await service_at(tmp_path / "artifacts.db", clock)
    try:
        created = await service.create_document(
            session_key="agent:main:webchat:edit-session-guards",
            name="Guarded edit",
            kind=ArtifactKind.DOCUMENT,
            initial_artifact=blob("base"),
            actor=USER,
        )
        edit_session = await service.start_edit_session(
            document_id=created.document.document_id,
            user_id="editor",
            ttl_ms=10,
            actor=USER,
            edit_session_id="edit-expiring",
        )
        clock.advance(11)
        with pytest.raises(ArtifactConflictError, match="expired"):
            await service.validate_edit_session_for_save(
                edit_session_id=edit_session.edit_session_id,
                document_id=created.document.document_id,
                user_id="editor",
                expected_state_revision=edit_session.state_revision,
                expected_last_saved_revision_id=edit_session.last_saved_revision_id,
            )
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_blob_hash_validation_is_enforced_before_persistence(tmp_path: Path) -> None:
    service = await service_at(tmp_path / "artifacts.db", FakeClock())
    try:
        with pytest.raises(ValueError, match="sha256"):
            await service.create_document(
                session_key="agent:main:webchat:invalid",
                name="Invalid",
                kind=ArtifactKind.DOCUMENT,
                initial_artifact=ArtifactBlobRef(
                    artifact_id="bad",
                    sha256="not-a-hash",
                    filename="bad.docx",
                    media_type="application/octet-stream",
                    byte_size=1,
                ),
                actor=USER,
            )
        assert await service.list_documents(session_key="agent:main:webchat:invalid") == ()
    finally:
        await service.close()
