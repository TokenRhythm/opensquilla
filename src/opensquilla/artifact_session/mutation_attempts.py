"""Narrow runtime controller for idempotent artifact mutation tool calls."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from .errors import ArtifactConflictError, ArtifactNotFoundError, ArtifactValidationError
from .models import ChangeSetStatus, MutationAttempt, MutationAttemptStatus
from .service import ArtifactSessionService


class ArtifactMutationCleanupAmbiguousError(RuntimeError):
    """A journaled candidate could not be proven deleted after a failed write.

    This exception carries no artifact identifiers or filesystem paths.  It is
    an internal dispatch signal: the durable attempt must remain restart-
    recoverable rather than being terminalized as an ordinary writer failure.
    """


@dataclass(frozen=True, slots=True)
class MutationAttemptReservation:
    """Reservation result; only ``created=True`` authorizes first execution."""

    attempt: MutationAttempt
    created: bool


@dataclass(frozen=True, slots=True)
class MutationIntentObservation:
    """Turn-local writer intent; observing it never creates durable state."""

    tool_use_id: str
    attempt_number: int
    created: bool


class ArtifactMutationAttemptController:
    """Fence pure proposals around exactly one durable commit attempt."""

    def __init__(
        self,
        service: ArtifactSessionService,
        *,
        document_id: str,
        base_revision_id: str,
        turn_id: str,
    ) -> None:
        self._service = service
        self._document_id = document_id
        self._base_revision_id = base_revision_id
        self._turn_id = turn_id
        self._intent_lock = asyncio.Lock()
        self._observed_tool_use_ids: list[str] = []
        self._rejected_tool_use_ids: set[str] = set()
        self._active_intent_id: str | None = None
        self._commit_tool_use_id: str | None = None
        self._commit_proposal_sha256: str | None = None
        self._active_tool_use_id: str | None = None
        self._replay_conflict_tool_use_ids: set[str] = set()

    def owns_commit(self, tool_use_id: str) -> bool:
        """Return whether ``tool_use_id`` crossed the durable commit boundary."""

        return (
            self._commit_tool_use_id == tool_use_id
            and tool_use_id not in self._replay_conflict_tool_use_ids
        )

    def _claim_commit(self, tool_use_id: str, proposal_sha256: str) -> None:
        """Close this turn's writer boundary after durable admission is known."""

        self._commit_tool_use_id = tool_use_id
        self._commit_proposal_sha256 = proposal_sha256
        self._active_intent_id = None
        self._active_tool_use_id = tool_use_id
        self._replay_conflict_tool_use_ids.discard(tool_use_id)

    def is_replay_conflict(self, tool_use_id: str) -> bool:
        """Return whether this call mismatched an existing durable proposal."""

        return tool_use_id in self._replay_conflict_tool_use_ids

    async def replay_conflict_attempt(self, tool_use_id: str) -> MutationAttempt:
        """Read the pre-existing receipt without accepting the changed proposal."""

        if not self.is_replay_conflict(tool_use_id):
            raise ArtifactConflictError("mutation call has no replay conflict")
        return await self._service.reconcile_mutation_attempt(
            document_id=self._document_id,
            turn_id=self._turn_id,
            tool_use_id=tool_use_id,
        )

    async def replay_commit(
        self,
        tool_use_id: str,
        proposal_sha256: str,
    ) -> MutationAttempt | None:
        """Return a prior attempt only for the exact same canonical proposal.

        This read-only fence runs before document-head validation. A response-loss
        replay necessarily references the old base revision after a successful
        commit, so checking the current head first would hide an argument mismatch
        behind a stale-context error and could incorrectly return the old receipt.
        """

        try:
            attempt = await self._service.reconcile_mutation_attempt(
                document_id=self._document_id,
                turn_id=self._turn_id,
                tool_use_id=tool_use_id,
            )
        except ArtifactNotFoundError:
            return None
        if (
            attempt.base_revision_id != self._base_revision_id
            or attempt.proposal_sha256 is None
            or attempt.proposal_sha256 != proposal_sha256
        ):
            async with self._intent_lock:
                self._replay_conflict_tool_use_ids.add(tool_use_id)
            raise ArtifactConflictError(
                "mutation replay does not match the committed proposal"
            )
        async with self._intent_lock:
            self._claim_commit(tool_use_id, proposal_sha256)
        return await self.reconcile(tool_use_id)

    @property
    def proposal_rejection_count(self) -> int:
        """Return the turn-local count of rejected, non-durable proposals."""

        return len(self._rejected_tool_use_ids)

    async def observe_intent(self, tool_use_id: str) -> MutationIntentObservation:
        """Observe one streamed writer identity without touching persistence.

        A provider must wait for the first proposal result before emitting a
        corrected proposal.  A distinct writer while another intent is active
        is therefore a parallel-writer protocol violation, not a correction.
        """

        async with self._intent_lock:
            if self._commit_tool_use_id is not None:
                if self._commit_tool_use_id != tool_use_id:
                    raise ArtifactConflictError("this turn already crossed the commit boundary")
                return MutationIntentObservation(
                    tool_use_id=tool_use_id,
                    attempt_number=self._observed_tool_use_ids.index(tool_use_id) + 1,
                    created=False,
                )
            if self._active_intent_id is not None:
                if self._active_intent_id != tool_use_id:
                    raise ArtifactConflictError("parallel document writer intents are not allowed")
                return MutationIntentObservation(
                    tool_use_id=tool_use_id,
                    attempt_number=self._observed_tool_use_ids.index(tool_use_id) + 1,
                    created=False,
                )
            if tool_use_id in self._observed_tool_use_ids:
                return MutationIntentObservation(
                    tool_use_id=tool_use_id,
                    attempt_number=self._observed_tool_use_ids.index(tool_use_id) + 1,
                    created=False,
                )
            self._observed_tool_use_ids.append(tool_use_id)
            self._active_intent_id = tool_use_id
            return MutationIntentObservation(
                tool_use_id=tool_use_id,
                attempt_number=len(self._observed_tool_use_ids),
                created=True,
            )

    async def reject_proposal(self, tool_use_id: str) -> None:
        """Release an invalid pure proposal so one corrected proposal may follow."""

        async with self._intent_lock:
            if self._commit_tool_use_id is not None:
                raise ArtifactConflictError("a committed proposal cannot be rejected")
            if self._active_intent_id != tool_use_id:
                if tool_use_id in self._rejected_tool_use_ids:
                    return
                raise ArtifactConflictError("proposal is not the active writer intent")
            self._active_intent_id = None
            self._rejected_tool_use_ids.add(tool_use_id)

    async def reserve_commit(
        self,
        tool_use_id: str,
        proposal_sha256: str,
    ) -> MutationAttemptReservation:
        """Cross the durable boundary after a proposal has fully validated."""

        async with self._intent_lock:
            if self._commit_tool_use_id is not None:
                _attempt, _created = await self._service.reserve_mutation_attempt_with_status(
                    document_id=self._document_id,
                    turn_id=self._turn_id,
                    tool_use_id=tool_use_id,
                    base_revision_id=self._base_revision_id,
                    proposal_sha256=proposal_sha256,
                )
                attempt = await self.reconcile(tool_use_id)
                return MutationAttemptReservation(attempt=attempt, created=False)
            if self._active_intent_id != tool_use_id:
                raise ArtifactConflictError("proposal was not observed before commit")
            try:
                attempt, created = await self._service.reserve_mutation_attempt_with_status(
                    document_id=self._document_id,
                    turn_id=self._turn_id,
                    tool_use_id=tool_use_id,
                    base_revision_id=self._base_revision_id,
                    proposal_sha256=proposal_sha256,
                )
            except asyncio.CancelledError:
                # Cancellation may arrive after SQLite committed but before the
                # await returned. Close the in-memory boundary so dispatch/turn
                # cleanup reconciles or marks the attempt ambiguous; it must
                # never release this call as another pure proposal.
                self._claim_commit(tool_use_id, proposal_sha256)
                raise
            except Exception as reserve_error:
                # A local persistence response can be lost after COMMIT. Read
                # the exact durable identity before deciding this was a pure
                # proposal. If the receipt is visible, this original caller is
                # still authorized to continue the RESERVED mutation exactly
                # once. If reconciliation itself is unavailable, fail closed at
                # the commit boundary and let dispatch report an unknown result.
                try:
                    recovered = await self._service.reconcile_mutation_attempt(
                        document_id=self._document_id,
                        turn_id=self._turn_id,
                        tool_use_id=tool_use_id,
                    )
                except asyncio.CancelledError:
                    self._claim_commit(tool_use_id, proposal_sha256)
                    raise
                except ArtifactNotFoundError:
                    if not isinstance(
                        reserve_error,
                        (
                            ArtifactConflictError,
                            ArtifactNotFoundError,
                            ArtifactValidationError,
                        ),
                    ):
                        self._claim_commit(tool_use_id, proposal_sha256)
                    raise reserve_error
                except Exception:
                    self._claim_commit(tool_use_id, proposal_sha256)
                    raise reserve_error
                if (
                    recovered.base_revision_id != self._base_revision_id
                    or recovered.proposal_sha256 is None
                    or recovered.proposal_sha256 != proposal_sha256
                ):
                    self._claim_commit(tool_use_id, proposal_sha256)
                    self._replay_conflict_tool_use_ids.add(tool_use_id)
                    raise ArtifactConflictError(
                        "mutation reservation does not match the admitted proposal"
                    ) from reserve_error
                self._claim_commit(tool_use_id, proposal_sha256)
                return MutationAttemptReservation(
                    attempt=recovered,
                    created=recovered.status is MutationAttemptStatus.RESERVED,
                )
            if not created:
                attempt = await self.reconcile(tool_use_id)
            self._claim_commit(tool_use_id, proposal_sha256)
            return MutationAttemptReservation(attempt=attempt, created=created)

    async def reconcile(self, tool_use_id: str) -> MutationAttempt:
        """Recover an applied result whose tool response was lost after commit."""

        attempt = await self._service.reconcile_mutation_attempt(
            document_id=self._document_id,
            turn_id=self._turn_id,
            tool_use_id=tool_use_id,
        )
        if attempt.base_revision_id != self._base_revision_id:
            raise ArtifactConflictError("mutation attempt uses another base revision")
        if (
            self._commit_proposal_sha256 is not None
            and attempt.proposal_sha256 != self._commit_proposal_sha256
        ):
            raise ArtifactConflictError("mutation attempt uses another proposal digest")
        if attempt.status is not MutationAttemptStatus.RESERVED:
            return attempt

        change_set = await self._service.get_change_set_by_turn(
            document_id=self._document_id,
            turn_id=self._turn_id,
        )
        if change_set is None or change_set.status is not ChangeSetStatus.APPLIED:
            return attempt
        if change_set.base_revision_id != self._base_revision_id:
            raise ArtifactConflictError("recovered change set uses another base revision")
        revision_id = change_set.applied_revision_id
        if revision_id is None:
            raise ArtifactConflictError("applied change set has no result revision")
        return await self._service.mark_mutation_attempt_applied(
            document_id=self._document_id,
            turn_id=self._turn_id,
            tool_use_id=tool_use_id,
            change_set_id=change_set.change_set_id,
            revision_id=revision_id,
        )

    async def mark_failed(
        self,
        tool_use_id: str,
        failure_code: str,
        *,
        change_set_id: str | None = None,
    ) -> MutationAttempt:
        return await self._service.mark_mutation_attempt_failed(
            document_id=self._document_id,
            turn_id=self._turn_id,
            tool_use_id=tool_use_id,
            failure_code=failure_code,
            change_set_id=change_set_id,
        )

    async def mark_applied(
        self,
        tool_use_id: str,
        change_set_id: str,
        revision_id: str,
    ) -> MutationAttempt:
        return await self._service.mark_mutation_attempt_applied(
            document_id=self._document_id,
            turn_id=self._turn_id,
            tool_use_id=tool_use_id,
            change_set_id=change_set_id,
            revision_id=revision_id,
        )

    async def mark_ambiguous(
        self,
        tool_use_id: str,
        failure_code: str,
        *,
        change_set_id: str | None = None,
        revision_id: str | None = None,
    ) -> MutationAttempt:
        return await self._service.mark_mutation_attempt_ambiguous(
            document_id=self._document_id,
            turn_id=self._turn_id,
            tool_use_id=tool_use_id,
            failure_code=failure_code,
            change_set_id=change_set_id,
            revision_id=revision_id,
        )

    async def mark_active_ambiguous(self, failure_code: str) -> MutationAttempt:
        """Fence the writer currently authorized by this turn-scoped controller."""

        async with self._intent_lock:
            tool_use_id = self._active_tool_use_id
            if not tool_use_id:
                raise ArtifactConflictError("mutation attempt has no active tool identity")
            return await self.mark_ambiguous(tool_use_id, failure_code)
