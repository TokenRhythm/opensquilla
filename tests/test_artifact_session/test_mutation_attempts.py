"""Idempotency and recovery contracts for durable artifact mutation attempts."""

from __future__ import annotations

from pathlib import Path

import pytest

from opensquilla.artifact_session import (
    Actor,
    ActorKind,
    ArtifactBlobRef,
    ArtifactConflictError,
    ArtifactKind,
    ArtifactMutationAttemptController,
    ArtifactNotFoundError,
    ArtifactSessionService,
    ArtifactValidationError,
    MutationAttemptStatus,
    document_mutation_outcome_from_attempt,
)

USER = Actor(ActorKind.USER, "user-1")
AGENT = Actor(ActorKind.AGENT, "agent-1")
PROPOSAL_SHA256 = "d" * 64


def test_conflict_receipt_projects_the_same_authoritative_outcome_after_reload() -> None:
    from opensquilla.artifact_session import MutationAttempt

    attempt = MutationAttempt(
        mutation_attempt_id="attempt-conflict",
        document_id="document-conflict",
        turn_id="turn-conflict",
        tool_use_id="tool-conflict",
        base_revision_id="revision-base",
        proposal_sha256=PROPOSAL_SHA256,
        status=MutationAttemptStatus.FAILED,
        change_set_id=None,
        revision_id=None,
        failure_code="DOCUMENT_MUTATION_CONFLICT",
        state_revision=2,
        created_at=1,
        updated_at=2,
    )

    outcome = document_mutation_outcome_from_attempt(attempt)

    assert outcome["status"] == "conflict"
    assert outcome["retryPolicy"] == "refresh"
    assert outcome["code"] == "DOCUMENT_MUTATION_CONFLICT"


def _blob(label: str) -> ArtifactBlobRef:
    digest = label.encode().hex() or "00"
    return ArtifactBlobRef(
        artifact_id=f"artifact-{label}",
        sha256=(digest * 64)[:64],
        filename="page.html",
        media_type="text/html",
        byte_size=len(label),
    )


async def _document(service: ArtifactSessionService, *, label: str = "base"):
    return await service.create_document(
        session_key="agent:main:webchat:mutation",
        session_id="session-mutation",
        name=f"Page {label}",
        kind=ArtifactKind.HTML,
        initial_artifact=_blob(label),
        actor=USER,
    )


@pytest.mark.asyncio
async def test_mutation_reservation_reconciles_same_tool_and_fences_another(
    tmp_path: Path,
) -> None:
    service = await ArtifactSessionService.open(tmp_path / "artifacts.db")
    try:
        created = await _document(service)
        args = {
            "document_id": created.document.document_id,
            "turn_id": "turn-1",
            "tool_use_id": "tool-1",
            "base_revision_id": created.revision.revision_id,
            "proposal_sha256": PROPOSAL_SHA256,
        }
        first = await service.reserve_mutation_attempt(**args)
        replay = await service.reserve_mutation_attempt(**args)
        reconciled = await service.reconcile_mutation_attempt(
            document_id=created.document.document_id,
            turn_id="turn-1",
            tool_use_id="tool-1",
        )

        assert first == replay == reconciled
        assert first.status is MutationAttemptStatus.RESERVED
        with pytest.raises(ArtifactConflictError, match="different mutation tool call"):
            await service.reserve_mutation_attempt(**{**args, "proposal_sha256": "e" * 64})
        with pytest.raises(ArtifactConflictError, match="different mutation tool call"):
            await service.reserve_mutation_attempt(**{**args, "tool_use_id": "tool-2"})
        with pytest.raises(ArtifactConflictError, match="different tool_use_id"):
            await service.reconcile_mutation_attempt(
                document_id=created.document.document_id,
                turn_id="turn-1",
                tool_use_id="tool-2",
            )
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_mutation_reservation_fences_same_turn_across_documents(tmp_path: Path) -> None:
    service = await ArtifactSessionService.open(tmp_path / "artifacts.db")
    try:
        first_document = await _document(service, label="first")
        second_document = await _document(service, label="second")

        first = await service.reserve_mutation_attempt(
            document_id=first_document.document.document_id,
            turn_id="turn-global-writer",
            tool_use_id="tool-first",
            base_revision_id=first_document.revision.revision_id,
            proposal_sha256=PROPOSAL_SHA256,
        )

        with pytest.raises(
            ArtifactConflictError,
            match="different mutation tool call or document",
        ):
            await service.reserve_mutation_attempt(
                document_id=second_document.document.document_id,
                turn_id="turn-global-writer",
                tool_use_id="tool-second",
                base_revision_id=second_document.revision.revision_id,
                proposal_sha256="e" * 64,
            )

        assert await service.list_unresolved_mutation_attempts() == (first,)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_exact_turn_receipt_read_preserves_order_and_session_scope(
    tmp_path: Path,
) -> None:
    service = await ArtifactSessionService.open(tmp_path / "artifacts.db")
    try:
        local = await _document(service, label="local")
        foreign = await service.create_document(
            session_key="agent:main:webchat:foreign",
            session_id="session-foreign",
            name="Foreign",
            kind=ArtifactKind.HTML,
            initial_artifact=_blob("foreign"),
            actor=USER,
        )
        local_attempt = await service.reserve_mutation_attempt(
            document_id=local.document.document_id,
            turn_id="turn-local",
            tool_use_id="tool-local",
            base_revision_id=local.revision.revision_id,
            proposal_sha256=PROPOSAL_SHA256,
        )
        await service.reserve_mutation_attempt(
            document_id=foreign.document.document_id,
            turn_id="turn-foreign",
            tool_use_id="tool-foreign",
            base_revision_id=foreign.revision.revision_id,
            proposal_sha256="e" * 64,
        )

        receipts = await service.list_mutation_attempts_by_turn_ids(
            session_key=local.document.session_key,
            turn_ids=("turn-foreign", "turn-local", "turn-local", "turn-missing"),
        )

        assert receipts == (local_attempt,)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_pure_proposals_do_not_create_durable_attempts(tmp_path: Path) -> None:
    service = await ArtifactSessionService.open(tmp_path / "artifacts.db")
    try:
        created = await _document(service)
        controller = ArtifactMutationAttemptController(
            service,
            document_id=created.document.document_id,
            base_revision_id=created.revision.revision_id,
            turn_id="turn-pure-proposals",
        )

        first = await controller.observe_intent("proposal-1")
        assert first.attempt_number == 1
        assert await service.list_unresolved_mutation_attempts() == ()
        await controller.reject_proposal("proposal-1")

        second = await controller.observe_intent("proposal-2")
        assert second.attempt_number == 2
        assert await service.list_unresolved_mutation_attempts() == ()
        committed = await controller.reserve_commit("proposal-2", PROPOSAL_SHA256)
        assert committed.created is True
        assert await service.list_unresolved_mutation_attempts() == (committed.attempt,)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_null_proposal_digest_is_recoverable_but_never_replayable(
    tmp_path: Path,
) -> None:
    service = await ArtifactSessionService.open(tmp_path / "artifacts.db")
    try:
        created = await _document(service)
        arguments = {
            "document_id": created.document.document_id,
            "turn_id": "turn-null-proposal",
            "tool_use_id": "tool-null-proposal",
            "base_revision_id": created.revision.revision_id,
            "proposal_sha256": None,
        }
        created_attempt = await service.reserve_mutation_attempt(**arguments)
        assert created_attempt.proposal_sha256 is None
        with pytest.raises(ArtifactConflictError, match="different mutation tool call"):
            await service.reserve_mutation_attempt(**arguments)
        with pytest.raises(ArtifactConflictError, match="different mutation tool call"):
            await service.reserve_mutation_attempt(
                **{**arguments, "proposal_sha256": PROPOSAL_SHA256}
            )
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_mutation_candidate_journal_is_idempotent_and_listed_for_recovery(
    tmp_path: Path,
) -> None:
    service = await ArtifactSessionService.open(tmp_path / "artifacts.db")
    try:
        created = await _document(service)
        await service.reserve_mutation_attempt(
            document_id=created.document.document_id,
            turn_id="turn-journal",
            tool_use_id="tool-journal",
            base_revision_id=created.revision.revision_id,
            proposal_sha256=PROPOSAL_SHA256,
        )
        args = {
            "document_id": created.document.document_id,
            "turn_id": "turn-journal",
            "candidate_session_id": "session-mutation",
            "candidate_artifact_id": "art-candidate-fixed",
            "candidate_artifact_sha256": "b" * 64,
        }

        journaled = await service.register_mutation_candidate(**args)
        replay = await service.register_mutation_candidate(**args)

        assert replay == journaled
        assert journaled.status is MutationAttemptStatus.RESERVED
        assert journaled.candidate_session_id == "session-mutation"
        assert journaled.candidate_artifact_id == "art-candidate-fixed"
        assert journaled.candidate_artifact_sha256 == "b" * 64
        assert journaled.candidate_registered_at is not None
        assert await service.list_unresolved_mutation_attempts() == (journaled,)
        with pytest.raises(ArtifactConflictError, match="already registered"):
            await service.register_mutation_candidate(
                **{**args, "candidate_artifact_id": "art-candidate-other"}
            )
        with pytest.raises(ArtifactValidationError, match="candidate session"):
            await service.reserve_mutation_attempt(
                document_id=created.document.document_id,
                turn_id="turn-wrong-session",
                tool_use_id="tool-wrong-session",
                base_revision_id=created.revision.revision_id,
                proposal_sha256="e" * 64,
            )
            await service.register_mutation_candidate(
                **{
                    **args,
                    "turn_id": "turn-wrong-session",
                    "candidate_session_id": "another-session",
                }
            )
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_applied_receipt_must_match_journaled_candidate(tmp_path: Path) -> None:
    service = await ArtifactSessionService.open(tmp_path / "artifacts.db")
    try:
        created = await _document(service)
        await service.reserve_mutation_attempt(
            document_id=created.document.document_id,
            turn_id="turn-journal-mismatch",
            tool_use_id="tool-journal-mismatch",
            base_revision_id=created.revision.revision_id,
            proposal_sha256=PROPOSAL_SHA256,
        )
        await service.register_mutation_candidate(
            document_id=created.document.document_id,
            turn_id="turn-journal-mismatch",
            candidate_session_id="session-mutation",
            candidate_artifact_id="art-journaled",
            candidate_artifact_sha256="c" * 64,
        )
        applied, change = await service.commit_change_set_atomically(
            document_id=created.document.document_id,
            base_revision_id=created.revision.revision_id,
            expected_document_state_revision=created.document.state_revision,
            operations=({"op": "replace_text"},),
            candidate_artifact=_blob("different"),
            validation={"status": "passed"},
            actor=AGENT,
            turn_id="turn-journal-mismatch",
        )

        with pytest.raises(ArtifactConflictError, match="journaled mutation candidate"):
            await service.mark_mutation_attempt_applied(
                document_id=created.document.document_id,
                turn_id="turn-journal-mismatch",
                tool_use_id="tool-journal-mismatch",
                change_set_id=change.change_set_id,
                revision_id=applied.revision.revision_id,
            )
        assert (
            await service.reconcile_mutation_attempt(
                document_id=created.document.document_id,
                turn_id="turn-journal-mismatch",
                tool_use_id="tool-journal-mismatch",
            )
        ).status is MutationAttemptStatus.RESERVED
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_applied_mutation_receipt_is_idempotent_and_matches_change_result(
    tmp_path: Path,
) -> None:
    service = await ArtifactSessionService.open(tmp_path / "artifacts.db")
    try:
        created = await _document(service)
        await service.reserve_mutation_attempt(
            document_id=created.document.document_id,
            turn_id="turn-applied",
            tool_use_id="tool-applied",
            base_revision_id=created.revision.revision_id,
            proposal_sha256=PROPOSAL_SHA256,
        )
        change = await service.create_change_set(
            document_id=created.document.document_id,
            base_revision_id=created.revision.revision_id,
            operations=({"op": "replace_text"},),
            actor=AGENT,
            turn_id="turn-applied",
        )
        ready = await service.ready_change_set(
            change_set_id=change.change_set_id,
            expected_state_revision=change.state_revision,
            candidate_artifact=_blob("result"),
            actor=AGENT,
        )
        applied = await service.apply_change_set(
            change_set_id=ready.change_set_id,
            expected_change_set_state_revision=ready.state_revision,
            expected_head_revision_id=created.revision.revision_id,
            expected_document_state_revision=created.document.state_revision,
            actor=AGENT,
        )
        receipt = await service.mark_mutation_attempt_applied(
            document_id=created.document.document_id,
            turn_id="turn-applied",
            tool_use_id="tool-applied",
            change_set_id=change.change_set_id,
            revision_id=applied.revision.revision_id,
        )
        replay = await service.mark_mutation_attempt_applied(
            document_id=created.document.document_id,
            turn_id="turn-applied",
            tool_use_id="tool-applied",
            change_set_id=change.change_set_id,
            revision_id=applied.revision.revision_id,
        )

        assert receipt == replay
        assert receipt.status is MutationAttemptStatus.APPLIED
        assert receipt.change_set_id == change.change_set_id
        assert receipt.revision_id == applied.revision.revision_id
        assert receipt.failure_code is None
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_controller_response_loss_reconciles_without_authorizing_reexecution(
    tmp_path: Path,
) -> None:
    service = await ArtifactSessionService.open(tmp_path / "artifacts.db")
    try:
        created = await _document(service)
        controller = ArtifactMutationAttemptController(
            service,
            document_id=created.document.document_id,
            base_revision_id=created.revision.revision_id,
            turn_id="turn-response-loss",
        )
        await controller.observe_intent("tool-response-loss")
        initial = await controller.reserve_commit("tool-response-loss", PROPOSAL_SHA256)
        in_flight_replay = await controller.reserve_commit("tool-response-loss", PROPOSAL_SHA256)
        assert initial.created is True
        assert initial.attempt.status is MutationAttemptStatus.RESERVED
        assert in_flight_replay.created is False
        assert in_flight_replay.attempt.status is MutationAttemptStatus.RESERVED

        change = await service.create_change_set(
            document_id=created.document.document_id,
            base_revision_id=created.revision.revision_id,
            operations=({"op": "replace_text"},),
            actor=AGENT,
            turn_id="turn-response-loss",
        )
        ready = await service.ready_change_set(
            change_set_id=change.change_set_id,
            expected_state_revision=change.state_revision,
            candidate_artifact=_blob("response-was-lost"),
            actor=AGENT,
        )
        applied = await service.apply_change_set(
            change_set_id=ready.change_set_id,
            expected_change_set_state_revision=ready.state_revision,
            expected_head_revision_id=created.revision.revision_id,
            expected_document_state_revision=created.document.state_revision,
            actor=AGENT,
        )

        recovered = await controller.reserve_commit("tool-response-loss", PROPOSAL_SHA256)
        assert recovered.created is False
        assert recovered.attempt.status is MutationAttemptStatus.APPLIED
        assert recovered.attempt.change_set_id == change.change_set_id
        assert recovered.attempt.revision_id == applied.revision.revision_id
        assert len(await service.list_revisions(created.document.document_id)) == 2

        with pytest.raises(ArtifactConflictError, match="commit boundary"):
            await controller.observe_intent("different-tool")
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_controller_recovers_reservation_when_commit_response_is_lost(
    tmp_path: Path,
) -> None:
    service = await ArtifactSessionService.open(tmp_path / "artifacts.db")

    class _ReserveResponseLossService:
        def __init__(self, delegate: ArtifactSessionService) -> None:
            self.delegate = delegate
            self.raise_after_commit = True

        def __getattr__(self, name: str):
            return getattr(self.delegate, name)

        async def reserve_mutation_attempt_with_status(self, **kwargs):
            result = await self.delegate.reserve_mutation_attempt_with_status(**kwargs)
            if self.raise_after_commit:
                self.raise_after_commit = False
                raise RuntimeError("synthetic response loss after durable reserve")
            return result

    try:
        created = await _document(service)
        lossy_service = _ReserveResponseLossService(service)
        controller = ArtifactMutationAttemptController(
            lossy_service,  # type: ignore[arg-type]
            document_id=created.document.document_id,
            base_revision_id=created.revision.revision_id,
            turn_id="turn-reserve-response-loss",
        )
        await controller.observe_intent("tool-reserve-response-loss")

        recovered = await controller.reserve_commit(
            "tool-reserve-response-loss",
            PROPOSAL_SHA256,
        )
        replay = await controller.reserve_commit(
            "tool-reserve-response-loss",
            PROPOSAL_SHA256,
        )

        assert recovered.created is True
        assert recovered.attempt.status is MutationAttemptStatus.RESERVED
        assert controller.owns_commit("tool-reserve-response-loss") is True
        assert replay.created is False
        assert replay.attempt == recovered.attempt
        assert await service.list_unresolved_mutation_attempts() == (recovered.attempt,)
        with pytest.raises(ArtifactConflictError, match="commit boundary"):
            await controller.observe_intent("tool-second-writer")
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_controller_tool_start_reservation_is_claimed_exactly_once_by_dispatch(
    tmp_path: Path,
) -> None:
    service = await ArtifactSessionService.open(tmp_path / "artifacts.db")
    try:
        created = await _document(service)
        controller = ArtifactMutationAttemptController(
            service,
            document_id=created.document.document_id,
            base_revision_id=created.revision.revision_id,
            turn_id="turn-stream-intent",
        )

        intent = await controller.observe_intent("writer-streamed")
        dispatch_claim = await controller.reserve_commit("writer-streamed", PROPOSAL_SHA256)
        replay = await controller.reserve_commit("writer-streamed", PROPOSAL_SHA256)

        assert intent.created is True
        assert dispatch_claim.created is True
        assert dispatch_claim.attempt.status is MutationAttemptStatus.RESERVED
        assert replay.created is False
        assert replay.attempt.status is MutationAttemptStatus.RESERVED
        with pytest.raises(ArtifactConflictError, match="commit boundary"):
            await controller.observe_intent("writer-second")
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_controller_replay_requires_the_same_proposal_digest_after_restart(
    tmp_path: Path,
) -> None:
    service = await ArtifactSessionService.open(tmp_path / "artifacts.db")
    try:
        created = await _document(service)
        first = ArtifactMutationAttemptController(
            service,
            document_id=created.document.document_id,
            base_revision_id=created.revision.revision_id,
            turn_id="turn-replay-digest",
        )
        await first.observe_intent("writer-replay")
        reserved = await first.reserve_commit("writer-replay", PROPOSAL_SHA256)
        assert reserved.created is True

        restarted = ArtifactMutationAttemptController(
            service,
            document_id=created.document.document_id,
            base_revision_id=created.revision.revision_id,
            turn_id="turn-replay-digest",
        )
        replay = await restarted.replay_commit("writer-replay", PROPOSAL_SHA256)
        assert replay is not None
        assert replay.mutation_attempt_id == reserved.attempt.mutation_attempt_id

        mismatched = ArtifactMutationAttemptController(
            service,
            document_id=created.document.document_id,
            base_revision_id=created.revision.revision_id,
            turn_id="turn-replay-digest",
        )
        with pytest.raises(ArtifactConflictError, match="does not match"):
            await mismatched.replay_commit("writer-replay", "e" * 64)
        assert mismatched.owns_commit("writer-replay") is False
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_failed_and_ambiguous_receipts_roll_back_faults_and_bound_codes(
    tmp_path: Path,
) -> None:
    service = await ArtifactSessionService.open(tmp_path / "artifacts.db")
    try:
        created = await _document(service)
        common = {
            "document_id": created.document.document_id,
            "turn_id": "turn-fault",
            "tool_use_id": "tool-fault",
        }
        await service.reserve_mutation_attempt(
            **common,
            base_revision_id=created.revision.revision_id,
            proposal_sha256=PROPOSAL_SHA256,
            mutation_attempt_id="attempt-fixed",
        )

        with pytest.raises(ArtifactNotFoundError, match="change set"):
            await service.mark_mutation_attempt_failed(
                **common,
                failure_code="change_set_missing",
                change_set_id="missing-change",
            )
        still_reserved = await service.reconcile_mutation_attempt(**common)
        assert still_reserved.status is MutationAttemptStatus.RESERVED

        with pytest.raises(ArtifactValidationError, match="bounded machine-readable"):
            await service.mark_mutation_attempt_failed(
                **common,
                failure_code="not a machine code",
            )
        with pytest.raises(ArtifactValidationError, match="bounded machine-readable"):
            await service.mark_mutation_attempt_failed(
                **common,
                failure_code="x" * 129,
            )

        ambiguous = await service.mark_mutation_attempt_ambiguous(
            **common,
            failure_code="commit_outcome_unknown",
        )
        assert ambiguous.status is MutationAttemptStatus.AMBIGUOUS
        assert (
            await service.mark_mutation_attempt_ambiguous(
                **common,
                failure_code="commit_outcome_unknown",
            )
            == ambiguous
        )

        failed = await service.mark_mutation_attempt_failed(
            **common,
            failure_code="commit_not_applied",
        )
        assert failed.status is MutationAttemptStatus.FAILED
        assert failed.state_revision == ambiguous.state_revision + 1

        with pytest.raises(ArtifactConflictError, match="already terminal"):
            await service.mark_mutation_attempt_ambiguous(
                **common,
                failure_code="late_unknown",
            )

        with pytest.raises(ArtifactConflictError, match="already in use"):
            await service.reserve_mutation_attempt(
                document_id=created.document.document_id,
                turn_id="turn-other",
                tool_use_id="tool-other",
                base_revision_id=created.revision.revision_id,
                proposal_sha256="e" * 64,
                mutation_attempt_id="attempt-fixed",
            )
        with pytest.raises(ArtifactNotFoundError):
            await service.reconcile_mutation_attempt(
                document_id=created.document.document_id,
                turn_id="turn-other",
                tool_use_id="tool-other",
            )
    finally:
        await service.close()
