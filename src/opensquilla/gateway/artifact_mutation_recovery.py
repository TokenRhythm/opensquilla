"""Restart reconciliation for journaled artifact mutation candidates."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from opensquilla.artifact_session import (
    ArtifactNotFoundError as ArtifactSessionNotFoundError,
)
from opensquilla.artifact_session import (
    ArtifactSessionService,
    ChangeSetStatus,
    MutationAttempt,
    MutationAttemptStatus,
)
from opensquilla.artifacts import ArtifactError, ArtifactStore


@dataclass(frozen=True, slots=True)
class ArtifactMutationRecoverySummary:
    examined: int = 0
    applied: int = 0
    failed: int = 0
    ambiguous: int = 0
    deleted_candidates: int = 0


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


__all__ = [
    "ArtifactMutationRecoverySummary",
    "reconcile_pending_artifact_mutations",
]
