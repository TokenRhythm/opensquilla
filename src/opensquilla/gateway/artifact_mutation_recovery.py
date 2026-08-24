"""Restart reconciliation for journaled artifact mutation candidates."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass

from opensquilla.artifact_session import (
    Actor,
    ActorKind,
    ArtifactSessionService,
    ChangeSet,
    ChangeSetStatus,
    MutationAttempt,
    MutationAttemptStatus,
)
from opensquilla.artifact_session import (
    ArtifactNotFoundError as ArtifactSessionNotFoundError,
)
from opensquilla.artifacts import ArtifactError, ArtifactNotFoundError, ArtifactStore


@dataclass(frozen=True, slots=True)
class ArtifactMutationRecoverySummary:
    examined: int = 0
    applied: int = 0
    failed: int = 0
    ambiguous: int = 0
    deleted_candidates: int = 0


@dataclass(frozen=True, slots=True)
class ArtifactDraftRecoverySummary:
    """Restart cleanup results for turn-local candidate ChangeSets."""

    examined: int = 0
    rejected: int = 0
    deleted_candidates: int = 0
    ambiguous: int = 0


async def _cleanup_rejected_candidate_journal(
    service: ArtifactSessionService,
    store: ArtifactStore,
    *,
    limit: int,
) -> tuple[int, int]:
    """Retry physical deletion after the reject transaction has committed.

    The audit payload is intentionally the only cleanup journal; no schema
    migration or public artifact state is needed.  Verify the recorded digest
    before deleting so an unexpected artifact-id collision fails closed.
    """

    candidates = await service.list_rejected_candidate_artifacts(limit=limit)
    return await _cleanup_candidate_artifact_records(service, store, candidates)


async def _cleanup_candidate_artifact_records(
    service: ArtifactSessionService,
    store: ArtifactStore,
    candidates: tuple[tuple[str, str, str, str], ...],
) -> tuple[int, int]:
    """Delete verified blobs and durably retire successful/missing journals."""

    deleted = 0
    ambiguous = 0
    actor = Actor(ActorKind.SYSTEM, "restart-recovery")
    for document_id, session_id, artifact_id, sha256 in candidates:
        clean = False
        try:
            ref = await asyncio.to_thread(
                store.get_ref,
                session_id=session_id,
                artifact_id=artifact_id,
            )
        except ArtifactNotFoundError:
            clean = True
            removed = False
        except (ArtifactError, OSError, ValueError):
            ambiguous += 1
            continue
        else:
            if ref.sha256 != sha256:
                ambiguous += 1
                continue
            try:
                removed = await asyncio.to_thread(
                    store.delete_ref,
                    session_id=session_id,
                    artifact_id=artifact_id,
                )
            except (ArtifactError, OSError, ValueError):
                ambiguous += 1
                continue
            clean = removed
            if not removed:
                # A concurrent cleanup may have won after get_ref. Confirm
                # absence before retiring the durable journal entry.
                try:
                    await asyncio.to_thread(
                        store.get_ref,
                        session_id=session_id,
                        artifact_id=artifact_id,
                    )
                except ArtifactNotFoundError:
                    clean = True
                except (ArtifactError, OSError, ValueError):
                    clean = False
        if not clean:
            ambiguous += 1
            continue
        try:
            await service.mark_candidate_artifact_cleaned(
                document_id=document_id,
                artifact_id=artifact_id,
                sha256=sha256,
                actor=actor,
            )
        except Exception:  # noqa: BLE001 - retain journal for retry
            ambiguous += 1
            continue
        deleted += int(removed)
    return deleted, ambiguous


async def _cleanup_applied_candidate_journal(
    service: ArtifactSessionService,
    store: ArtifactStore,
    *,
    limit: int,
) -> tuple[int, int]:
    """Remove superseded candidate blobs after an applied turn commit.

    Candidate replacement normally deletes the previous internal blob eagerly.
    If that deletion fails and the final ``DRAFT -> APPLIED`` transaction then
    succeeds, there is no rejected-draft journal to drive cleanup.  The applied
    ChangeSet's turn id is durable ownership proof; scan only its exact hashed
    candidate source suffix and preserve the artifact referenced by the final
    revision.  Ordinary internal/listed artifacts cannot match this fence.
    """

    records = await service.list_applied_candidate_artifacts(limit=limit)
    candidates: list[tuple[str, str, str, str]] = []
    ambiguous = 0
    checked_heads: dict[tuple[str, str], bool] = {}
    for document_id, session_id, artifact_id, sha256, current_artifact_id in records:
        head_key = (session_id, current_artifact_id)
        head_valid = checked_heads.get(head_key)
        if head_valid is None:
            try:
                current_ref, _current_path = await asyncio.to_thread(
                    store.resolve_for_download,
                    current_artifact_id,
                    session_id=session_id,
                )
            except (ArtifactError, OSError, ValueError):
                head_valid = False
            else:
                head_valid = current_ref.id == current_artifact_id
            checked_heads[head_key] = head_valid
        if not head_valid:
            ambiguous += 1
            continue
        candidates.append((document_id, session_id, artifact_id, sha256))
    deleted, cleanup_ambiguous = await _cleanup_candidate_artifact_records(
        service,
        store,
        tuple(candidates),
    )
    return deleted, ambiguous + cleanup_ambiguous


def _merge(
    summary: ArtifactMutationRecoverySummary,
    *,
    status: MutationAttemptStatus,
    deleted: bool = False,
) -> ArtifactMutationRecoverySummary:
    return ArtifactMutationRecoverySummary(
        examined=summary.examined + 1,
        applied=summary.applied + (status is MutationAttemptStatus.APPLIED),
        failed=summary.failed + (status is MutationAttemptStatus.FAILED),
        ambiguous=summary.ambiguous + (status is MutationAttemptStatus.AMBIGUOUS),
        deleted_candidates=summary.deleted_candidates + deleted,
    )


async def _mark_ambiguous(
    service: ArtifactSessionService,
    attempt: MutationAttempt,
    failure_code: str,
) -> MutationAttempt:
    if attempt.status is MutationAttemptStatus.AMBIGUOUS:
        return attempt
    return await service.mark_mutation_attempt_ambiguous(
        document_id=attempt.document_id,
        turn_id=attempt.turn_id,
        tool_use_id=attempt.tool_use_id,
        failure_code=failure_code,
    )


async def _delete_journaled_candidate(
    store: ArtifactStore,
    attempt: MutationAttempt,
) -> bool:
    session_id = attempt.candidate_session_id
    artifact_id = attempt.candidate_artifact_id
    if session_id is None or artifact_id is None:
        return False
    return await asyncio.to_thread(
        store.delete_reserved_bucket,
        session_id=session_id,
        artifact_id=artifact_id,
    )


async def _reject_candidate_draft_with_retry(
    service: ArtifactSessionService,
    change_set: ChangeSet,
    *,
    actor: Actor,
    reason: str,
) -> tuple[ChangeSet | None, bool]:
    """Reject one candidate DRAFT, converging a lost/CAS-conflict response.

    A restart can observe a stale state revision while the prior process (or
    another recovery worker) has already rejected or committed the row.  Read
    the durable row after a failed CAS, and retry exactly once only while it
    remains a DRAFT.  The repository rejects the DRAFT and fails any
    RESERVED/AMBIGUOUS receipt in one transaction.  ``proof`` is true only
    when a REJECTED row was observed.
    """

    current = change_set
    for _attempt in range(2):
        if current.status is ChangeSetStatus.REJECTED:
            return current, True
        if current.status is not ChangeSetStatus.DRAFT:
            return current, False
        try:
            rejected, _terminal_attempt = (
                await service.reject_candidate_draft_and_fail_attempt_for_recovery(
                    change_set_id=current.change_set_id,
                    expected_state_revision=current.state_revision,
                    actor=actor,
                    reason=reason,
                    failure_code="process_restarted_before_commit",
                )
            )
        except Exception:
            # The response may have been lost after the CAS.  Reload before
            # classifying the outcome; never infer rejection from an error.
            try:
                latest = await asyncio.shield(
                    service.get_change_set(current.change_set_id)
                )
            except Exception:
                return None, False
            if latest.status is ChangeSetStatus.REJECTED:
                return latest, True
            if latest.status is not ChangeSetStatus.DRAFT:
                return latest, False
            current = latest
            continue
        return rejected, rejected.status is ChangeSetStatus.REJECTED
    return current, current.status is ChangeSetStatus.REJECTED


def _verify_candidate_bucket(
    store: ArtifactStore,
    *,
    session_id: str,
    artifact_id: str,
    sha256: str,
) -> None:
    ref = store.get_ref(session_id=session_id, artifact_id=artifact_id)
    resolved, _path = store.resolve_for_download(artifact_id, session_id=session_id)
    if ref.id != artifact_id or resolved.id != artifact_id:
        raise ArtifactError("journaled artifact id does not match stored metadata")
    if ref.sha256 != sha256 or resolved.sha256 != sha256:
        raise ArtifactError("journaled artifact hash does not match stored material")


async def _reconcile_one(
    service: ArtifactSessionService,
    store: ArtifactStore,
    attempt: MutationAttempt,
) -> tuple[MutationAttemptStatus, bool]:
    change_set = await service.get_change_set_by_turn(
        document_id=attempt.document_id,
        turn_id=attempt.turn_id,
    )
    if change_set is None:
        try:
            deleted = await _delete_journaled_candidate(store, attempt)
        except (ArtifactError, OSError, ValueError):
            terminal = await _mark_ambiguous(
                service,
                attempt,
                "restart_candidate_cleanup_failed",
            )
            return terminal.status, False
        terminal = await service.mark_mutation_attempt_failed(
            document_id=attempt.document_id,
            turn_id=attempt.turn_id,
            tool_use_id=attempt.tool_use_id,
            failure_code=(
                "process_restarted_before_candidate"
                if attempt.candidate_artifact_id is None
                else "process_restarted_before_commit"
            ),
        )
        return terminal.status, deleted

    # A candidate-loop finish journals its mutation attempt before the final
    # DRAFT -> APPLIED CAS.  If the process died in that window, reject the
    # draft only after proving that this row is explicitly marked as a
    # candidate loop.  Ordinary agent/collaboration DRAFTs must remain
    # untouched, and an already-APPLIED receipt must never be downgraded.
    if change_set.status is ChangeSetStatus.DRAFT:
        if attempt.status is MutationAttemptStatus.APPLIED:
            return attempt.status, False
        try:
            is_candidate_loop = await service.is_candidate_loop_change_set(
                change_set.change_set_id
            )
        except Exception:
            terminal = await _mark_ambiguous(
                service,
                attempt,
                "restart_candidate_marker_unavailable",
            )
            return terminal.status, False
        if not is_candidate_loop:
            terminal = await _mark_ambiguous(
                service,
                attempt,
                "restart_unmarked_draft_preserved",
            )
            return terminal.status, False
        rejected, rejection_proven = await _reject_candidate_draft_with_retry(
            service,
            change_set,
            actor=Actor(ActorKind.SYSTEM, "restart-recovery"),
            reason="process_restarted_before_document_finish",
        )
        if not rejection_proven or rejected is None:
            terminal = await _mark_ambiguous(
                service,
                attempt,
                "restart_candidate_draft_rejection_unresolved",
            )
            return terminal.status, False
        change_set = rejected

    revision = None
    if change_set.applied_revision_id is not None:
        try:
            revision = await service.get_revision(change_set.applied_revision_id)
        except ArtifactSessionNotFoundError:
            revision = None

    candidate_id = attempt.candidate_artifact_id or change_set.candidate_artifact_id
    candidate_sha = (
        attempt.candidate_artifact_sha256 or change_set.candidate_artifact_sha256
    )
    candidate_session_id = attempt.candidate_session_id
    if candidate_session_id is None:
        candidate_session_id = (await service.get_document(attempt.document_id)).session_id
    applied_matches = (
        change_set.status is ChangeSetStatus.APPLIED
        and revision is not None
        and candidate_id is not None
        and candidate_sha is not None
        and change_set.base_revision_id == attempt.base_revision_id
        and change_set.candidate_artifact_id == candidate_id
        and change_set.candidate_artifact_sha256 == candidate_sha
        and revision.change_set_id == change_set.change_set_id
        and revision.artifact_id == candidate_id
        and revision.artifact_sha256 == candidate_sha
        and candidate_session_id is not None
    )
    if applied_matches:
        assert revision is not None
        assert candidate_id is not None
        assert candidate_sha is not None
        assert candidate_session_id is not None
        try:
            await asyncio.to_thread(
                _verify_candidate_bucket,
                store,
                session_id=candidate_session_id,
                artifact_id=candidate_id,
                sha256=candidate_sha,
            )
        except (ArtifactError, OSError, ValueError):
            terminal = await _mark_ambiguous(
                service,
                attempt,
                "restart_applied_candidate_invalid",
            )
            return terminal.status, False
        terminal = await service.mark_mutation_attempt_applied(
            document_id=attempt.document_id,
            turn_id=attempt.turn_id,
            tool_use_id=attempt.tool_use_id,
            change_set_id=change_set.change_set_id,
            revision_id=revision.revision_id,
        )
        return terminal.status, False

    # A rejected DRAFT is an explicit no-revision outcome.  This branch is
    # intentionally handled before the conservative mismatch->AMBIGUOUS
    # fallback so a transient failure in the draft-cleanup pass can be retried
    # on the next boot without permanently misreporting an unresolved commit.
    if (
        change_set.status is ChangeSetStatus.REJECTED
        and change_set.applied_revision_id is None
    ):
        try:
            deleted = await _delete_journaled_candidate(store, attempt)
        except (ArtifactError, OSError, ValueError):
            deleted = False
        try:
            terminal = await service.mark_mutation_attempt_failed(
                document_id=attempt.document_id,
                turn_id=attempt.turn_id,
                tool_use_id=attempt.tool_use_id,
                failure_code="process_restarted_before_commit",
                change_set_id=change_set.change_set_id,
            )
        except Exception:  # noqa: BLE001 - retain a retryable ambiguous receipt
            terminal = await _mark_ambiguous(
                service,
                attempt,
                "restart_rejected_receipt_terminalization_failed",
            )
        return terminal.status, deleted

    try:
        deleted = await _delete_journaled_candidate(store, attempt)
    except (ArtifactError, OSError, ValueError):
        deleted = False
    terminal = await _mark_ambiguous(
        service,
        attempt,
        "restart_persistent_result_mismatch",
    )
    return terminal.status, deleted


async def reconcile_pending_artifact_mutations(
    service: ArtifactSessionService,
    store: ArtifactStore,
    *,
    batch_size: int = 100,
) -> ArtifactMutationRecoverySummary:
    """Terminalize every mutation receipt left unresolved by a prior process."""

    summary = ArtifactMutationRecoverySummary()
    after: str | None = None
    while True:
        attempts = await service.list_unresolved_mutation_attempts(
            limit=batch_size,
            after_mutation_attempt_id=after,
        )
        if not attempts:
            return summary
        for attempt in attempts:
            status, deleted = await _reconcile_one(service, store, attempt)
            summary = _merge(summary, status=status, deleted=deleted)
        after = attempts[-1].mutation_attempt_id


async def reject_orphaned_artifact_drafts(
    service: ArtifactSessionService,
    store: ArtifactStore,
    *,
    batch_size: int = 100,
) -> ArtifactDraftRecoverySummary:
    """Reject candidate drafts and clean superseded blobs after restart.

    No turn-local controller or protocol-v4 preview handle survives a process
    restart, so resuming a DRAFT would be unsafe.  The durable reject CAS runs
    before normal traffic; physical blob deletion remains best effort and is
    safe to retry through the store's orphan cleanup.
    """

    summary = ArtifactDraftRecoverySummary()
    actor = Actor(ActorKind.SYSTEM, "restart-recovery")
    while True:
        drafts = await service.list_draft_change_sets(
            limit=batch_size,
            candidate_only=True,
        )
        if not drafts:
            deleted, ambiguous = await _cleanup_rejected_candidate_journal(
                service,
                store,
                limit=batch_size * 5,
            )
            applied_deleted, applied_ambiguous = await _cleanup_applied_candidate_journal(
                service,
                store,
                limit=batch_size * 5,
            )
            return ArtifactDraftRecoverySummary(
                examined=summary.examined,
                rejected=summary.rejected,
                deleted_candidates=summary.deleted_candidates + deleted + applied_deleted,
                ambiguous=summary.ambiguous + ambiguous + applied_ambiguous,
            )
        progressed = False
        for draft in drafts:
            summary = ArtifactDraftRecoverySummary(
                examined=summary.examined + 1,
                rejected=summary.rejected,
                deleted_candidates=summary.deleted_candidates,
                ambiguous=summary.ambiguous,
            )
            try:
                document = await service.get_document(draft.document_id)
            except Exception:  # noqa: BLE001 - leave a conflicting row for next boot
                summary = ArtifactDraftRecoverySummary(
                    examined=summary.examined,
                    rejected=summary.rejected,
                    deleted_candidates=summary.deleted_candidates,
                    ambiguous=summary.ambiguous + 1,
                )
                continue
            rejected, rejection_proven = await _reject_candidate_draft_with_retry(
                service,
                draft,
                actor=actor,
                reason="process_restarted_before_document_finish",
            )
            if not rejection_proven or rejected is None:
                summary = ArtifactDraftRecoverySummary(
                    examined=summary.examined,
                    rejected=summary.rejected,
                    deleted_candidates=summary.deleted_candidates,
                    ambiguous=summary.ambiguous + 1,
                )
                continue
            candidate = rejected.candidate_artifact or draft.candidate_artifact
            # The atomic reject helper normally closes RESERVED/AMBIGUOUS in
            # the same transaction.  Re-read and idempotently terminalize here
            # for upgrade compatibility with legacy REJECTED + unresolved
            # rows, and for a response lost after an older split transaction.
            # APPLIED is never downgraded.
            try:
                attempt = await service.get_mutation_attempt_for_resolution(
                    document_id=draft.document_id,
                    turn_id=draft.turn_id or "",
                )
            except ArtifactSessionNotFoundError:
                attempt = None
            if attempt is not None and attempt.status in {
                MutationAttemptStatus.RESERVED,
                MutationAttemptStatus.AMBIGUOUS,
            }:
                try:
                    await service.mark_mutation_attempt_failed(
                        document_id=attempt.document_id,
                        turn_id=attempt.turn_id,
                        tool_use_id=attempt.tool_use_id,
                        failure_code="process_restarted_before_commit",
                        change_set_id=draft.change_set_id,
                    )
                except Exception:  # noqa: BLE001 - leave receipt for next boot
                    summary = ArtifactDraftRecoverySummary(
                        examined=summary.examined,
                        rejected=summary.rejected,
                        deleted_candidates=summary.deleted_candidates,
                        ambiguous=summary.ambiguous + 1,
                    )
            deleted = False
            if candidate is not None and document.session_id is not None:
                try:
                    deleted = await asyncio.to_thread(
                        store.delete_ref,
                        session_id=document.session_id,
                        artifact_id=candidate.artifact_id,
                    )
                except (ArtifactError, OSError, ValueError):
                    deleted = False
            # A candidate writer publishes its hidden blob before the draft
            # CAS.  If the process dies in that narrow window, the draft has
            # no candidate columns/audit event to identify the blob.  New
            # writers include a hash of the turn id in the source marker, so a
            # bounded, internal-only scan can safely remove every blob owned
            # by this now-rejected turn (including superseded candidates).
            if document.session_id is not None and draft.turn_id:
                turn_digest = hashlib.sha256(
                    draft.turn_id.encode("utf-8")
                ).hexdigest()
                scan_ambiguous = False
                try:
                    orphaned = await asyncio.to_thread(
                        store.list_internal_refs,
                        document.session_id,
                        source_suffix=f"_agent_candidate:{turn_digest}",
                        limit=100,
                    )
                except (ArtifactError, OSError, ValueError):
                    orphaned = ()
                    scan_ambiguous = True
                orphaned = tuple(
                    ref for ref in orphaned if ref.source.startswith("document_")
                )
                deleted_count = int(deleted)
                for orphan in orphaned:
                    try:
                        removed = await asyncio.to_thread(
                            store.delete_ref,
                            session_id=document.session_id,
                            artifact_id=orphan.id,
                        )
                    except (ArtifactError, OSError, ValueError):
                        removed = False
                    deleted_count += int(removed)
            else:
                deleted_count = int(deleted)
                scan_ambiguous = False
            summary = ArtifactDraftRecoverySummary(
                examined=summary.examined,
                rejected=summary.rejected + 1,
                deleted_candidates=summary.deleted_candidates + deleted_count,
                ambiguous=summary.ambiguous + int(scan_ambiguous),
            )
            progressed = True
        if not progressed:
            deleted, ambiguous = await _cleanup_rejected_candidate_journal(
                service,
                store,
                limit=batch_size * 5,
            )
            applied_deleted, applied_ambiguous = await _cleanup_applied_candidate_journal(
                service,
                store,
                limit=batch_size * 5,
            )
            return ArtifactDraftRecoverySummary(
                examined=summary.examined,
                rejected=summary.rejected,
                deleted_candidates=summary.deleted_candidates + deleted + applied_deleted,
                ambiguous=summary.ambiguous + ambiguous + applied_ambiguous,
            )


__all__ = [
    "ArtifactDraftRecoverySummary",
    "ArtifactMutationRecoverySummary",
    "reject_orphaned_artifact_drafts",
    "reconcile_pending_artifact_mutations",
]
