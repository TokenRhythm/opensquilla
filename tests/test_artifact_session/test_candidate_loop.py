"""Candidate preview loop persistence and one-shot commit contracts."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from opensquilla.artifact_session import (
    Actor,
    ActorKind,
    ArtifactBlobRef,
    ArtifactConflictError,
    ArtifactKind,
    ArtifactNotFoundError,
    ArtifactSessionService,
    ArtifactValidationError,
    ChangeSetStatus,
)

USER = Actor(ActorKind.USER, "reviewer")
AGENT = Actor(ActorKind.AGENT, "agent")


def _blob(label: str) -> ArtifactBlobRef:
    digest = (label.encode().hex() * 64)[:64]
    return ArtifactBlobRef(
        artifact_id=f"artifact-{label}",
        sha256=digest,
        filename="page.html",
        media_type="text/html",
        byte_size=len(label),
    )


async def _document(service: ArtifactSessionService):
    return await service.create_document(
        session_key="agent:main:webchat:candidate-loop",
        session_id="candidate-session",
        name="Candidate page",
        kind=ArtifactKind.HTML,
        initial_artifact=_blob("base"),
        actor=USER,
    )


@pytest.mark.asyncio
async def test_candidate_controller_never_adopts_ordinary_turn_draft(tmp_path: Path) -> None:
    """A same-turn review draft is not writable by the candidate loop."""

    service = await ArtifactSessionService.open(tmp_path / "ordinary-draft.db")
    try:
        created = await _document(service)
        ordinary = await service.create_change_set(
            document_id=created.document.document_id,
            base_revision_id=created.revision.revision_id,
            operations=({"op": "review", "text": "keep"},),
            actor=AGENT,
            turn_id="turn-collision",
        )
        from opensquilla.artifact_session import ArtifactCandidateLoopController

        controller = ArtifactCandidateLoopController(
            service,
            document_id=created.document.document_id,
            base_revision_id=created.revision.revision_id,
            turn_id="turn-collision",
        )
        with pytest.raises(ArtifactConflictError, match="another workflow"):
            await controller.stage_candidate(
                candidate_artifact=_blob("must-not-adopt"),
                operations=({"op": "replace", "text": "unsafe"},),
                actor=AGENT,
            )
        unchanged = await service.get_change_set(ordinary.change_set_id)
        assert unchanged.status is ChangeSetStatus.DRAFT
        assert unchanged.candidate_artifact is None
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_candidate_reservation_refuses_already_rejected_draft(tmp_path: Path) -> None:
    """A finish cannot reserve after the candidate reject transaction wins."""

    service = await ArtifactSessionService.open(tmp_path / "reserve-after-reject.db")
    try:
        created = await _document(service)
        from opensquilla.artifact_session import ArtifactCandidateLoopController

        controller = ArtifactCandidateLoopController(
            service,
            document_id=created.document.document_id,
            base_revision_id=created.revision.revision_id,
            turn_id="turn-reserve-after-reject",
        )
        staged = await controller.stage_candidate(
            candidate_artifact=_blob("reserve-after-reject"),
            operations=({"op": "replace"},),
            actor=AGENT,
        )
        rejected = await service.reject_draft_change_set_and_cleanup(
            change_set_id=staged.change_set_id,
            expected_state_revision=staged.state_revision,
            actor=AGENT,
            reason="turn closed",
            require_no_active_mutation_attempt=True,
        )
        assert rejected.status is ChangeSetStatus.REJECTED

        with pytest.raises(ArtifactConflictError, match="no longer a draft"):
            await service.reserve_mutation_attempt_with_status(
                document_id=created.document.document_id,
                turn_id="turn-reserve-after-reject",
                tool_use_id="finish-after-reject",
                base_revision_id=created.revision.revision_id,
                proposal_sha256=staged.candidate_artifact_sha256,
                candidate_change_set_id=staged.change_set_id,
                expected_candidate_state_revision=staged.state_revision,
            )
        with pytest.raises(ArtifactNotFoundError):
            await service.get_mutation_attempt_for_resolution(
                document_id=created.document.document_id,
                turn_id="turn-reserve-after-reject",
            )
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_candidate_reservation_requires_marker_candidate_and_exact_sha(
    tmp_path: Path,
) -> None:
    """Candidate-only reservation cannot bind an ordinary or incomplete DRAFT."""

    service = await ArtifactSessionService.open(tmp_path / "reserve-identity.db")
    try:
        created = await _document(service)
        ordinary = await service.create_change_set(
            document_id=created.document.document_id,
            base_revision_id=created.revision.revision_id,
            operations=({"op": "ordinary"},),
            actor=AGENT,
            turn_id="turn-ordinary-reserve",
        )
        with pytest.raises(ArtifactConflictError, match="candidate loop"):
            await service.reserve_mutation_attempt_with_status(
                document_id=created.document.document_id,
                turn_id="turn-ordinary-reserve",
                tool_use_id="finish-ordinary",
                base_revision_id=created.revision.revision_id,
                proposal_sha256="a" * 64,
                candidate_change_set_id=ordinary.change_set_id,
                expected_candidate_state_revision=ordinary.state_revision,
            )

        incomplete = await service.create_change_set(
            document_id=created.document.document_id,
            base_revision_id=created.revision.revision_id,
            operations=({"op": "candidate"},),
            actor=AGENT,
            turn_id="turn-incomplete-reserve",
            candidate_loop=True,
        )
        with pytest.raises(ArtifactValidationError, match="complete artifact"):
            await service.reserve_mutation_attempt_with_status(
                document_id=created.document.document_id,
                turn_id="turn-incomplete-reserve",
                tool_use_id="finish-incomplete",
                base_revision_id=created.revision.revision_id,
                proposal_sha256="b" * 64,
                candidate_change_set_id=incomplete.change_set_id,
                expected_candidate_state_revision=incomplete.state_revision,
            )

        staged = await service.update_draft_change_set_candidate(
            change_set_id=incomplete.change_set_id,
            expected_state_revision=incomplete.state_revision,
            candidate_artifact=_blob("reserve-exact-sha"),
            operations=({"op": "candidate"},),
            actor=AGENT,
        )
        with pytest.raises(ArtifactConflictError, match="digest changed"):
            await service.reserve_mutation_attempt_with_status(
                document_id=created.document.document_id,
                turn_id="turn-incomplete-reserve",
                tool_use_id="finish-wrong-sha",
                base_revision_id=created.revision.revision_id,
                proposal_sha256="c" * 64,
                candidate_change_set_id=staged.change_set_id,
                expected_candidate_state_revision=staged.state_revision,
            )
        for turn_id in ("turn-ordinary-reserve", "turn-incomplete-reserve"):
            with pytest.raises(ArtifactNotFoundError):
                await service.get_mutation_attempt_for_resolution(
                    document_id=created.document.document_id,
                    turn_id=turn_id,
                )
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_candidate_reject_refuses_active_finish_receipt(tmp_path: Path) -> None:
    """The reject transaction cannot overtake an existing durable finish."""

    service = await ArtifactSessionService.open(tmp_path / "reject-after-reserve.db")
    try:
        created = await _document(service)
        from opensquilla.artifact_session import ArtifactCandidateLoopController

        controller = ArtifactCandidateLoopController(
            service,
            document_id=created.document.document_id,
            base_revision_id=created.revision.revision_id,
            turn_id="turn-reject-after-reserve",
        )
        staged = await controller.stage_candidate(
            candidate_artifact=_blob("reject-after-reserve"),
            operations=({"op": "replace"},),
            actor=AGENT,
        )
        receipt, created_receipt = await service.reserve_mutation_attempt_with_status(
            document_id=created.document.document_id,
            turn_id="turn-reject-after-reserve",
            tool_use_id="finish-before-reject",
            base_revision_id=created.revision.revision_id,
            proposal_sha256=staged.candidate_artifact_sha256,
            candidate_change_set_id=staged.change_set_id,
            expected_candidate_state_revision=staged.state_revision,
        )
        assert created_receipt is True
        assert receipt.status.value == "reserved"
        receipt = await service.mark_mutation_attempt_ambiguous(
            document_id=created.document.document_id,
            turn_id="turn-reject-after-reserve",
            tool_use_id="finish-before-reject",
            failure_code="finish_response_unavailable",
        )
        assert receipt.status.value == "ambiguous"

        with pytest.raises(ArtifactConflictError, match="requires reconciliation"):
            await service.reject_draft_change_set_and_cleanup(
                change_set_id=staged.change_set_id,
                expected_state_revision=staged.state_revision,
                actor=AGENT,
                reason="turn closed",
                require_no_active_mutation_attempt=True,
            )
        current = await service.get_change_set(staged.change_set_id)
        assert current.status is ChangeSetStatus.DRAFT
        current_receipt = await service.get_mutation_attempt_for_resolution(
            document_id=created.document.document_id,
            turn_id="turn-reject-after-reserve",
        )
        assert current_receipt.status.value == "ambiguous"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_candidate_reserve_and_reject_race_has_one_durable_winner(
    tmp_path: Path,
) -> None:
    """Concurrent reserve/reject cannot leave a rejected candidate with a receipt."""

    database_path = tmp_path / "reserve-reject-race.db"
    service = await ArtifactSessionService.open(database_path)
    contender: ArtifactSessionService | None = None
    try:
        created = await _document(service)
        from opensquilla.artifact_session import ArtifactCandidateLoopController

        controller = ArtifactCandidateLoopController(
            service,
            document_id=created.document.document_id,
            base_revision_id=created.revision.revision_id,
            turn_id="turn-reserve-reject-race",
        )
        staged = await controller.stage_candidate(
            candidate_artifact=_blob("reserve-reject-race"),
            operations=({"op": "replace"},),
            actor=AGENT,
        )
        # Use a second SQLite connection to exercise the cross-process order,
        # not merely the repository's in-process write lock.
        contender = await ArtifactSessionService.open(database_path)
        start = asyncio.Event()

        async def reserve() -> object:
            await start.wait()
            return await service.reserve_mutation_attempt_with_status(
                document_id=created.document.document_id,
                turn_id="turn-reserve-reject-race",
                tool_use_id="finish-race",
                base_revision_id=created.revision.revision_id,
                proposal_sha256=staged.candidate_artifact_sha256,
                candidate_change_set_id=staged.change_set_id,
                expected_candidate_state_revision=staged.state_revision,
            )

        async def reject() -> object:
            await start.wait()
            assert contender is not None
            return await contender.reject_draft_change_set_and_cleanup(
                change_set_id=staged.change_set_id,
                expected_state_revision=staged.state_revision,
                actor=AGENT,
                reason="turn closed",
                require_no_active_mutation_attempt=True,
            )

        reserve_task = asyncio.create_task(reserve())
        reject_task = asyncio.create_task(reject())
        start.set()
        results = await asyncio.gather(reserve_task, reject_task, return_exceptions=True)
        assert sum(not isinstance(result, BaseException) for result in results) == 1
        assert sum(isinstance(result, ArtifactConflictError) for result in results) == 1

        current = await service.get_change_set(staged.change_set_id)
        try:
            current_receipt = await service.get_mutation_attempt_for_resolution(
                document_id=created.document.document_id,
                turn_id="turn-reserve-reject-race",
            )
        except ArtifactNotFoundError:
            current_receipt = None
        if current.status is ChangeSetStatus.DRAFT:
            assert current_receipt is not None
            assert current_receipt.status.value == "reserved"
        else:
            assert current.status is ChangeSetStatus.REJECTED
            assert current_receipt is None
    finally:
        if contender is not None:
            await contender.close()
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("starting_status", ["reserved", "ambiguous"])
async def test_startup_recovery_rejects_and_fails_receipt_atomically(
    tmp_path: Path,
    starting_status: str,
) -> None:
    """Only the startup API closes an unresolved finish while rejecting its DRAFT."""

    service = await ArtifactSessionService.open(
        tmp_path / f"atomic-recovery-{starting_status}.db"
    )
    try:
        created = await _document(service)
        from opensquilla.artifact_session import ArtifactCandidateLoopController

        controller = ArtifactCandidateLoopController(
            service,
            document_id=created.document.document_id,
            base_revision_id=created.revision.revision_id,
            turn_id="turn-atomic-recovery",
        )
        staged = await controller.stage_candidate(
            candidate_artifact=_blob("atomic-recovery"),
            operations=({"op": "replace"},),
            actor=AGENT,
        )
        await service.reserve_mutation_attempt_with_status(
            document_id=created.document.document_id,
            turn_id="turn-atomic-recovery",
            tool_use_id="finish-atomic-recovery",
            base_revision_id=created.revision.revision_id,
            proposal_sha256=staged.candidate_artifact_sha256,
            candidate_change_set_id=staged.change_set_id,
            expected_candidate_state_revision=staged.state_revision,
        )
        if starting_status == "ambiguous":
            await service.mark_mutation_attempt_ambiguous(
                document_id=created.document.document_id,
                turn_id="turn-atomic-recovery",
                tool_use_id="finish-atomic-recovery",
                failure_code="restart_pending",
            )
        rejected, terminal = (
            await service.reject_candidate_draft_and_fail_attempt_for_recovery(
                change_set_id=staged.change_set_id,
                expected_state_revision=staged.state_revision,
                actor=Actor(ActorKind.SYSTEM, "restart-recovery"),
                reason="process restarted",
                failure_code="process_restarted_before_commit",
            )
        )

        assert rejected.status is ChangeSetStatus.REJECTED
        assert rejected.candidate_artifact is None
        assert terminal is not None
        assert terminal.status.value == "failed"
        assert terminal.change_set_id == staged.change_set_id
        assert terminal.failure_code == "process_restarted_before_commit"
        replayed, replay_created = await service.reserve_mutation_attempt_with_status(
            document_id=created.document.document_id,
            turn_id="turn-atomic-recovery",
            tool_use_id="finish-atomic-recovery",
            base_revision_id=created.revision.revision_id,
            proposal_sha256=staged.candidate_artifact_sha256,
            candidate_change_set_id=staged.change_set_id,
            expected_candidate_state_revision=staged.state_revision,
        )
        assert replay_created is False
        assert replayed == terminal
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_startup_recovery_rolls_back_receipt_and_draft_together(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A late transaction failure cannot expose FAILED plus a live DRAFT."""

    service = await ArtifactSessionService.open(tmp_path / "atomic-recovery-rollback.db")
    try:
        created = await _document(service)
        from opensquilla.artifact_session import ArtifactCandidateLoopController

        controller = ArtifactCandidateLoopController(
            service,
            document_id=created.document.document_id,
            base_revision_id=created.revision.revision_id,
            turn_id="turn-atomic-recovery-rollback",
        )
        staged = await controller.stage_candidate(
            candidate_artifact=_blob("atomic-recovery-rollback"),
            operations=({"op": "replace"},),
            actor=AGENT,
        )
        await service.reserve_mutation_attempt_with_status(
            document_id=created.document.document_id,
            turn_id="turn-atomic-recovery-rollback",
            tool_use_id="finish-atomic-recovery-rollback",
            base_revision_id=created.revision.revision_id,
            proposal_sha256=staged.candidate_artifact_sha256,
            candidate_change_set_id=staged.change_set_id,
            expected_candidate_state_revision=staged.state_revision,
        )
        original_append_audit = service.repository._append_audit  # noqa: SLF001

        async def fail_reject_audit(*args: object, **kwargs: object) -> object:
            if kwargs.get("event_type") == "change_set.rejected":
                raise RuntimeError("audit unavailable")
            return await original_append_audit(*args, **kwargs)

        monkeypatch.setattr(service.repository, "_append_audit", fail_reject_audit)
        with pytest.raises(RuntimeError, match="audit unavailable"):
            await service.reject_candidate_draft_and_fail_attempt_for_recovery(
                change_set_id=staged.change_set_id,
                expected_state_revision=staged.state_revision,
                actor=Actor(ActorKind.SYSTEM, "restart-recovery"),
                reason="process restarted",
                failure_code="process_restarted_before_commit",
            )

        current = await service.get_change_set(staged.change_set_id)
        receipt = await service.get_mutation_attempt_for_resolution(
            document_id=created.document.document_id,
            turn_id="turn-atomic-recovery-rollback",
        )
        assert current.status is ChangeSetStatus.DRAFT
        assert receipt.status.value == "reserved"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_startup_recovery_preserves_preexisting_failed_receipt(tmp_path: Path) -> None:
    """Recovery may reject the orphan DRAFT without rewriting terminal failure facts."""

    service = await ArtifactSessionService.open(tmp_path / "atomic-recovery-failed.db")
    try:
        created = await _document(service)
        from opensquilla.artifact_session import ArtifactCandidateLoopController

        controller = ArtifactCandidateLoopController(
            service,
            document_id=created.document.document_id,
            base_revision_id=created.revision.revision_id,
            turn_id="turn-atomic-recovery-failed",
        )
        staged = await controller.stage_candidate(
            candidate_artifact=_blob("atomic-recovery-failed"),
            operations=({"op": "replace"},),
            actor=AGENT,
        )
        await service.reserve_mutation_attempt_with_status(
            document_id=created.document.document_id,
            turn_id="turn-atomic-recovery-failed",
            tool_use_id="finish-atomic-recovery-failed",
            base_revision_id=created.revision.revision_id,
            proposal_sha256=staged.candidate_artifact_sha256,
            candidate_change_set_id=staged.change_set_id,
            expected_candidate_state_revision=staged.state_revision,
        )
        failed = await service.mark_mutation_attempt_failed(
            document_id=created.document.document_id,
            turn_id="turn-atomic-recovery-failed",
            tool_use_id="finish-atomic-recovery-failed",
            failure_code="preexisting_failure",
            change_set_id=staged.change_set_id,
        )
        rejected, terminal = (
            await service.reject_candidate_draft_and_fail_attempt_for_recovery(
                change_set_id=staged.change_set_id,
                expected_state_revision=staged.state_revision,
                actor=Actor(ActorKind.SYSTEM, "restart-recovery"),
                reason="process restarted",
                failure_code="process_restarted_before_commit",
            )
        )

        assert rejected.status is ChangeSetStatus.REJECTED
        assert terminal == failed
        assert terminal is not None
        assert terminal.failure_code == "preexisting_failure"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_candidate_loop_replaces_draft_and_commits_once(tmp_path: Path) -> None:
    service = await ArtifactSessionService.open(tmp_path / "candidate.db")
    try:
        created = await _document(service)
        from opensquilla.artifact_session import ArtifactCandidateLoopController

        controller = ArtifactCandidateLoopController(
            service,
            document_id=created.document.document_id,
            base_revision_id=created.revision.revision_id,
            turn_id="turn-candidate-loop",
        )
        first = await controller.stage_candidate(
            candidate_artifact=_blob("first"),
            operations=({"op": "replace", "text": "first"},),
            validation={"runtime": "pending"},
            actor=AGENT,
        )
        assert first.status is ChangeSetStatus.DRAFT
        assert (await service.get_document(created.document.document_id)).generation == 1
        await controller.record_verification(
            candidate_sha256=first.candidate_artifact_sha256 or "",
            verification_token="receipt-first",
        )

        second = await controller.stage_candidate(
            candidate_artifact=_blob("second"),
            operations=(
                {"op": "replace", "text": "first"},
                {"op": "insert", "text": "second"},
            ),
            validation={"runtime": "passed"},
            actor=AGENT,
        )
        assert second.status is ChangeSetStatus.DRAFT
        assert len(second.operations) == 2
        assert controller.candidate_epoch == 2
        with pytest.raises(ArtifactConflictError, match="stale"):
            await controller.commit(
                actor=AGENT,
                expected_candidate_sha256=first.candidate_artifact_sha256 or "",
                verification_token="receipt-first",
            )

        await controller.record_verification(
            candidate_sha256=second.candidate_artifact_sha256 or "",
            verification_token="receipt-second",
        )
        applied, committed = await controller.commit(
            actor=AGENT,
            expected_candidate_sha256=second.candidate_artifact_sha256 or "",
            verification_token="receipt-second",
        )
        assert committed.status is ChangeSetStatus.APPLIED
        assert committed.applied_revision_id == applied.revision.revision_id
        assert applied.document.generation == 2
        assert len(await service.list_revisions(created.document.document_id)) == 2
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_candidate_writer_replay_reuses_draft_after_response_loss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lost writer response must not publish a second candidate blob."""

    service = await ArtifactSessionService.open(tmp_path / "writer-replay.db")
    try:
        created = await _document(service)
        from opensquilla.artifact_session import ArtifactCandidateLoopController

        controller = ArtifactCandidateLoopController(
            service,
            document_id=created.document.document_id,
            base_revision_id=created.revision.revision_id,
            turn_id="turn-writer-replay",
        )
        original_update = service.update_draft_change_set_candidate

        async def update_then_lose_response(*args: object, **kwargs: object):
            await original_update(*args, **kwargs)
            raise RuntimeError("writer response lost after candidate CAS")

        monkeypatch.setattr(
            service,
            "update_draft_change_set_candidate",
            update_then_lose_response,
        )
        with pytest.raises(RuntimeError, match="response lost"):
            await controller.stage_candidate(
                candidate_artifact=_blob("writer-first"),
                operations=({"op": "replace", "text": "first"},),
                actor=AGENT,
                tool_use_id="writer-1",
                proposal_sha256="a" * 64,
            )
        durable = await service.get_change_set_by_turn(
            document_id=created.document.document_id,
            turn_id="turn-writer-replay",
        )
        assert durable is not None
        assert durable.candidate_artifact_id == "artifact-writer-first"
        state_revision = durable.state_revision

        monkeypatch.setattr(service, "update_draft_change_set_candidate", original_update)
        replay = await controller.stage_candidate(
            candidate_artifact=_blob("writer-second-must-not-win"),
            operations=({"op": "replace", "text": "second"},),
            actor=AGENT,
            tool_use_id="writer-1",
            proposal_sha256="a" * 64,
        )
        assert replay.change_set_id == durable.change_set_id
        assert replay.candidate_artifact_id == "artifact-writer-first"
        assert replay.state_revision == state_revision
        assert controller.candidate_epoch == 1

        with pytest.raises(ArtifactConflictError, match="does not match"):
            await controller.stage_candidate(
                candidate_artifact=_blob("writer-conflicting-replay"),
                operations=({"op": "replace", "text": "conflict"},),
                actor=AGENT,
                tool_use_id="writer-1",
                proposal_sha256="b" * 64,
            )

        restarted = ArtifactCandidateLoopController(
            service,
            document_id=created.document.document_id,
            base_revision_id=created.revision.revision_id,
            turn_id="turn-writer-replay",
        )
        recovered = await restarted.replay_candidate(
            tool_use_id="writer-1",
            proposal_sha256="a" * 64,
        )
        assert recovered is not None
        assert recovered.candidate_artifact_id == "artifact-writer-first"
        assert recovered.state_revision == state_revision
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_candidate_loop_commit_records_final_mutation_receipt_atomically(
    tmp_path: Path,
) -> None:
    service = await ArtifactSessionService.open(tmp_path / "receipt.db")
    try:
        created = await _document(service)
        from opensquilla.artifact_session import ArtifactCandidateLoopController

        controller = ArtifactCandidateLoopController(
            service,
            document_id=created.document.document_id,
            base_revision_id=created.revision.revision_id,
            turn_id="turn-receipt",
        )
        staged = await controller.stage_candidate(
            candidate_artifact=_blob("receipt-candidate"),
            operations=({"op": "replace", "text": "receipt"},),
            actor=AGENT,
        )
        sha256 = staged.candidate_artifact_sha256 or ""
        await controller.record_verification(
            candidate_sha256=sha256,
            verification_token="receipt-token",
        )
        applied, change_set = await controller.commit(
            actor=AGENT,
            expected_candidate_sha256=sha256,
            verification_token="receipt-token",
            tool_use_id="finish-receipt",
        )
        receipt = await service.reconcile_mutation_attempt(
            document_id=created.document.document_id,
            turn_id="turn-receipt",
            tool_use_id="finish-receipt",
        )
        assert receipt.status.value == "applied"
        assert receipt.change_set_id == change_set.change_set_id
        assert receipt.revision_id == applied.revision.revision_id
        with pytest.raises(ArtifactConflictError, match="only a draft"):
            await service.reject_candidate_draft_and_fail_attempt_for_recovery(
                change_set_id=change_set.change_set_id,
                expected_state_revision=change_set.state_revision,
                actor=Actor(ActorKind.SYSTEM, "restart-recovery"),
                reason="late recovery",
                failure_code="process_restarted_before_commit",
            )
        still_applied = await service.get_mutation_attempt_for_resolution(
            document_id=created.document.document_id,
            turn_id="turn-receipt",
        )
        assert still_applied.status.value == "applied"
        assert (await service.get_document(created.document.document_id)).generation == 2
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_candidate_loop_reuses_ambiguous_journaled_candidate_on_retry(
    tmp_path: Path,
) -> None:
    """A lost response must not try to re-register an already journaled blob."""

    service = await ArtifactSessionService.open(tmp_path / "ambiguous.db")
    try:
        created = await _document(service)
        from opensquilla.artifact_session import ArtifactCandidateLoopController

        controller = ArtifactCandidateLoopController(
            service,
            document_id=created.document.document_id,
            base_revision_id=created.revision.revision_id,
            turn_id="turn-ambiguous",
        )
        staged = await controller.stage_candidate(
            candidate_artifact=_blob("ambiguous-candidate"),
            operations=({"op": "replace", "text": "ambiguous"},),
            actor=AGENT,
        )
        sha256 = staged.candidate_artifact_sha256 or ""
        await controller.record_verification(
            candidate_sha256=sha256,
            verification_token="ambiguous-token",
        )

        attempt, _created = await service.reserve_mutation_attempt_with_status(
            document_id=created.document.document_id,
            turn_id="turn-ambiguous",
            tool_use_id="finish-ambiguous",
            base_revision_id=created.revision.revision_id,
            proposal_sha256=sha256,
        )
        attempt = await service.register_mutation_candidate(
            document_id=created.document.document_id,
            turn_id="turn-ambiguous",
            candidate_session_id=created.document.session_id,
            candidate_artifact_id=staged.candidate_artifact_id or "",
            candidate_artifact_sha256=sha256,
        )
        assert attempt.candidate_artifact_id == staged.candidate_artifact_id
        await service.mark_mutation_attempt_ambiguous(
            document_id=created.document.document_id,
            turn_id="turn-ambiguous",
            tool_use_id="finish-ambiguous",
            failure_code="response_lost",
        )

        applied, committed = await controller.commit(
            actor=AGENT,
            expected_candidate_sha256=sha256,
            verification_token="ambiguous-token",
            tool_use_id="finish-ambiguous",
        )
        assert committed.status is ChangeSetStatus.APPLIED
        receipt = await service.reconcile_mutation_attempt(
            document_id=created.document.document_id,
            turn_id="turn-ambiguous",
            tool_use_id="finish-ambiguous",
        )
        assert receipt.status.value == "applied"
        assert receipt.revision_id == applied.revision.revision_id
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_candidate_loop_discard_detaches_candidate_without_revision(tmp_path: Path) -> None:
    service = await ArtifactSessionService.open(tmp_path / "discard.db")
    try:
        created = await _document(service)
        from opensquilla.artifact_session import ArtifactCandidateLoopController

        controller = ArtifactCandidateLoopController(
            service,
            document_id=created.document.document_id,
            base_revision_id=created.revision.revision_id,
            turn_id="turn-discard",
        )
        staged = await controller.stage_candidate(
            candidate_artifact=_blob("discard-me"),
            operations=({"op": "remove", "text": "x"},),
            actor=AGENT,
        )
        rejected = await controller.discard(actor=AGENT, reason="preview failed")
        assert staged.candidate_artifact_id is not None
        assert rejected.status is ChangeSetStatus.REJECTED
        assert rejected.candidate_artifact is None
        assert (await service.get_document(created.document.document_id)).generation == 1
        assert len(await service.list_revisions(created.document.document_id)) == 1
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_empty_candidate_loop_discard_is_idempotent_noop(tmp_path: Path) -> None:
    service = await ArtifactSessionService.open(tmp_path / "empty-discard.db")
    try:
        created = await _document(service)
        from opensquilla.artifact_session import ArtifactCandidateLoopController

        controller = ArtifactCandidateLoopController(
            service,
            document_id=created.document.document_id,
            base_revision_id=created.revision.revision_id,
            turn_id="turn-empty-discard",
        )
        await controller.discard_without_candidate()
        await controller.discard_without_candidate()
        assert controller.state.status == "discarded"
        assert controller.change_set is None
        assert controller.candidate_artifact is None
        document = await service.get_document(created.document.document_id)
        assert document.generation == 1
        assert len(await service.list_revisions(created.document.document_id)) == 1
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_candidate_loop_discard_recovers_reject_response_loss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A committed reject is idempotent when its response is lost."""

    service = await ArtifactSessionService.open(tmp_path / "discard-response-loss.db")
    try:
        created = await _document(service)
        from opensquilla.artifact_session import ArtifactCandidateLoopController

        controller = ArtifactCandidateLoopController(
            service,
            document_id=created.document.document_id,
            base_revision_id=created.revision.revision_id,
            turn_id="turn-discard-response-loss",
        )
        await controller.stage_candidate(
            candidate_artifact=_blob("discard-response-loss"),
            operations=({"op": "replace", "text": "discard"},),
            actor=AGENT,
        )
        original_reject = service.reject_draft_change_set_and_cleanup
        calls = 0

        async def reject_then_lose(*args: object, **kwargs: object):
            nonlocal calls
            calls += 1
            rejected = await original_reject(*args, **kwargs)
            del rejected
            raise RuntimeError("response lost after draft reject")

        monkeypatch.setattr(service, "reject_draft_change_set_and_cleanup", reject_then_lose)
        rejected = await controller.discard(actor=AGENT, reason="response-loss")
        assert calls == 1
        assert rejected.status is ChangeSetStatus.REJECTED
        assert controller.state.status == "discarded"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_candidate_loop_discard_retries_one_concurrent_draft_cas_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single concurrent draft update is reread before rejecting."""

    service = await ArtifactSessionService.open(tmp_path / "discard-cas-retry.db")
    try:
        created = await _document(service)
        from opensquilla.artifact_session import ArtifactCandidateLoopController

        controller = ArtifactCandidateLoopController(
            service,
            document_id=created.document.document_id,
            base_revision_id=created.revision.revision_id,
            turn_id="turn-discard-cas-retry",
        )
        staged = await controller.stage_candidate(
            candidate_artifact=_blob("discard-cas-before"),
            operations=({"op": "replace", "text": "before"},),
            actor=AGENT,
        )
        original_reject = service.reject_draft_change_set_and_cleanup
        original_update = service.update_draft_change_set_candidate
        injected = False

        async def reject_after_concurrent_update(*args: object, **kwargs: object):
            nonlocal injected
            if not injected:
                injected = True
                await original_update(
                    change_set_id=staged.change_set_id,
                    expected_state_revision=staged.state_revision,
                    candidate_artifact=_blob("discard-cas-after"),
                    operations=({"op": "replace", "text": "after"},),
                    actor=AGENT,
                )
                raise ArtifactConflictError("injected concurrent draft update")
            return await original_reject(*args, **kwargs)

        monkeypatch.setattr(
            service,
            "reject_draft_change_set_and_cleanup",
            reject_after_concurrent_update,
        )
        rejected = await controller.discard(actor=AGENT, reason="cas-retry")
        assert injected is True
        assert rejected.status is ChangeSetStatus.REJECTED
        assert controller.state.status == "discarded"
        assert await service.list_change_sets(created.document.document_id) == (rejected,)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_candidate_loop_discards_empty_draft_after_first_stage_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed first CAS must not strand an empty durable DRAFT."""

    service = await ArtifactSessionService.open(tmp_path / "empty-draft.db")
    try:
        created = await _document(service)
        from opensquilla.artifact_session import ArtifactCandidateLoopController

        controller = ArtifactCandidateLoopController(
            service,
            document_id=created.document.document_id,
            base_revision_id=created.revision.revision_id,
            turn_id="turn-empty-draft",
        )

        async def fail_stage(*args: object, **kwargs: object) -> object:
            del args, kwargs
            raise ArtifactConflictError("injected candidate CAS failure")

        monkeypatch.setattr(service, "update_draft_change_set_candidate", fail_stage)
        with pytest.raises(ArtifactConflictError, match="injected"):
            await controller.stage_candidate(
                candidate_artifact=_blob("never-staged"),
                operations=({"op": "replace", "text": "never"},),
                actor=AGENT,
            )

        draft = await service.get_change_set_by_turn(
            document_id=created.document.document_id,
            turn_id="turn-empty-draft",
        )
        assert draft is not None
        assert draft.status is ChangeSetStatus.DRAFT
        assert draft.candidate_artifact is None
        assert controller.state.status == "open"
        assert controller.state.candidate_sha256 is None

        rejected = await controller.discard(actor=AGENT, reason="stage failed")
        assert rejected.status is ChangeSetStatus.REJECTED
        assert rejected.candidate_artifact is None
        assert await service.list_change_sets(created.document.document_id) == (rejected,)
        assert len(await service.list_revisions(created.document.document_id)) == 1
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_candidate_loop_recovers_create_response_loss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A committed create response may be lost without losing the turn draft."""

    service = await ArtifactSessionService.open(tmp_path / "create-response-loss.db")
    try:
        created = await _document(service)
        from opensquilla.artifact_session import ArtifactCandidateLoopController

        original_create = service.create_change_set

        async def create_then_lose_response(*args: object, **kwargs: object):
            row = await original_create(*args, **kwargs)
            del row
            raise RuntimeError("response lost after create")

        monkeypatch.setattr(service, "create_change_set", create_then_lose_response)
        controller = ArtifactCandidateLoopController(
            service,
            document_id=created.document.document_id,
            base_revision_id=created.revision.revision_id,
            turn_id="turn-create-response-loss",
        )
        staged = await controller.stage_candidate(
            candidate_artifact=_blob("create-recovered"),
            operations=({"op": "replace"},),
            actor=AGENT,
        )
        assert staged.status is ChangeSetStatus.DRAFT
        assert staged.candidate_artifact is not None
        assert len(await service.list_change_sets(created.document.document_id)) == 1
        rejected = await controller.discard(actor=AGENT, reason="response-loss cleanup")
        assert rejected.status is ChangeSetStatus.REJECTED
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_candidate_loop_preserves_candidate_after_stage_response_loss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lost candidate CAS response must not make cleanup delete its blob ref."""

    service = await ArtifactSessionService.open(tmp_path / "stage-response-loss.db")
    try:
        created = await _document(service)
        from opensquilla.artifact_session import ArtifactCandidateLoopController

        original_update = service.update_draft_change_set_candidate
        controller = ArtifactCandidateLoopController(
            service,
            document_id=created.document.document_id,
            base_revision_id=created.revision.revision_id,
            turn_id="turn-stage-response-loss",
        )

        async def update_then_lose_response(*args: object, **kwargs: object):
            row = await original_update(*args, **kwargs)
            del row
            raise RuntimeError("response lost after candidate CAS")

        monkeypatch.setattr(
            service,
            "update_draft_change_set_candidate",
            update_then_lose_response,
        )
        candidate = _blob("stage-recovered")
        with pytest.raises(RuntimeError, match="candidate CAS"):
            await controller.stage_candidate(
                candidate_artifact=candidate,
                operations=({"op": "replace"},),
                actor=AGENT,
            )
        assert controller.candidate_artifact is not None
        assert controller.candidate_artifact.artifact_id == candidate.artifact_id
        rejected = await controller.discard(actor=AGENT, reason="response-loss cleanup")
        assert rejected.status is ChangeSetStatus.REJECTED
        assert rejected.candidate_artifact is None
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_candidate_loop_preserves_reservation_after_reserve_response_loss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ordinary cleanup cannot downgrade a durable response-loss receipt."""

    service = await ArtifactSessionService.open(tmp_path / "reserve-response-loss.db")
    try:
        created = await _document(service)
        from opensquilla.artifact_session import ArtifactCandidateLoopController

        controller = ArtifactCandidateLoopController(
            service,
            document_id=created.document.document_id,
            base_revision_id=created.revision.revision_id,
            turn_id="turn-reserve-response-loss",
        )
        staged = await controller.stage_candidate(
            candidate_artifact=_blob("reserve-recovered"),
            operations=({"op": "replace"},),
            actor=AGENT,
        )
        original_reserve = service.reserve_mutation_attempt_with_status

        async def reserve_then_lose_response(*args: object, **kwargs: object):
            await original_reserve(*args, **kwargs)
            raise RuntimeError("response lost after reservation")

        monkeypatch.setattr(
            service,
            "reserve_mutation_attempt_with_status",
            reserve_then_lose_response,
        )
        await controller.record_verification(
            candidate_sha256=staged.candidate_artifact_sha256 or "",
            verification_token="reserve-response-token",
        )
        with pytest.raises(RuntimeError, match="reservation"):
            await controller.commit(
                actor=AGENT,
                expected_candidate_sha256=staged.candidate_artifact_sha256 or "",
                verification_token="reserve-response-token",
                tool_use_id="finish-reserve-response-loss",
            )
        with pytest.raises(ArtifactConflictError, match="requires reconciliation"):
            await controller.discard(actor=AGENT, reason="response-loss cleanup")
        receipt = await service.reconcile_mutation_attempt(
            document_id=created.document.document_id,
            turn_id="turn-reserve-response-loss",
            tool_use_id="finish-reserve-response-loss",
        )
        assert receipt.status.value == "reserved"
        current = await service.get_change_set(staged.change_set_id)
        assert current.status is ChangeSetStatus.DRAFT
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_candidate_loop_closes_conflicting_reserved_receipt_after_reserve_race(
    tmp_path: Path,
) -> None:
    """A losing finish must not clean the turn's winning durable receipt."""

    service = await ArtifactSessionService.open(tmp_path / "reserve-conflict.db")
    try:
        created = await _document(service)
        from opensquilla.artifact_session import ArtifactCandidateLoopController

        controller = ArtifactCandidateLoopController(
            service,
            document_id=created.document.document_id,
            base_revision_id=created.revision.revision_id,
            turn_id="turn-reserve-conflict",
        )
        staged = await controller.stage_candidate(
            candidate_artifact=_blob("reserve-conflict-candidate"),
            operations=({"op": "replace", "text": "conflict"},),
            actor=AGENT,
        )
        sha256 = staged.candidate_artifact_sha256 or ""
        await service.reserve_mutation_attempt_with_status(
            document_id=created.document.document_id,
            turn_id="turn-reserve-conflict",
            tool_use_id="finish-winner",
            base_revision_id=created.revision.revision_id,
            proposal_sha256=sha256,
        )
        # Simulate the first finish having received the durable reservation
        # before its response was lost.  A duplicate call is rejected by the
        # controller's local identity fence, not by a second reservation RPC.
        controller._mutation_attempt_tool_use_id = "finish-winner"  # noqa: SLF001
        await controller.record_verification(
            candidate_sha256=sha256,
            verification_token="reserve-conflict-token",
        )

        # The turn is already durably reserved by another tool call.  The
        # losing call must not become the cleanup identity when reservation
        # rejects it before returning an attempt row.
        with pytest.raises(ArtifactConflictError, match="durable boundary"):
            await controller.commit(
                actor=AGENT,
                expected_candidate_sha256=sha256,
                verification_token="reserve-conflict-token",
                tool_use_id="finish-loser",
            )

        # The loser must leave both the DRAFT and the winner's RESERVED
        # receipt untouched.  Otherwise its turn-finalization cleanup could
        # cancel a commit that is still in flight.
        with pytest.raises(ArtifactConflictError, match="requires reconciliation"):
            await controller.discard(actor=AGENT, reason="reserve race cleanup")
        receipt = await service.reconcile_mutation_attempt(
            document_id=created.document.document_id,
            turn_id="turn-reserve-conflict",
            tool_use_id="finish-winner",
        )
        assert receipt.status.value == "reserved"
        draft = await service.get_change_set_by_turn(
            document_id=created.document.document_id,
            turn_id="turn-reserve-conflict",
        )
        assert draft is not None
        assert draft.status is ChangeSetStatus.DRAFT

        # The original owner can still finish the candidate after the loser
        # has returned its conflict result.
        await controller.record_verification(
            candidate_sha256=sha256,
            verification_token="reserve-conflict-token-retry",
        )
        applied, committed = await controller.commit(
            actor=AGENT,
            expected_candidate_sha256=sha256,
            verification_token="reserve-conflict-token-retry",
            tool_use_id="finish-winner",
        )
        assert committed.status is ChangeSetStatus.APPLIED
        assert applied.revision.revision_id == committed.applied_revision_id
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_candidate_loop_reconcile_does_not_discard_other_finish_reservation(
    tmp_path: Path,
) -> None:
    """A reconstructed controller cannot claim another finish's receipt."""

    service = await ArtifactSessionService.open(tmp_path / "reconcile-reserved.db")
    try:
        created = await _document(service)
        from opensquilla.artifact_session import ArtifactCandidateLoopController

        first = ArtifactCandidateLoopController(
            service,
            document_id=created.document.document_id,
            base_revision_id=created.revision.revision_id,
            turn_id="turn-reconcile-reserved",
        )
        staged = await first.stage_candidate(
            candidate_artifact=_blob("reconcile-reserved-candidate"),
            operations=({"op": "replace", "text": "reserved"},),
            actor=AGENT,
        )
        sha256 = staged.candidate_artifact_sha256 or ""
        await service.reserve_mutation_attempt_with_status(
            document_id=created.document.document_id,
            turn_id="turn-reconcile-reserved",
            tool_use_id="finish-reconcile",
            base_revision_id=created.revision.revision_id,
            proposal_sha256=sha256,
        )

        restarted = ArtifactCandidateLoopController(
            service,
            document_id=created.document.document_id,
            base_revision_id=created.revision.revision_id,
            turn_id="turn-reconcile-reserved",
        )
        assert await restarted.reconcile() is None
        assert restarted.state.status == "candidate_staged"
        with pytest.raises(ArtifactConflictError, match="requires reconciliation"):
            await restarted.discard(actor=AGENT, reason="reconstructed cleanup")

        receipt = await service.reconcile_mutation_attempt(
            document_id=created.document.document_id,
            turn_id="turn-reconcile-reserved",
            tool_use_id="finish-reconcile",
        )
        assert receipt.status.value == "reserved"
        draft = await service.get_change_set_by_turn(
            document_id=created.document.document_id,
            turn_id="turn-reconcile-reserved",
        )
        assert draft is not None
        assert draft.status is ChangeSetStatus.DRAFT
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_candidate_loop_preserves_receipt_when_lease_acquisition_conflicts(
    tmp_path: Path,
) -> None:
    """A lease failure after reservation remains restart-reconcilable."""

    service = await ArtifactSessionService.open(tmp_path / "lease-conflict.db")
    lease = None
    try:
        created = await _document(service)
        from opensquilla.artifact_session import ArtifactCandidateLoopController

        lease = await service.acquire_writer_lease(
            document_id=created.document.document_id,
            holder_id="other-writer",
            ttl_ms=60_000,
            actor=USER,
        )
        controller = ArtifactCandidateLoopController(
            service,
            document_id=created.document.document_id,
            base_revision_id=created.revision.revision_id,
            turn_id="turn-lease-conflict",
        )
        staged = await controller.stage_candidate(
            candidate_artifact=_blob("lease-conflict-candidate"),
            operations=({"op": "replace", "text": "lease"},),
            actor=AGENT,
        )
        sha256 = staged.candidate_artifact_sha256 or ""
        await controller.record_verification(
            candidate_sha256=sha256,
            verification_token="lease-conflict-token",
        )
        with pytest.raises(ArtifactConflictError, match="writer"):
            await controller.commit(
                actor=AGENT,
                expected_candidate_sha256=sha256,
                verification_token="lease-conflict-token",
                tool_use_id="finish-lease-conflict",
            )

        with pytest.raises(ArtifactConflictError, match="requires reconciliation"):
            await controller.discard(actor=AGENT, reason="lease conflict cleanup")
        receipt = await service.reconcile_mutation_attempt(
            document_id=created.document.document_id,
            turn_id="turn-lease-conflict",
            tool_use_id="finish-lease-conflict",
        )
        assert receipt.status.value == "reserved"
        current = await service.get_change_set(staged.change_set_id)
        assert current.status is ChangeSetStatus.DRAFT
    finally:
        if lease is not None:
            try:
                await service.release_writer_lease(lease=lease, actor=USER)
            except Exception:
                # Teardown is best-effort and must not mask the primary test
                # outcome when the lease was already released by recovery.
                pass
        await service.close()


@pytest.mark.asyncio
async def test_candidate_loop_recovers_candidate_when_stage_task_is_cancelled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation immediately after a committed CAS still leaves cleanup able to reject it."""

    service = await ArtifactSessionService.open(tmp_path / "stage-cancel.db")
    try:
        created = await _document(service)
        from opensquilla.artifact_session import ArtifactCandidateLoopController

        original_update = service.update_draft_change_set_candidate
        controller = ArtifactCandidateLoopController(
            service,
            document_id=created.document.document_id,
            base_revision_id=created.revision.revision_id,
            turn_id="turn-stage-cancel",
        )

        async def update_then_cancel(*args: object, **kwargs: object):
            row = await original_update(*args, **kwargs)
            asyncio.current_task().cancel()  # type: ignore[union-attr]
            await asyncio.sleep(0)
            return row

        monkeypatch.setattr(service, "update_draft_change_set_candidate", update_then_cancel)
        with pytest.raises(asyncio.CancelledError):
            await controller.stage_candidate(
                candidate_artifact=_blob("stage-cancelled"),
                operations=({"op": "replace"},),
                actor=AGENT,
            )
        assert controller.candidate_artifact is not None
        rejected = await controller.discard(actor=AGENT, reason="cancelled")
        assert rejected.status is ChangeSetStatus.REJECTED
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_candidate_discard_preserves_reserved_finish_receipt(tmp_path: Path) -> None:
    """Turn cleanup leaves a durable finish receipt for restart reconciliation."""

    service = await ArtifactSessionService.open(tmp_path / "discard-reserved.db")
    try:
        created = await _document(service)
        from opensquilla.artifact_session import ArtifactCandidateLoopController

        controller = ArtifactCandidateLoopController(
            service,
            document_id=created.document.document_id,
            base_revision_id=created.revision.revision_id,
            turn_id="turn-discard-reserved",
        )
        staged = await controller.stage_candidate(
            candidate_artifact=_blob("reserved-candidate"),
            operations=({"op": "replace", "text": "reserved"},),
            actor=AGENT,
        )
        await service.reserve_mutation_attempt_with_status(
            document_id=created.document.document_id,
            turn_id="turn-discard-reserved",
            tool_use_id="finish-reserved",
            base_revision_id=created.revision.revision_id,
            proposal_sha256=staged.candidate_artifact_sha256 or "",
        )
        await service.register_mutation_candidate(
            document_id=created.document.document_id,
            turn_id="turn-discard-reserved",
            candidate_session_id=created.document.session_id,
            candidate_artifact_id=staged.candidate_artifact_id or "",
            candidate_artifact_sha256=staged.candidate_artifact_sha256 or "",
        )
        # Simulate the controller having crossed the journal boundary before
        # cancellation interrupted the final draft CAS.
        controller._mutation_attempt_tool_use_id = "finish-reserved"  # noqa: SLF001
        with pytest.raises(ArtifactConflictError, match="requires reconciliation"):
            await controller.discard(actor=AGENT, reason="cancelled")

        receipt = await service.reconcile_mutation_attempt(
            document_id=created.document.document_id,
            turn_id="turn-discard-reserved",
            tool_use_id="finish-reserved",
        )
        assert receipt.status.value == "reserved"
        current = await service.get_change_set(staged.change_set_id)
        assert current.status is ChangeSetStatus.DRAFT
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_draft_commit_requires_current_candidate_digest(tmp_path: Path) -> None:
    service = await ArtifactSessionService.open(tmp_path / "digest.db")
    try:
        created = await _document(service)
        draft = await service.create_change_set(
            document_id=created.document.document_id,
            base_revision_id=created.revision.revision_id,
            operations=({"op": "replace"},),
            actor=AGENT,
            turn_id="turn-digest",
        )
        staged = await service.update_draft_change_set_candidate(
            change_set_id=draft.change_set_id,
            expected_state_revision=draft.state_revision,
            candidate_artifact=_blob("candidate"),
            operations=({"op": "replace"},),
            actor=AGENT,
        )
        with pytest.raises(ArtifactConflictError, match="digest"):
            await service.commit_draft_change_set_atomically(
                change_set_id=staged.change_set_id,
                expected_change_set_state_revision=staged.state_revision,
                expected_head_revision_id=created.revision.revision_id,
                expected_document_state_revision=created.document.state_revision,
                expected_candidate_sha256="0" * 64,
                actor=AGENT,
            )
        assert (await service.get_document(created.document.document_id)).generation == 1
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_draft_commit_rejects_noop_candidate(tmp_path: Path) -> None:
    service = await ArtifactSessionService.open(tmp_path / "noop.db")
    try:
        created = await _document(service)
        draft = await service.create_change_set(
            document_id=created.document.document_id,
            base_revision_id=created.revision.revision_id,
            operations=({"op": "replace"},),
            actor=AGENT,
            turn_id="turn-noop",
        )
        staged = await service.update_draft_change_set_candidate(
            change_set_id=draft.change_set_id,
            expected_state_revision=draft.state_revision,
            candidate_artifact=_blob("base"),
            operations=({"op": "replace"},),
            actor=AGENT,
        )
        with pytest.raises(ArtifactValidationError, match="does not change"):
            await service.commit_draft_change_set_atomically(
                change_set_id=staged.change_set_id,
                expected_change_set_state_revision=staged.state_revision,
                expected_head_revision_id=created.revision.revision_id,
                expected_document_state_revision=created.document.state_revision,
                expected_candidate_sha256=staged.candidate_artifact_sha256,
                actor=AGENT,
            )
        assert len(await service.list_revisions(created.document.document_id)) == 1
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_candidate_loop_reconciles_applied_draft_after_controller_restart(
    tmp_path: Path,
) -> None:
    service = await ArtifactSessionService.open(tmp_path / "reconcile.db")
    try:
        created = await _document(service)
        from opensquilla.artifact_session import ArtifactCandidateLoopController

        first = ArtifactCandidateLoopController(
            service,
            document_id=created.document.document_id,
            base_revision_id=created.revision.revision_id,
            turn_id="turn-reconcile",
        )
        staged = await first.stage_candidate(
            candidate_artifact=_blob("reconciled"),
            operations=({"op": "replace"},),
            actor=AGENT,
        )
        await first.record_verification(
            candidate_sha256=staged.candidate_artifact_sha256 or "",
            verification_token="receipt-reconcile",
        )
        applied, _ = await first.commit(
            actor=AGENT,
            expected_candidate_sha256=staged.candidate_artifact_sha256 or "",
            verification_token="receipt-reconcile",
        )

        restarted = ArtifactCandidateLoopController(
            service,
            document_id=created.document.document_id,
            base_revision_id=created.revision.revision_id,
            turn_id="turn-reconcile",
        )
        recovered = await restarted.reconcile()
        assert recovered is not None
        recovered_result, recovered_change = recovered
        assert recovered_result.revision.revision_id == applied.revision.revision_id
        assert recovered_change.status is ChangeSetStatus.APPLIED
        assert restarted.state.status == "committed"
    finally:
        await service.close()
