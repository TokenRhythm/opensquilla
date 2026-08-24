"""Restart recovery contracts for journaled artifact mutation candidates."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from opensquilla.artifact_session import (
    Actor,
    ActorKind,
    ArtifactBlobRef,
    ArtifactKind,
    ArtifactSessionService,
    ChangeSetStatus,
    MutationAttemptStatus,
)
from opensquilla.artifacts import ArtifactNotFoundError, ArtifactStore
from opensquilla.gateway.artifact_mutation_recovery import (
    reconcile_pending_artifact_mutations,
    reject_orphaned_artifact_drafts,
)

USER = Actor(ActorKind.USER, "user-recovery")
AGENT = Actor(ActorKind.AGENT, "agent-recovery")
SESSION_ID = "synthetic-recovery-session"


def _blob(label: str) -> ArtifactBlobRef:
    digest = hashlib.sha256(label.encode()).hexdigest()
    return ArtifactBlobRef(
        artifact_id=f"artifact-{label}",
        sha256=digest,
        filename="page.html",
        media_type="text/html",
        byte_size=len(label),
    )


def _blob_from_ref(ref) -> ArtifactBlobRef:
    return ArtifactBlobRef(
        artifact_id=ref.id,
        sha256=ref.sha256,
        filename=ref.name,
        media_type=ref.mime,
        byte_size=ref.size,
    )


async def _created(service: ArtifactSessionService):
    return await service.create_document(
        session_key="agent:main:webchat:synthetic-recovery",
        session_id=SESSION_ID,
        name="Synthetic recovery page",
        kind=ArtifactKind.HTML,
        initial_artifact=_blob("base"),
        actor=USER,
    )


async def _reserve(
    service: ArtifactSessionService,
    *,
    document_id: str,
    revision_id: str,
    turn_id: str,
):
    return await service.reserve_mutation_attempt(
        document_id=document_id,
        turn_id=turn_id,
        tool_use_id=f"tool-{turn_id}",
        base_revision_id=revision_id,
        proposal_sha256="a" * 64,
    )


@pytest.mark.asyncio
async def test_restart_terminalizes_reserve_only_attempt(tmp_path: Path) -> None:
    service = await ArtifactSessionService.open(tmp_path / "sessions.db")
    try:
        created = await _created(service)
        await _reserve(
            service,
            document_id=created.document.document_id,
            revision_id=created.revision.revision_id,
            turn_id="reserve-only",
        )

        summary = await reconcile_pending_artifact_mutations(
            service,
            ArtifactStore(tmp_path / "media"),
        )

        assert summary.examined == 1
        assert summary.failed == 1
        receipt = await service.reconcile_mutation_attempt(
            document_id=created.document.document_id,
            turn_id="reserve-only",
            tool_use_id="tool-reserve-only",
        )
        assert receipt.status is MutationAttemptStatus.FAILED
        assert receipt.failure_code == "process_restarted_before_candidate"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_restart_rejects_orphaned_candidate_draft(tmp_path: Path) -> None:
    service = await ArtifactSessionService.open(tmp_path / "draft-recovery.db")
    store = ArtifactStore(tmp_path / "media")
    try:
        created = await _created(service)
        draft = await service.create_change_set(
            document_id=created.document.document_id,
            base_revision_id=created.revision.revision_id,
            operations=({"op": "replace_text"},),
            actor=AGENT,
            turn_id="orphan-draft",
            candidate_loop=True,
        )
        payload = b"<h1>orphan</h1>"
        ref = store.publish_bytes(
            payload,
            session_id=SESSION_ID,
            session_key=created.document.session_key,
            name="orphan.html",
            mime="text/html",
            source="artifact_html_agent_candidate",
            visibility="internal",
        )
        staged = await service.update_draft_change_set_candidate(
            change_set_id=draft.change_set_id,
            expected_state_revision=draft.state_revision,
            candidate_artifact=_blob_from_ref(ref),
            operations=({"op": "replace_text"},),
            validation={"status": "candidate_staged"},
            actor=AGENT,
        )

        summary = await reject_orphaned_artifact_drafts(service, store)

        assert summary.examined == 1
        assert summary.rejected == 1
        assert staged.status.value == "draft"
        rejected = await service.get_change_set(draft.change_set_id)
        assert rejected.status.value == "rejected"
        with pytest.raises(ArtifactNotFoundError):
            store.resolve_for_download(ref.id, session_id=SESSION_ID)
        assert len(await service.list_revisions(created.document.document_id)) == 1
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_restart_removes_candidate_published_before_draft_cas(tmp_path: Path) -> None:
    """A crash between blob publication and candidate CAS must not leak bytes."""

    service = await ArtifactSessionService.open(tmp_path / "pre-cas-orphan.db")
    store = ArtifactStore(tmp_path / "media")
    try:
        created = await _created(service)
        turn_id = "pre-cas-orphan"
        draft = await service.create_change_set(
            document_id=created.document.document_id,
            base_revision_id=created.revision.revision_id,
            operations=({"op": "replace_text"},),
            actor=AGENT,
            turn_id=turn_id,
            candidate_loop=True,
        )
        turn_digest = hashlib.sha256(turn_id.encode("utf-8")).hexdigest()
        ref = store.publish_bytes(
            b"<h1>published before CAS</h1>",
            session_id=SESSION_ID,
            session_key=created.document.session_key,
            name="pre-cas.html",
            mime="text/html",
            source=f"document_html_agent_candidate:{turn_digest}",
            visibility="internal",
        )

        summary = await reject_orphaned_artifact_drafts(service, store)

        assert summary.examined == 1
        assert summary.rejected == 1
        assert summary.deleted_candidates == 1
        assert (await service.get_change_set(draft.change_set_id)).status.value == "rejected"
        with pytest.raises(ArtifactNotFoundError):
            store.resolve_for_download(ref.id, session_id=SESSION_ID)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_restart_leaves_ordinary_manual_draft_untouched(tmp_path: Path) -> None:
    """Boot cleanup must not reject collaboration/manual DRAFT change sets."""

    service = await ArtifactSessionService.open(tmp_path / "manual-draft.db")
    store = ArtifactStore(tmp_path / "media")
    try:
        created = await _created(service)
        draft = await service.create_change_set(
            document_id=created.document.document_id,
            base_revision_id=created.revision.revision_id,
            operations=({"op": "replace_text"},),
            actor=AGENT,
            # Collaboration/review may key an ordinary agent proposal by turn;
            # the explicit candidate_loop audit flag remains false.
            turn_id="ordinary-agent-draft",
        )
        ref = store.publish_bytes(
            b"<h1>manual draft</h1>",
            session_id=SESSION_ID,
            session_key=created.document.session_key,
            name="manual.html",
            mime="text/html",
            source="document_manual_draft",
            visibility="internal",
        )
        staged = await service.update_draft_change_set_candidate(
            change_set_id=draft.change_set_id,
            expected_state_revision=draft.state_revision,
            candidate_artifact=_blob_from_ref(ref),
            operations=({"op": "replace_text"},),
            validation={"status": "candidate_staged"},
            actor=USER,
        )

        summary = await reject_orphaned_artifact_drafts(service, store)

        assert summary.examined == 0
        assert summary.rejected == 0
        assert summary.ambiguous == 0
        current = await service.get_change_set(draft.change_set_id)
        assert current.status is ChangeSetStatus.DRAFT
        assert current.state_revision == staged.state_revision
        assert store.resolve_for_download(ref.id, session_id=SESSION_ID)[0].id == ref.id
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_mutation_recovery_rejects_candidate_draft_before_terminalizing_receipt(
    tmp_path: Path,
) -> None:
    """A crash during finish rejects the candidate before marking receipt failed."""

    service = await ArtifactSessionService.open(tmp_path / "draft-attempt.db")
    store = ArtifactStore(tmp_path / "media")
    try:
        created = await _created(service)
        turn_id = "draft-attempt"
        draft = await service.create_change_set(
            document_id=created.document.document_id,
            base_revision_id=created.revision.revision_id,
            operations=({"op": "replace_text"},),
            actor=AGENT,
            turn_id=turn_id,
            candidate_loop=True,
        )
        await _reserve(
            service,
            document_id=created.document.document_id,
            revision_id=created.revision.revision_id,
            turn_id=turn_id,
        )
        payload = b"<h1>staged before finish</h1>"
        artifact_id = store.allocate_artifact_id()
        digest = hashlib.sha256(payload).hexdigest()
        await service.register_mutation_candidate(
            document_id=created.document.document_id,
            turn_id=turn_id,
            candidate_session_id=SESSION_ID,
            candidate_artifact_id=artifact_id,
            candidate_artifact_sha256=digest,
        )
        ref = store.publish_bytes(
            payload,
            session_id=SESSION_ID,
            session_key=created.document.session_key,
            name="draft-attempt.html",
            mime="text/html",
            source="artifact_html_agent_edit",
            visibility="internal",
            artifact_id=artifact_id,
        )
        await service.update_draft_change_set_candidate(
            change_set_id=draft.change_set_id,
            expected_state_revision=draft.state_revision,
            candidate_artifact=_blob_from_ref(ref),
            operations=({"op": "replace_text"},),
            validation={"status": "candidate_staged"},
            actor=AGENT,
        )

        summary = await reconcile_pending_artifact_mutations(service, store)

        assert summary.failed == 1
        assert summary.ambiguous == 0
        assert (
            await service.get_change_set(draft.change_set_id)
        ).status is ChangeSetStatus.REJECTED
        receipt = await service.reconcile_mutation_attempt(
            document_id=created.document.document_id,
            turn_id=turn_id,
            tool_use_id=f"tool-{turn_id}",
        )
        assert receipt.status is MutationAttemptStatus.FAILED
        with pytest.raises(ArtifactNotFoundError):
            store.resolve_for_download(ref.id, session_id=SESSION_ID)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_mutation_recovery_preserves_unmarked_agent_draft(
    tmp_path: Path,
) -> None:
    """An ordinary turn-scoped agent draft is not a candidate-loop draft."""

    service = await ArtifactSessionService.open(tmp_path / "ordinary-attempt.db")
    store = ArtifactStore(tmp_path / "media")
    try:
        created = await _created(service)
        turn_id = "ordinary-attempt"
        draft = await service.create_change_set(
            document_id=created.document.document_id,
            base_revision_id=created.revision.revision_id,
            operations=({"op": "replace_text"},),
            actor=AGENT,
            turn_id=turn_id,
        )
        await _reserve(
            service,
            document_id=created.document.document_id,
            revision_id=created.revision.revision_id,
            turn_id=turn_id,
        )
        payload = b"<h1>ordinary proposal</h1>"
        artifact_id = store.allocate_artifact_id()
        digest = hashlib.sha256(payload).hexdigest()
        await service.register_mutation_candidate(
            document_id=created.document.document_id,
            turn_id=turn_id,
            candidate_session_id=SESSION_ID,
            candidate_artifact_id=artifact_id,
            candidate_artifact_sha256=digest,
        )
        ref = store.publish_bytes(
            payload,
            session_id=SESSION_ID,
            session_key=created.document.session_key,
            name="ordinary-proposal.html",
            mime="text/html",
            source="artifact_html_agent_edit",
            visibility="internal",
            artifact_id=artifact_id,
        )
        await service.update_draft_change_set_candidate(
            change_set_id=draft.change_set_id,
            expected_state_revision=draft.state_revision,
            candidate_artifact=_blob_from_ref(ref),
            operations=({"op": "replace_text"},),
            validation={"status": "candidate_staged"},
            actor=AGENT,
        )

        summary = await reconcile_pending_artifact_mutations(service, store)

        assert summary.ambiguous == 1
        assert summary.failed == 0
        assert (await service.get_change_set(draft.change_set_id)).status is ChangeSetStatus.DRAFT
        receipt = await service.reconcile_mutation_attempt(
            document_id=created.document.document_id,
            turn_id=turn_id,
            tool_use_id=f"tool-{turn_id}",
        )
        assert receipt.status is MutationAttemptStatus.AMBIGUOUS
        assert store.resolve_for_download(ref.id, session_id=SESSION_ID)[0].id == ref.id
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_restart_retries_lost_candidate_draft_reject_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reject CAS that loses its response converges from the durable row."""

    service = await ArtifactSessionService.open(tmp_path / "reject-response.db")
    store = ArtifactStore(tmp_path / "media")
    try:
        created = await _created(service)
        draft = await service.create_change_set(
            document_id=created.document.document_id,
            base_revision_id=created.revision.revision_id,
            operations=({"op": "replace_text"},),
            actor=AGENT,
            turn_id="reject-response",
            candidate_loop=True,
        )
        ref = store.publish_bytes(
            b"<h1>lost reject response</h1>",
            session_id=SESSION_ID,
            session_key=created.document.session_key,
            name="lost-reject.html",
            mime="text/html",
            source="artifact_html_agent_candidate",
            visibility="internal",
        )
        await service.update_draft_change_set_candidate(
            change_set_id=draft.change_set_id,
            expected_state_revision=draft.state_revision,
            candidate_artifact=_blob_from_ref(ref),
            operations=({"op": "replace_text"},),
            validation={"status": "candidate_staged"},
            actor=AGENT,
        )
        original_reject = (
            service.reject_candidate_draft_and_fail_attempt_for_recovery
        )

        async def reject_then_lose_response(**kwargs: object):
            await original_reject(**kwargs)
            raise RuntimeError("reject response lost")

        monkeypatch.setattr(
            service,
            "reject_candidate_draft_and_fail_attempt_for_recovery",
            reject_then_lose_response,
        )

        summary = await reject_orphaned_artifact_drafts(service, store)

        assert summary.rejected == 1
        assert (
            await service.get_change_set(draft.change_set_id)
        ).status is ChangeSetStatus.REJECTED
        with pytest.raises(ArtifactNotFoundError):
            store.resolve_for_download(ref.id, session_id=SESSION_ID)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_restart_cleans_superseded_candidates_after_applied_commit(
    tmp_path: Path,
) -> None:
    """A failed eager delete cannot strand a replaced candidate after commit."""

    service = await ArtifactSessionService.open(tmp_path / "applied-candidates.db")
    store = ArtifactStore(tmp_path / "media")
    try:
        created = await _created(service)
        turn_id = "applied-superseded"
        turn_digest = hashlib.sha256(turn_id.encode("utf-8")).hexdigest()
        source = f"document_html_agent_candidate:{turn_digest}"
        draft = await service.create_change_set(
            document_id=created.document.document_id,
            base_revision_id=created.revision.revision_id,
            operations=({"op": "replace_text"},),
            actor=AGENT,
            turn_id=turn_id,
            candidate_loop=True,
        )
        first_ref = store.publish_bytes(
            b"<h1>candidate one</h1>",
            session_id=SESSION_ID,
            session_key=created.document.session_key,
            name="candidate-one.html",
            mime="text/html",
            source=source,
            visibility="internal",
        )
        first = await service.update_draft_change_set_candidate(
            change_set_id=draft.change_set_id,
            expected_state_revision=draft.state_revision,
            candidate_artifact=_blob_from_ref(first_ref),
            operations=({"op": "replace_text"},),
            validation={"status": "candidate_staged"},
            actor=AGENT,
        )
        second_ref = store.publish_bytes(
            b"<h1>candidate two</h1>",
            session_id=SESSION_ID,
            session_key=created.document.session_key,
            name="candidate-two.html",
            mime="text/html",
            source=source,
            visibility="internal",
        )
        second = await service.update_draft_change_set_candidate(
            change_set_id=draft.change_set_id,
            expected_state_revision=first.state_revision,
            candidate_artifact=_blob_from_ref(second_ref),
            operations=({"op": "replace_text"},),
            validation={"status": "verification_passed"},
            actor=AGENT,
        )
        await service.commit_draft_change_set_atomically(
            change_set_id=draft.change_set_id,
            expected_change_set_state_revision=second.state_revision,
            expected_head_revision_id=created.revision.revision_id,
            expected_document_state_revision=created.document.state_revision,
            actor=AGENT,
            expected_candidate_sha256=second_ref.sha256,
        )
        # Simulate a process dying after the final DRAFT -> APPLIED transaction
        # but before the best-effort deletion of the superseded first blob.
        assert store.resolve_for_download(first_ref.id, session_id=SESSION_ID)
        assert store.resolve_for_download(second_ref.id, session_id=SESSION_ID)

        summary = await reject_orphaned_artifact_drafts(service, store)

        assert summary.examined == 0
        assert summary.rejected == 0
        assert summary.deleted_candidates == 1
        with pytest.raises(ArtifactNotFoundError):
            store.resolve_for_download(first_ref.id, session_id=SESSION_ID)
        assert (
            store.resolve_for_download(second_ref.id, session_id=SESSION_ID)[0].id
            == second_ref.id
        )
        assert len(await service.list_revisions(created.document.document_id)) == 2
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_cleanup_markers_advance_past_bounded_applied_journal_page(
    tmp_path: Path,
) -> None:
    service = await ArtifactSessionService.open(tmp_path / "applied-cleanup-pagination.db")
    store = ArtifactStore(tmp_path / "media")
    superseded = []
    try:
        for index in range(6):
            created = await service.create_document(
                session_key=f"agent:main:webchat:applied-page-{index}",
                session_id=SESSION_ID,
                name=f"Applied page {index}",
                kind=ArtifactKind.HTML,
                initial_artifact=_blob(f"base-page-{index}"),
                actor=USER,
            )
            turn_id = f"applied-cleanup-page-{index}"
            digest = hashlib.sha256(turn_id.encode()).hexdigest()
            source = f"document_html_agent_candidate:{digest}"
            draft = await service.create_change_set(
                document_id=created.document.document_id,
                base_revision_id=created.revision.revision_id,
                operations=({"op": "replace_text"},),
                actor=AGENT,
                turn_id=turn_id,
                candidate_loop=True,
            )
            old_ref = store.publish_bytes(
                f"<h1>old {index}</h1>".encode(),
                session_id=SESSION_ID,
                session_key=created.document.session_key,
                name=f"old-{index}.html",
                mime="text/html",
                source=source,
                visibility="internal",
            )
            superseded.append(old_ref)
            old = await service.update_draft_change_set_candidate(
                change_set_id=draft.change_set_id,
                expected_state_revision=draft.state_revision,
                candidate_artifact=_blob_from_ref(old_ref),
                operations=({"op": "replace_text"},),
                validation={"status": "candidate_staged"},
                actor=AGENT,
            )
            final_ref = store.publish_bytes(
                f"<h1>final {index}</h1>".encode(),
                session_id=SESSION_ID,
                session_key=created.document.session_key,
                name=f"final-{index}.html",
                mime="text/html",
                source=source,
                visibility="internal",
            )
            final = await service.update_draft_change_set_candidate(
                change_set_id=draft.change_set_id,
                expected_state_revision=old.state_revision,
                candidate_artifact=_blob_from_ref(final_ref),
                operations=({"op": "replace_text"},),
                validation={"status": "verification_passed"},
                actor=AGENT,
            )
            await service.commit_draft_change_set_atomically(
                change_set_id=draft.change_set_id,
                expected_change_set_state_revision=final.state_revision,
                expected_head_revision_id=created.revision.revision_id,
                expected_document_state_revision=created.document.state_revision,
                actor=AGENT,
                expected_candidate_sha256=final_ref.sha256,
            )

        first = await reject_orphaned_artifact_drafts(service, store, batch_size=1)
        second = await reject_orphaned_artifact_drafts(service, store, batch_size=1)
        assert first.deleted_candidates == 5
        assert second.deleted_candidates == 1
        assert await service.list_applied_candidate_artifacts(limit=10) == ()
        for ref in superseded:
            with pytest.raises(ArtifactNotFoundError):
                store.resolve_for_download(ref.id, session_id=SESSION_ID)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_restart_preserves_candidates_when_applied_head_blob_is_missing(
    tmp_path: Path,
) -> None:
    """Never sweep turn candidates when the durable final artifact is absent."""

    service = await ArtifactSessionService.open(tmp_path / "applied-missing-head.db")
    store = ArtifactStore(tmp_path / "media")
    try:
        created = await _created(service)
        turn_id = "applied-missing-head"
        digest = hashlib.sha256(turn_id.encode("utf-8")).hexdigest()
        source = f"document_html_agent_candidate:{digest}"
        draft = await service.create_change_set(
            document_id=created.document.document_id,
            base_revision_id=created.revision.revision_id,
            operations=({"op": "replace_text"},),
            actor=AGENT,
            turn_id=turn_id,
            candidate_loop=True,
        )
        first_ref = store.publish_bytes(
            b"<h1>candidate one</h1>",
            session_id=SESSION_ID,
            session_key=created.document.session_key,
            name="candidate-one.html",
            mime="text/html",
            source=source,
            visibility="internal",
        )
        first = await service.update_draft_change_set_candidate(
            change_set_id=draft.change_set_id,
            expected_state_revision=draft.state_revision,
            candidate_artifact=_blob_from_ref(first_ref),
            operations=({"op": "replace_text"},),
            validation={"status": "candidate_staged"},
            actor=AGENT,
        )
        final_ref = store.publish_bytes(
            b"<h1>candidate final</h1>",
            session_id=SESSION_ID,
            session_key=created.document.session_key,
            name="candidate-final.html",
            mime="text/html",
            source=source,
            visibility="internal",
        )
        final = await service.update_draft_change_set_candidate(
            change_set_id=draft.change_set_id,
            expected_state_revision=first.state_revision,
            candidate_artifact=_blob_from_ref(final_ref),
            operations=({"op": "replace_text"},),
            validation={"status": "verification_passed"},
            actor=AGENT,
        )
        await service.commit_draft_change_set_atomically(
            change_set_id=draft.change_set_id,
            expected_change_set_state_revision=final.state_revision,
            expected_head_revision_id=created.revision.revision_id,
            expected_document_state_revision=created.document.state_revision,
            actor=AGENT,
            expected_candidate_sha256=final_ref.sha256,
        )
        assert store.delete_ref(session_id=SESSION_ID, artifact_id=final_ref.id)

        summary = await reject_orphaned_artifact_drafts(service, store)

        assert summary.examined == 0
        assert summary.deleted_candidates == 0
        assert summary.ambiguous >= 1
        # The superseded blob remains available for manual repair/reconcile;
        # cleanup must not delete it after discovering a missing final head.
        assert store.resolve_for_download(first_ref.id, session_id=SESSION_ID)[0].id == first_ref.id
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_restart_rejects_draft_and_closes_reserved_finish_receipt(
    tmp_path: Path,
) -> None:
    service = await ArtifactSessionService.open(tmp_path / "draft-reserved.db")
    store = ArtifactStore(tmp_path / "media")
    try:
        created = await _created(service)
        turn_id = "orphan-draft-reserved"
        draft = await service.create_change_set(
            document_id=created.document.document_id,
            base_revision_id=created.revision.revision_id,
            operations=({"op": "replace_text"},),
            actor=AGENT,
            turn_id=turn_id,
            candidate_loop=True,
        )
        await _reserve(
            service,
            document_id=created.document.document_id,
            revision_id=created.revision.revision_id,
            turn_id=turn_id,
        )
        payload = b"<h1>orphan-reserved</h1>"
        ref = store.publish_bytes(
            payload,
            session_id=SESSION_ID,
            session_key=created.document.session_key,
            name="orphan-reserved.html",
            mime="text/html",
            source="artifact_html_agent_candidate",
            visibility="internal",
        )
        await service.update_draft_change_set_candidate(
            change_set_id=draft.change_set_id,
            expected_state_revision=draft.state_revision,
            candidate_artifact=_blob_from_ref(ref),
            operations=({"op": "replace_text"},),
            validation={"status": "candidate_staged"},
            actor=AGENT,
        )

        draft_summary = await reject_orphaned_artifact_drafts(service, store)
        assert draft_summary.rejected == 1

        # The draft rejection proves that no durable revision can be produced
        # by the pre-commit receipt.  The subsequent pass must therefore see
        # no ambiguous unresolved attempt.
        mutation_summary = await reconcile_pending_artifact_mutations(service, store)
        assert mutation_summary.examined == 0
        receipt = await service.reconcile_mutation_attempt(
            document_id=created.document.document_id,
            turn_id=turn_id,
            tool_use_id=f"tool-{turn_id}",
        )
        assert receipt.status is MutationAttemptStatus.FAILED
        assert receipt.failure_code == "process_restarted_before_commit"
        with pytest.raises(ArtifactNotFoundError):
            store.resolve_for_download(ref.id, session_id=SESSION_ID)
        assert len(await service.list_revisions(created.document.document_id)) == 1
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_restart_retries_physical_cleanup_after_reject_transaction(
    tmp_path: Path,
) -> None:
    """A crash after the reject CAS is recoverable without a schema marker."""

    service = await ArtifactSessionService.open(tmp_path / "reject-journal.db")
    store = ArtifactStore(tmp_path / "media")
    try:
        created = await _created(service)
        draft = await service.create_change_set(
            document_id=created.document.document_id,
            base_revision_id=created.revision.revision_id,
            operations=({"op": "replace_text"},),
            actor=AGENT,
            turn_id="reject-journal",
            candidate_loop=True,
        )
        ref = store.publish_bytes(
            b"<h1>journaled orphan</h1>",
            session_id=SESSION_ID,
            session_key=created.document.session_key,
            name="journaled-orphan.html",
            mime="text/html",
            source="artifact_html_agent_candidate",
            visibility="internal",
        )
        await service.update_draft_change_set_candidate(
            change_set_id=draft.change_set_id,
            expected_state_revision=draft.state_revision,
            candidate_artifact=_blob_from_ref(ref),
            operations=({"op": "replace_text"},),
            validation={"status": "candidate_staged"},
            actor=AGENT,
        )

        # Model the process dying after this transaction commits but before
        # the ArtifactStore deletion can run.
        await service.reject_draft_change_set_and_cleanup(
            change_set_id=draft.change_set_id,
            expected_state_revision=draft.state_revision + 1,
            actor=AGENT,
            reason="user_cancelled",
        )
        assert store.resolve_for_download(ref.id, session_id=SESSION_ID)

        summary = await reject_orphaned_artifact_drafts(service, store)
        assert summary.deleted_candidates == 1
        with pytest.raises(ArtifactNotFoundError):
            store.resolve_for_download(ref.id, session_id=SESSION_ID)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_cleanup_markers_advance_past_bounded_rejected_journal_page(
    tmp_path: Path,
) -> None:
    """Cleaned audit rows must not permanently starve later candidates."""

    service = await ArtifactSessionService.open(tmp_path / "cleanup-pagination.db")
    store = ArtifactStore(tmp_path / "media")
    refs = []
    try:
        created = await _created(service)
        for index in range(6):
            draft = await service.create_change_set(
                document_id=created.document.document_id,
                base_revision_id=created.revision.revision_id,
                operations=({"op": "replace_text", "index": index},),
                actor=AGENT,
                turn_id=f"cleanup-page-{index}",
                candidate_loop=True,
            )
            ref = store.publish_bytes(
                f"<h1>candidate {index}</h1>".encode(),
                session_id=SESSION_ID,
                session_key=created.document.session_key,
                name=f"candidate-{index}.html",
                mime="text/html",
                source="document_html_agent_candidate:cleanup-page",
                visibility="internal",
            )
            refs.append(ref)
            staged = await service.update_draft_change_set_candidate(
                change_set_id=draft.change_set_id,
                expected_state_revision=draft.state_revision,
                candidate_artifact=_blob_from_ref(ref),
                operations=({"op": "replace_text", "index": index},),
                validation={"status": "candidate_staged"},
                actor=AGENT,
            )
            await service.reject_draft_change_set_and_cleanup(
                change_set_id=draft.change_set_id,
                expected_state_revision=staged.state_revision,
                actor=AGENT,
                reason="synthetic cleanup backlog",
            )

        deleted = 0
        passes = 0
        while await service.list_rejected_candidate_artifacts(limit=10):
            summary = await reject_orphaned_artifact_drafts(
                service,
                store,
                batch_size=1,
            )
            assert summary.deleted_candidates > 0
            deleted += summary.deleted_candidates
            passes += 1
            assert passes <= 6
        assert deleted == 6
        assert passes >= 2
        assert await service.list_rejected_candidate_artifacts(limit=10) == ()
        for ref in refs:
            with pytest.raises(ArtifactNotFoundError):
                store.resolve_for_download(ref.id, session_id=SESSION_ID)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_recovery_does_not_delete_candidate_on_ordinary_reject(tmp_path: Path) -> None:
    """Only candidate-loop cleanup may detach/delete a rejected proposal blob."""

    service = await ArtifactSessionService.open(tmp_path / "ordinary-reject.db")
    store = ArtifactStore(tmp_path / "media")
    try:
        created = await _created(service)
        draft = await service.create_change_set(
            document_id=created.document.document_id,
            base_revision_id=created.revision.revision_id,
            operations=({"op": "replace_text"},),
            actor=AGENT,
            turn_id="ordinary-reject",
        )
        ref = store.publish_bytes(
            b"<h1>ordinary rejected proposal</h1>",
            session_id=SESSION_ID,
            session_key=created.document.session_key,
            name="ordinary-rejected.html",
            mime="text/html",
            source="document_manual_proposal",
            visibility="internal",
        )
        staged = await service.update_draft_change_set_candidate(
            change_set_id=draft.change_set_id,
            expected_state_revision=draft.state_revision,
            candidate_artifact=_blob_from_ref(ref),
            operations=({"op": "replace_text"},),
            validation={"status": "candidate_staged"},
            actor=AGENT,
        )
        await service.reject_change_set(
            change_set_id=draft.change_set_id,
            expected_state_revision=staged.state_revision,
            actor=AGENT,
            reason="reviewer_declined",
        )

        summary = await reject_orphaned_artifact_drafts(service, store)

        assert summary.rejected == 0
        assert summary.deleted_candidates == 0
        assert store.resolve_for_download(ref.id, session_id=SESSION_ID)[0].id == ref.id
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_global_recovery_database_failure_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = await ArtifactSessionService.open(tmp_path / "sessions.db")

    async def fail_list(**_kwargs):
        raise RuntimeError("synthetic recovery database failure")

    monkeypatch.setattr(service, "list_unresolved_mutation_attempts", fail_list)
    try:
        with pytest.raises(RuntimeError, match="recovery database failure"):
            await reconcile_pending_artifact_mutations(
                service,
                ArtifactStore(tmp_path / "media"),
            )
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_restart_deletes_published_candidate_without_commit(tmp_path: Path) -> None:
    service = await ArtifactSessionService.open(tmp_path / "sessions.db")
    store = ArtifactStore(tmp_path / "media")
    try:
        created = await _created(service)
        await _reserve(
            service,
            document_id=created.document.document_id,
            revision_id=created.revision.revision_id,
            turn_id="published-only",
        )
        payload = b"<h1>candidate</h1>"
        artifact_id = store.allocate_artifact_id()
        await service.register_mutation_candidate(
            document_id=created.document.document_id,
            turn_id="published-only",
            candidate_session_id=SESSION_ID,
            candidate_artifact_id=artifact_id,
            candidate_artifact_sha256=hashlib.sha256(payload).hexdigest(),
        )
        store.publish_bytes(
            payload,
            session_id=SESSION_ID,
            session_key=created.document.session_key,
            name="candidate.html",
            mime="text/html",
            source="artifact_html_agent_edit",
            visibility="internal",
            artifact_id=artifact_id,
        )

        summary = await reconcile_pending_artifact_mutations(service, store)

        assert summary.failed == 1
        assert summary.deleted_candidates == 1
        with pytest.raises(ArtifactNotFoundError):
            store.resolve_for_download(artifact_id, session_id=SESSION_ID)
        assert await service.list_change_sets(created.document.document_id) == ()
        assert len(await service.list_revisions(created.document.document_id)) == 1
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_restart_recovers_commit_before_attempt_terminalization(tmp_path: Path) -> None:
    path = tmp_path / "sessions.db"
    store = ArtifactStore(tmp_path / "media")
    service = await ArtifactSessionService.open(path)
    created = await _created(service)
    await _reserve(
        service,
        document_id=created.document.document_id,
        revision_id=created.revision.revision_id,
        turn_id="committed",
    )
    payload = b"<h1>committed</h1>"
    artifact_id = store.allocate_artifact_id()
    await service.register_mutation_candidate(
        document_id=created.document.document_id,
        turn_id="committed",
        candidate_session_id=SESSION_ID,
        candidate_artifact_id=artifact_id,
        candidate_artifact_sha256=hashlib.sha256(payload).hexdigest(),
    )
    ref = store.publish_bytes(
        payload,
        session_id=SESSION_ID,
        session_key=created.document.session_key,
        name="candidate.html",
        mime="text/html",
        source="artifact_html_agent_edit",
        visibility="internal",
        artifact_id=artifact_id,
    )
    applied, change = await service.commit_change_set_atomically(
        document_id=created.document.document_id,
        base_revision_id=created.revision.revision_id,
        expected_document_state_revision=created.document.state_revision,
        operations=({"op": "replace_text"},),
        candidate_artifact=_blob_from_ref(ref),
        validation={"status": "passed"},
        actor=AGENT,
        turn_id="committed",
    )
    await service.close()

    recovered = await ArtifactSessionService.open(path)
    try:
        summary = await reconcile_pending_artifact_mutations(recovered, store)
        assert summary.applied == 1
        assert summary.deleted_candidates == 0
        receipt = await recovered.reconcile_mutation_attempt(
            document_id=created.document.document_id,
            turn_id="committed",
            tool_use_id="tool-committed",
        )
        assert receipt.status is MutationAttemptStatus.APPLIED
        assert receipt.change_set_id == change.change_set_id
        assert receipt.revision_id == applied.revision.revision_id
        assert store.resolve_for_download(artifact_id, session_id=SESSION_ID)[0] == ref
    finally:
        await recovered.close()


@pytest.mark.asyncio
async def test_restart_marks_candidate_cleanup_integrity_error_ambiguous(
    tmp_path: Path,
) -> None:
    service = await ArtifactSessionService.open(tmp_path / "sessions.db")
    store = ArtifactStore(tmp_path / "media")
    try:
        created = await _created(service)
        await _reserve(
            service,
            document_id=created.document.document_id,
            revision_id=created.revision.revision_id,
            turn_id="unsafe-bucket",
        )
        payload = b"<h1>candidate</h1>"
        artifact_id = store.allocate_artifact_id()
        await service.register_mutation_candidate(
            document_id=created.document.document_id,
            turn_id="unsafe-bucket",
            candidate_session_id=SESSION_ID,
            candidate_artifact_id=artifact_id,
            candidate_artifact_sha256=hashlib.sha256(payload).hexdigest(),
        )
        ref = store.publish_bytes(
            payload,
            session_id=SESSION_ID,
            session_key=created.document.session_key,
            name="candidate.html",
            mime="text/html",
            source="artifact_html_agent_edit",
            artifact_id=artifact_id,
        )
        (store.path_for(ref).parent / ".artifact-id").write_text(
            store.allocate_artifact_id() + "\n",
            encoding="ascii",
        )

        summary = await reconcile_pending_artifact_mutations(service, store)

        assert summary.ambiguous == 1
        receipt = await service.reconcile_mutation_attempt(
            document_id=created.document.document_id,
            turn_id="unsafe-bucket",
            tool_use_id="tool-unsafe-bucket",
        )
        assert receipt.status is MutationAttemptStatus.AMBIGUOUS
        assert receipt.failure_code == "restart_candidate_cleanup_failed"
        assert store.path_for(ref).read_bytes() == payload
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["journaled", "published", "committed"])
async def test_subprocess_hard_crash_reconciles_candidate_journal(
    tmp_path: Path,
    phase: str,
) -> None:
    database = tmp_path / "sessions.db"
    media_root = tmp_path / "media"
    ready = tmp_path / "worker-ready.json"
    turn_id = f"hard-crash-{phase}"
    service = await ArtifactSessionService.open(database)
    created = await _created(service)
    await _reserve(
        service,
        document_id=created.document.document_id,
        revision_id=created.revision.revision_id,
        turn_id=turn_id,
    )
    document_id = created.document.document_id
    await service.close()

    repository_root = Path(__file__).resolve().parents[2]
    worker = repository_root / "tests/helpers/artifact_candidate_crash_worker.py"
    process = subprocess.Popen(
        [
            sys.executable,
            str(worker),
            "--database",
            str(database),
            "--media-root",
            str(media_root),
            "--ready",
            str(ready),
            "--document-id",
            document_id,
            "--turn-id",
            turn_id,
            "--session-id",
            SESSION_ID,
            "--phase",
            phase,
        ],
        cwd=repository_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 10
    while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    if not ready.exists():
        process.kill()
        stdout, stderr = process.communicate(timeout=5)
        pytest.fail(
            f"synthetic crash worker did not reach {phase}: {stdout}\n{stderr}"
        )
    worker_result = json.loads(ready.read_text(encoding="utf-8"))
    process.kill()
    process.communicate(timeout=5)

    recovered = await ArtifactSessionService.open(database)
    store = ArtifactStore(media_root)
    try:
        summary = await reconcile_pending_artifact_mutations(recovered, store)
        receipt = await recovered.reconcile_mutation_attempt(
            document_id=document_id,
            turn_id=turn_id,
            tool_use_id=f"tool-{turn_id}",
        )
        if phase == "committed":
            assert summary.applied == 1
            assert summary.deleted_candidates == 0
            assert receipt.status is MutationAttemptStatus.APPLIED
            assert receipt.change_set_id == worker_result["change_set_id"]
            assert receipt.revision_id == worker_result["revision_id"]
            store.resolve_for_download(
                worker_result["artifact_id"],
                session_id=SESSION_ID,
            )
            assert len(await recovered.list_revisions(document_id)) == 2
            assert len(await recovered.list_change_sets(document_id)) == 1
        else:
            assert summary.failed == 1
            assert receipt.status is MutationAttemptStatus.FAILED
            with pytest.raises(ArtifactNotFoundError):
                store.resolve_for_download(
                    worker_result["artifact_id"],
                    session_id=SESSION_ID,
                )
            assert len(await recovered.list_revisions(document_id)) == 1
            assert await recovered.list_change_sets(document_id) == ()
    finally:
        await recovered.close()
