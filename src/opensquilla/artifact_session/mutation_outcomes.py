"""Authoritative public outcomes derived from durable mutation receipts."""

from __future__ import annotations

from typing import Any

from .models import MutationAttempt, MutationAttemptStatus


def document_mutation_outcome_from_attempt(
    attempt: MutationAttempt,
) -> dict[str, Any]:
    """Project one durable receipt into the public version-1 outcome shape.

    The mutation ledger is the final source of truth for whether the effect
    crossed the commit boundary.  A residual ``RESERVED`` row means the
    durable result still needs reconciliation; it must never be presented as
    either a successful edit or a known non-application.
    """

    if attempt.status is MutationAttemptStatus.APPLIED:
        status = "applied"
        retry_policy = "never"
        code = "document_mutation_applied"
    elif attempt.status is MutationAttemptStatus.FAILED:
        code = attempt.failure_code or "document_mutation_not_applied"
        if code == "DOCUMENT_MUTATION_CONFLICT" or "conflict" in code.lower():
            status = "conflict"
            retry_policy = "refresh"
        else:
            status = "not_applied"
            retry_policy = "new_turn"
    elif attempt.status is MutationAttemptStatus.AMBIGUOUS:
        status = "ambiguous"
        retry_policy = "reconcile"
        code = attempt.failure_code or "document_mutation_outcome_unknown"
    else:
        status = "ambiguous"
        retry_policy = "reconcile"
        code = "document_mutation_reconciliation_pending"

    outcome: dict[str, Any] = {
        "version": 1,
        "status": status,
        "phase": "commit",
        "retryPolicy": retry_policy,
        "code": code,
        "attemptId": attempt.mutation_attempt_id,
        "documentId": attempt.document_id,
        "baseRevisionId": attempt.base_revision_id,
        "stateRevision": attempt.state_revision,
    }
    if attempt.change_set_id is not None:
        outcome["changeSetId"] = attempt.change_set_id
    if attempt.revision_id is not None:
        outcome["resultRevisionId"] = attempt.revision_id
    return outcome


__all__ = ["document_mutation_outcome_from_attempt"]
