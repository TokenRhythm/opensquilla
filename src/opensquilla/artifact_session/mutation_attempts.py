"""Narrow runtime controller for idempotent artifact mutation tool calls."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .errors import ArtifactConflictError, ArtifactNotFoundError, ArtifactValidationError
from .models import (
    Actor,
    ArtifactBlobRef,
    ChangeSet,
    ChangeSetStatus,
    CommitResult,
    MutationAttempt,
    MutationAttemptStatus,
    RevisionSource,
    WriterLease,
)
from .service import ArtifactSessionService


class ArtifactMutationCleanupAmbiguousError(RuntimeError):
    """A journaled candidate could not be proven deleted after a failed write.

    This exception carries no artifact identifiers or filesystem paths.  It is
    an internal dispatch signal: the durable attempt must remain restart-
    recoverable rather than being terminalized as an ordinary writer failure.
    """


_CANDIDATE_WRITER_METADATA_KEY = "_candidate_writer"


def _candidate_proposal_digest(value: str | None) -> str | None:
    """Normalize the turn-local writer digest used for replay fencing."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise ArtifactValidationError("candidate proposal digest must be a string")
    normalized = value.strip().lower()
    try:
        decoded = bytes.fromhex(normalized)
    except ValueError:
        raise ArtifactValidationError("candidate proposal digest must be sha256") from None
    if len(decoded) != hashlib.sha256().digest_size:
        raise ArtifactValidationError("candidate proposal digest must be sha256")
    return normalized


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


@dataclass(frozen=True, slots=True)
class CandidateLoopState:
    """Turn-local snapshot exposed to the model/tool orchestration layer."""

    document_id: str
    base_revision_id: str
    turn_id: str
    change_set_id: str | None
    state_revision: int | None
    candidate_epoch: int
    candidate_sha256: str | None
    verification_token: str | None
    status: str


class ArtifactCandidateLoopController:
    """Manage repeated draft edits before one explicit durable commit.

    This controller intentionally does not know how a candidate is rendered.
    The Electron/browser adapter records an opaque verification receipt via
    :meth:`record_verification`; only that receipt plus the current candidate
    digest can cross the commit boundary.  All persistence writes use the
    ArtifactSession service's state-revision CAS methods.
    """

    def __init__(
        self,
        service: ArtifactSessionService,
        *,
        document_id: str,
        base_revision_id: str,
        turn_id: str,
        expected_document_state_revision: int | None = None,
    ) -> None:
        self._service = service
        self._document_id = document_id
        self._base_revision_id = base_revision_id
        self._turn_id = turn_id
        self._expected_document_state_revision = expected_document_state_revision
        self._change_set: ChangeSet | None = None
        self._operations: tuple[dict[str, Any], ...] = ()
        self._candidate_epoch = 0
        self._candidate_sha256: str | None = None
        # The repository clears candidate columns when rejecting a DRAFT.
        # Retain the last blob reference locally so a cancelled/lost discard
        # can still remove the detached physical artifact.
        self._last_candidate_artifact: ArtifactBlobRef | None = None
        self._verification_token: str | None = None
        self._status = "open"
        self._lock = asyncio.Lock()
        # Writer calls are not durable mutation attempts in a candidate loop,
        # but a provider may replay the same call after losing its response.
        # Keep the latest opaque tool id and canonical proposal digest in the
        # DRAFT validation envelope so a retry can return the existing
        # candidate without publishing another blob or advancing its epoch.
        self._candidate_writer_tool_use_id: str | None = None
        self._candidate_writer_proposal_sha256: str | None = None
        # A finish(commit) journals a mutation attempt before the final draft
        # CAS.  Keep that identity so turn cancellation reports an unresolved
        # durable boundary instead of claiming discard.  Only startup recovery
        # may atomically reject the DRAFT and close RESERVED/AMBIGUOUS.
        self._mutation_attempt_tool_use_id: str | None = None
        self._mutation_attempt_id: str | None = None
        # A turn can receive duplicate ``document_finish`` calls while the
        # first call is still crossing the durable reservation boundary.  If
        # this controller observes an active receipt owned by another tool
        # call, turn-finalization must not reject the shared DRAFT (or restore
        # the winning preview) underneath that call.
        self._finish_request_tool_use_id: str | None = None
        self._discard_blocked_by_other_finish = False
        # The desktop bridge may bind a candidate preview, but it must never
        # receive an artifact id, path, URL, or source bytes.  This opaque,
        # turn-local handle is intentionally not persisted or exposed in the
        # public ChangeSet schema.
        self._preview_handle = f"candidate_{secrets.token_urlsafe(24)}"

    is_candidate_loop = True

    @property
    def state(self) -> CandidateLoopState:
        change_set = self._change_set
        return CandidateLoopState(
            document_id=self._document_id,
            base_revision_id=self._base_revision_id,
            turn_id=self._turn_id,
            change_set_id=None if change_set is None else change_set.change_set_id,
            state_revision=None if change_set is None else change_set.state_revision,
            candidate_epoch=self._candidate_epoch,
            candidate_sha256=self._candidate_sha256,
            verification_token=self._verification_token,
            status=self._status,
        )

    @property
    def candidate_epoch(self) -> int:
        return self._candidate_epoch

    @property
    def candidate_sha256(self) -> str | None:
        return self._candidate_sha256

    @property
    def candidate_artifact(self) -> ArtifactBlobRef | None:
        """Return the currently staged blob reference, if one exists."""

        if self._change_set is not None and self._change_set.candidate_artifact is not None:
            return self._change_set.candidate_artifact
        return self._last_candidate_artifact

    @property
    def verification_token(self) -> str | None:
        return self._verification_token

    @property
    def change_set(self) -> ChangeSet | None:
        """Return the latest in-memory draft snapshot for tool adapters."""

        return self._change_set

    @property
    def preview_handle(self) -> str:
        """Opaque handle accepted by a protocol-v4 desktop bridge."""

        return self._preview_handle

    @property
    def discard_blocked_by_other_finish(self) -> bool:
        """Whether a durable finish receipt currently fences turn cleanup.

        This is intentionally a local safety hint for outer turn cleanup.  A
        durable receipt remains the authority; :meth:`discard` performs its
        own ownership read before rejecting a draft.
        """

        return self._discard_blocked_by_other_finish

    def _restore_candidate_writer_metadata(self, change_set: ChangeSet) -> None:
        """Restore the last writer identity from a durable DRAFT snapshot."""

        validation = change_set.validation
        metadata = validation.get(_CANDIDATE_WRITER_METADATA_KEY) if isinstance(
            validation, dict
        ) else None
        if metadata is None:
            self._candidate_writer_tool_use_id = None
            self._candidate_writer_proposal_sha256 = None
            return
        if not isinstance(metadata, dict):
            raise ArtifactConflictError("candidate writer metadata is invalid")
        tool_use_id = metadata.get("tool_use_id")
        proposal_sha256 = metadata.get("proposal_sha256")
        if (
            not isinstance(tool_use_id, str)
            or not tool_use_id.strip()
            or not isinstance(proposal_sha256, str)
        ):
            raise ArtifactConflictError("candidate writer metadata is incomplete")
        self._candidate_writer_tool_use_id = tool_use_id
        self._candidate_writer_proposal_sha256 = _candidate_proposal_digest(proposal_sha256)

    async def replay_candidate(
        self,
        *,
        tool_use_id: str,
        proposal_sha256: str,
    ) -> ChangeSet | None:
        """Return a prior staged candidate for an exact writer replay.

        Writer calls intentionally do not use ``artifact_mutation_attempts``;
        that table is reserved for the final ``document_finish`` commit.  The
        DRAFT validation envelope is the smallest durable turn-local replay
        receipt: same tool id with a different digest is rejected, while an
        exact digest returns the existing candidate and does not publish bytes
        again.  A matching digest from a replacement tool id is also treated
        as an idempotent retry, since the candidate output is byte-identical.
        """

        if not isinstance(tool_use_id, str) or not tool_use_id.strip():
            raise ArtifactValidationError("candidate writer tool_use_id must be non-empty")
        normalized = _candidate_proposal_digest(proposal_sha256)
        assert normalized is not None
        async with self._lock:
            return await self._replay_candidate_locked(
                tool_use_id=tool_use_id,
                proposal_sha256=normalized,
            )

    async def _replay_candidate_locked(
        self,
        *,
        tool_use_id: str,
        proposal_sha256: str,
    ) -> ChangeSet | None:
        """Lock-free body for :meth:`replay_candidate` and staging CAS."""

        change_set = self._change_set
        if change_set is None:
            change_set = await self._service.get_change_set_by_turn(
                document_id=self._document_id,
                turn_id=self._turn_id,
            )
            if change_set is None:
                return None
            if not await self._service.is_candidate_loop_change_set(
                change_set.change_set_id
            ):
                raise ArtifactConflictError(
                    "candidate change set is owned by another workflow"
                )
            if change_set.document_id != self._document_id:
                raise ArtifactConflictError("candidate change set belongs to another document")
            if change_set.base_revision_id != self._base_revision_id:
                raise ArtifactConflictError("candidate change set uses another base revision")
            self._change_set = change_set
            self._operations = change_set.operations
            if change_set.candidate_artifact is not None:
                self._last_candidate_artifact = change_set.candidate_artifact
            self._restore_candidate_writer_metadata(change_set)
        if change_set.status is not ChangeSetStatus.DRAFT:
            return None
        if change_set.candidate_artifact is None:
            # An empty DRAFT is created before bytes are published.  It is not
            # a replay receipt yet; let the original writer continue.
            return None
        stored_tool_use_id = self._candidate_writer_tool_use_id
        stored_digest = self._candidate_writer_proposal_sha256
        if stored_tool_use_id is None or stored_digest is None:
            return None
        if stored_tool_use_id == tool_use_id and stored_digest != proposal_sha256:
            raise ArtifactConflictError(
                "candidate writer replay does not match the original proposal"
            )
        if stored_digest != proposal_sha256:
            return None
        # Replaying a byte-identical writer is still only valid while the
        # candidate's base remains the current Document head.  Do the same
        # state/head fence as a fresh stage before returning the receipt.
        await self._ensure_document_state_revision()
        self._candidate_sha256 = change_set.candidate_artifact_sha256
        self._candidate_epoch = max(1, change_set.state_revision - 1)
        self._status = "candidate_staged"
        return change_set

    async def _ensure_document_state_revision(self) -> int:
        document = await self._service.get_document(self._document_id)
        if document.head_revision_id != self._base_revision_id:
            raise ArtifactConflictError("candidate base is no longer document head")
        if self._expected_document_state_revision is None:
            self._expected_document_state_revision = document.state_revision
        elif self._expected_document_state_revision != document.state_revision:
            raise ArtifactConflictError("document state_revision changed")
        return self._expected_document_state_revision

    async def ensure_draft(
        self,
        *,
        operations: Sequence[dict[str, Any]],
        actor: Actor,
        summary: str = "",
    ) -> ChangeSet:
        """Create or reload this turn's single draft change set."""

        if self._status not in {
            "open",
            "candidate_staged",
            "verification_passed",
            "verification_failed",
        }:
            raise ArtifactConflictError("candidate loop is already terminal")
        if self._change_set is not None:
            await self._ensure_document_state_revision()
            return self._change_set
        # Fence the base before creating a durable draft.  ``create_change_set``
        # validates that the base revision exists, but intentionally does not
        # require it to still be the document head (ordinary review proposals
        # may be prepared against an older revision).  A candidate loop must
        # not leave an orphan draft when its turn starts stale.
        await self._ensure_document_state_revision()
        existing = await self._service.get_change_set_by_turn(
            document_id=self._document_id,
            turn_id=self._turn_id,
        )
        if existing is None:
            try:
                existing = await self._service.create_change_set(
                    document_id=self._document_id,
                    base_revision_id=self._base_revision_id,
                    operations=operations,
                    actor=actor,
                    turn_id=self._turn_id,
                    summary=summary,
                    candidate_loop=True,
                )
            except asyncio.CancelledError:
                # The insert may have committed before its response was lost,
                # including while the turn is being cancelled.  Reload the
                # exact turn-local row so cleanup can reject it instead of
                # leaking an empty DRAFT.  Preserve the original exception;
                # this is recovery, not permission to swallow cancellation.
                try:
                    recovered = await asyncio.shield(
                        self._service.get_change_set_by_turn(
                            document_id=self._document_id,
                            turn_id=self._turn_id,
                        )
                    )
                except Exception:
                    recovered = None
                if recovered is not None:
                    # The surrounding cleanup reloads this turn-local row;
                    # this check preserves the recovery wait before re-raising.
                    pass
                raise
            except Exception:
                # The insert may have committed before an ordinary response
                # failure. Reload the exact turn-local row so cleanup can
                # reject it instead of leaking an empty DRAFT.
                try:
                    recovered = await asyncio.shield(
                        self._service.get_change_set_by_turn(
                            document_id=self._document_id,
                            turn_id=self._turn_id,
                        )
                    )
                except Exception:
                    recovered = None
                if recovered is None:
                    raise
                existing = recovered
        # A document turn may already have an ordinary agent/review DRAFT
        # with the same (document, turn) key.  Its turn-scoped identity is not
        # sufficient ownership proof: only the immutable creation audit marker
        # authorizes this candidate-loop controller to replace or reject it.
        # Fail closed rather than silently adopting another workflow's draft.
        if existing is not None:
            is_candidate_loop = await self._service.is_candidate_loop_change_set(
                existing.change_set_id
            )
            if not is_candidate_loop:
                raise ArtifactConflictError(
                    "candidate change set is owned by another workflow"
                )
        if existing.document_id != self._document_id:
            raise ArtifactConflictError("candidate change set belongs to another document")
        if existing.base_revision_id != self._base_revision_id:
            raise ArtifactConflictError("candidate change set uses another base revision")
        if existing.status is not ChangeSetStatus.DRAFT:
            raise ArtifactConflictError("candidate change set is already terminal")
        self._change_set = existing
        self._operations = existing.operations
        if existing.candidate_artifact is not None:
            self._last_candidate_artifact = existing.candidate_artifact
        if existing.candidate_artifact_sha256 is not None:
            self._candidate_sha256 = existing.candidate_artifact_sha256
            # Draft state_revision starts at one and advances once per staged
            # candidate, so it is a durable epoch without another schema
            # column.  ``max`` also handles legacy rows that predate this
            # controller and already carry candidate bytes.
            self._candidate_epoch = max(1, existing.state_revision - 1)
            self._status = "candidate_staged"
            self._restore_candidate_writer_metadata(existing)
        return existing

    async def stage_candidate(
        self,
        *,
        candidate_artifact: ArtifactBlobRef,
        operations: Sequence[dict[str, Any]],
        actor: Actor,
        validation: dict[str, Any] | None = None,
        summary: str = "",
        tool_use_id: str | None = None,
        proposal_sha256: str | None = None,
    ) -> ChangeSet:
        """Replace the draft bytes and invalidate prior verification receipts."""

        normalized_proposal = _candidate_proposal_digest(proposal_sha256)
        if (tool_use_id is None) != (normalized_proposal is None):
            raise ArtifactValidationError(
                "candidate writer replay identity requires tool_use_id and proposal_sha256"
            )
        if tool_use_id is not None and (
            not isinstance(tool_use_id, str) or not tool_use_id.strip()
        ):
            raise ArtifactValidationError("candidate writer tool_use_id must be non-empty")
        async with self._lock:
            if tool_use_id is not None and normalized_proposal is not None:
                replay = await self._replay_candidate_locked(
                    tool_use_id=tool_use_id,
                    proposal_sha256=normalized_proposal,
                )
                if replay is not None:
                    return replay
            change_set = await self.ensure_draft(
                operations=operations,
                actor=actor,
                summary=summary,
            )
            incoming_operations = tuple(dict(operation) for operation in operations)
            # The public staging call accepts either the new delta or the
            # caller's already-aggregated operation list.  Normalize both to
            # one complete deterministic list before the repository CAS.
            if not self._operations:
                aggregate_operations = incoming_operations
            elif incoming_operations[: len(self._operations)] == self._operations:
                aggregate_operations = incoming_operations
            else:
                aggregate_operations = (*self._operations, *incoming_operations)
            persisted_validation = None if validation is None else dict(validation)
            if tool_use_id is not None and normalized_proposal is not None:
                persisted_validation = {
                    **(persisted_validation or {}),
                    _CANDIDATE_WRITER_METADATA_KEY: {
                        "tool_use_id": tool_use_id,
                        "proposal_sha256": normalized_proposal,
                    },
                }
            try:
                staged = await self._service.update_draft_change_set_candidate(
                    change_set_id=change_set.change_set_id,
                    expected_state_revision=change_set.state_revision,
                    candidate_artifact=candidate_artifact,
                    operations=aggregate_operations,
                    actor=actor,
                    validation=persisted_validation,
                )
            except BaseException:
                # A CAS can commit before the response reaches this process.
                # Refresh the in-memory snapshot while the controller lock is
                # held so outer cleanup knows whether the just-published blob
                # is still referenced by the durable draft.
                try:
                    recovered = await asyncio.shield(
                        self._service.get_change_set_by_turn(
                            document_id=self._document_id,
                            turn_id=self._turn_id,
                        )
                    )
                except Exception:
                    recovered = None
                if (
                    recovered is not None
                    and recovered.status is ChangeSetStatus.DRAFT
                    and recovered.candidate_artifact is not None
                    and recovered.candidate_artifact.artifact_id
                    == candidate_artifact.artifact_id
                    and recovered.candidate_artifact.sha256 == candidate_artifact.sha256
                ):
                    self._change_set = recovered
                    self._operations = recovered.operations
                    self._last_candidate_artifact = recovered.candidate_artifact
                    self._candidate_epoch = max(1, recovered.state_revision - 1)
                    self._candidate_sha256 = recovered.candidate_artifact_sha256
                    self._verification_token = None
                    self._status = "candidate_staged"
                    self._restore_candidate_writer_metadata(recovered)
                raise
            self._change_set = staged
            self._operations = staged.operations
            self._last_candidate_artifact = staged.candidate_artifact
            self._candidate_epoch += 1
            self._candidate_sha256 = staged.candidate_artifact_sha256
            self._verification_token = None
            self._status = "candidate_staged"
            if tool_use_id is not None and normalized_proposal is not None:
                self._candidate_writer_tool_use_id = tool_use_id
                self._candidate_writer_proposal_sha256 = normalized_proposal
            else:
                self._restore_candidate_writer_metadata(staged)
            return staged

    async def record_verification(
        self,
        *,
        candidate_sha256: str,
        verification_token: str,
    ) -> CandidateLoopState:
        """Attach an opaque browser receipt to the current candidate."""

        async with self._lock:
            if self._status not in {
                "candidate_staged",
                "verification_passed",
                "verification_failed",
            }:
                raise ArtifactConflictError("candidate loop is not awaiting verification")
            if self._change_set is None:
                raise ArtifactConflictError("candidate loop is not open")
            if not isinstance(candidate_sha256, str):
                raise ArtifactValidationError("candidate_sha256 must be a string")
            if self._candidate_sha256 != candidate_sha256.lower():
                raise ArtifactConflictError("verification is for a stale candidate")
            if not verification_token or not isinstance(verification_token, str):
                raise ArtifactValidationError("verification_token must not be empty")
            self._verification_token = verification_token
            self._status = "verification_passed"
            return self.state

    async def invalidate_verification(self, *, reason: str | None = None) -> CandidateLoopState:
        """Invalidate a browser receipt after an action/reload/preview change."""

        async with self._lock:
            if self._status not in {
                "candidate_staged",
                "verification_passed",
                "verification_failed",
            }:
                raise ArtifactConflictError("candidate loop is not open")
            self._verification_token = None
            self._status = "verification_failed" if reason else "candidate_staged"
            return self.state

    async def mark_verification_failed(self, reason: str | None = None) -> CandidateLoopState:
        """Semantic alias for browser adapters reporting failed evidence."""

        return await self.invalidate_verification(reason=reason or "verification_failed")

    async def _recover_mutation_attempt_identity(self) -> MutationAttempt | None:
        """Load the turn's durable receipt and bind its real tool identity.

        ``commit`` records a provisional tool id before awaiting the reservation
        transaction so a response lost after SQLite commit remains recoverable.
        A different finish call can, however, race an already-reserved turn and
        fail before that provisional id is durable.  The lookup is scoped by
        the controller's document/turn binding; an unresolved receipt fences
        all ordinary discard paths and never authorizes another call to commit.
        """

        getter = getattr(self._service, "get_mutation_attempt_for_resolution", None)
        if not callable(getter):
            return None
        try:
            attempt = await getter(
                document_id=self._document_id,
                turn_id=self._turn_id,
            )
        except ArtifactNotFoundError:
            return None
        if not isinstance(attempt, MutationAttempt):
            return None
        if attempt.document_id != self._document_id or attempt.turn_id != self._turn_id:
            return None
        self._mutation_attempt_id = attempt.mutation_attempt_id
        self._mutation_attempt_tool_use_id = attempt.tool_use_id
        if attempt.status in {
            MutationAttemptStatus.RESERVED,
            MutationAttemptStatus.AMBIGUOUS,
        }:
            # Any unresolved durable finish receipt makes ordinary discard
            # unsafe, including one created by this controller.  Only startup
            # recovery may atomically prove DRAFT rejection and close it.
            self._discard_blocked_by_other_finish = True
        return attempt

    async def _ensure_discard_owner(self) -> None:
        """Fence discard against a concurrent durable finish reservation.

        Rejecting a DRAFT is destructive to every finish call sharing this
        turn.  Ordinary cleanup must therefore prove that no
        RESERVED/AMBIGUOUS receipt exists, even one created by this controller.
        If the receipt cannot be read, fail closed and leave recovery to the
        gateway reconciler.
        """

        getter = getattr(self._service, "get_mutation_attempt_for_resolution", None)
        if not callable(getter):
            if self._discard_blocked_by_other_finish:
                raise ArtifactConflictError(
                    "another document finish owns the candidate"
                )
            return
        try:
            attempt = await asyncio.shield(
                getter(document_id=self._document_id, turn_id=self._turn_id)
            )
        except ArtifactNotFoundError:
            # No durable finish receipt exists; an ordinary cancellation may
            # safely reject the draft.
            self._discard_blocked_by_other_finish = False
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A read failure leaves ownership unknown.  Never reject under an
            # unreadable receipt; startup reconciliation can retry safely.
            self._discard_blocked_by_other_finish = True
            raise ArtifactConflictError(
                "cannot verify document finish ownership before discard"
            ) from exc
        if not isinstance(attempt, MutationAttempt):
            self._discard_blocked_by_other_finish = True
            raise ArtifactConflictError(
                "cannot verify document finish ownership before discard"
            )
        if attempt.document_id != self._document_id or attempt.turn_id != self._turn_id:
            self._discard_blocked_by_other_finish = True
            raise ArtifactConflictError("document finish receipt scope is invalid")
        if attempt.status in {
            MutationAttemptStatus.RESERVED,
            MutationAttemptStatus.AMBIGUOUS,
        }:
            self._discard_blocked_by_other_finish = True
            self._mutation_attempt_id = attempt.mutation_attempt_id
            self._mutation_attempt_tool_use_id = attempt.tool_use_id
            raise ArtifactConflictError(
                "document finish outcome requires reconciliation"
            )
        if attempt.status is MutationAttemptStatus.APPLIED:
            # A winner already crossed the durable boundary.  Never turn its
            # revision into a rejected draft; the outer turn can reconcile the
            # terminal receipt on its next pass.
            self._discard_blocked_by_other_finish = True
            raise ArtifactConflictError("document finish has already committed")
        # FAILED receipts no longer own the candidate.  Clear a stale local
        # conflict marker and permit ordinary draft cleanup.
        self._discard_blocked_by_other_finish = False

    async def commit(
        self,
        *,
        actor: Actor,
        expected_candidate_sha256: str,
        verification_token: str,
        tool_use_id: str | None = None,
        lease: WriterLease | None = None,
        require_lease: bool = False,
        source: RevisionSource = RevisionSource.AGENT,
    ) -> tuple[CommitResult, ChangeSet]:
        """Commit exactly the verified candidate and close this controller."""

        async with self._lock:
            if not isinstance(expected_candidate_sha256, str):
                raise ArtifactValidationError("expected_candidate_sha256 must be a string")
            if (
                self._mutation_attempt_tool_use_id is not None
                and self._mutation_attempt_tool_use_id != tool_use_id
            ):
                # A prior finish may have reserved a durable receipt whose
                # response was lost.  Never let a second tool call overwrite
                # that identity and create an untracked RESERVED attempt.
                # Mark this invocation as a loser before raising so outer turn
                # cleanup cannot subsequently treat the winner's receipt as
                # its own and reject the shared DRAFT.
                self._finish_request_tool_use_id = tool_use_id
                self._discard_blocked_by_other_finish = True
                raise ArtifactConflictError(
                    "another document finish is already at the durable boundary"
                )
            if self._candidate_sha256 != expected_candidate_sha256.lower():
                raise ArtifactConflictError(
                    "candidate digest is stale and does not match current draft"
                )
            if self._status != "verification_passed" or self._change_set is None:
                raise ArtifactConflictError("candidate loop is not open")
            if self._verification_token != verification_token:
                raise ArtifactConflictError("verification receipt is missing or stale")
            expected_state = await self._ensure_document_state_revision()
            mutation_attempt_id: str | None = None
            mutation_attempt_tool_use_id: str | None = None
            if tool_use_id is not None:
                if not isinstance(tool_use_id, str) or not tool_use_id.strip():
                    raise ArtifactValidationError("tool_use_id must be a non-empty string")
                candidate_artifact = self._change_set.candidate_artifact
                if candidate_artifact is None:
                    raise ArtifactValidationError("candidate artifact is unavailable")
                document = await self._service.get_document(self._document_id)
                if not isinstance(document.session_id, str) or not document.session_id:
                    raise ArtifactValidationError("candidate artifact session is unavailable")
                candidate_session_id = document.session_id
                self._finish_request_tool_use_id = tool_use_id
                # Record the tool identity before awaiting persistence.  If
                # SQLite commits and the response/cancellation is observed
                # before the await resumes, cleanup can still reconcile the
                # exact RESERVED receipt.
                self._mutation_attempt_tool_use_id = tool_use_id
                try:
                    attempt, _created = (
                        await self._service.reserve_mutation_attempt_with_status(
                            document_id=self._document_id,
                            turn_id=self._turn_id,
                            tool_use_id=tool_use_id,
                            base_revision_id=self._base_revision_id,
                            proposal_sha256=expected_candidate_sha256,
                            candidate_change_set_id=self._change_set.change_set_id,
                            expected_candidate_state_revision=(
                                self._change_set.state_revision
                            ),
                        )
                    )
                except BaseException as reserve_error:
                    # Distinguish a response lost after this reservation from a
                    # conflict with a different tool call.  In both cases load
                    # the turn's authoritative receipt so outer cleanup knows
                    # it must preserve the DRAFT for reconciliation; preserve
                    # the original exception and never let this read authorize
                    # a commit.
                    try:
                        recovered_attempt = await asyncio.shield(
                            self._recover_mutation_attempt_identity()
                        )
                    except BaseException:  # noqa: BLE001 - preserve original failure
                        recovered_attempt = None
                    if recovered_attempt is None and isinstance(
                        reserve_error,
                        (ArtifactConflictError, ArtifactNotFoundError, ArtifactValidationError),
                    ):
                        # These errors are raised before a reservation can be
                        # durable.  Do not let a provisional id turn an
                        # ordinary validation/stale-head error into an
                        # ambiguous finish outcome.  Unknown exceptions remain
                        # fail-closed because the transaction may have committed
                        # before its response was lost.
                        self._mutation_attempt_id = None
                        self._mutation_attempt_tool_use_id = None
                        self._discard_blocked_by_other_finish = False
                    elif recovered_attempt is None:
                        # Unknown reservation outcome: fail closed during
                        # outer cleanup until a later reconciliation proves
                        # that no durable owner exists.
                        self._discard_blocked_by_other_finish = True
                    raise
                # Capture the identity immediately after reservation.  The
                # subsequent candidate registration can itself lose a local
                # response after SQLite commits; the controller must retain
                # the durable boundary across that window.
                self._mutation_attempt_id = attempt.mutation_attempt_id
                self._mutation_attempt_tool_use_id = tool_use_id
                self._discard_blocked_by_other_finish = attempt.status in {
                    MutationAttemptStatus.RESERVED,
                    MutationAttemptStatus.AMBIGUOUS,
                }
                if attempt.status is MutationAttemptStatus.APPLIED:
                    applied_change = await self._service.get_change_set_by_turn(
                        document_id=self._document_id,
                        turn_id=self._turn_id,
                    )
                    revision_id = (
                        None
                        if applied_change is None
                        else applied_change.applied_revision_id
                    )
                    if applied_change is None or revision_id is None:
                        raise ArtifactConflictError(
                            "mutation receipt is applied but its change set is unavailable"
                        )
                    revision = await self._service.get_revision(revision_id)
                    document = await self._service.get_document(self._document_id)
                    self._change_set = applied_change
                    self._candidate_sha256 = revision.artifact_sha256
                    self._verification_token = None
                    self._status = "committed"
                    self._mutation_attempt_id = None
                    self._mutation_attempt_tool_use_id = None
                    self._candidate_writer_tool_use_id = None
                    self._candidate_writer_proposal_sha256 = None
                    self._finish_request_tool_use_id = None
                    self._discard_blocked_by_other_finish = False
                    return (
                        CommitResult(document=document, revision=revision),
                        applied_change,
                    )
                if attempt.status not in {
                    MutationAttemptStatus.RESERVED,
                    MutationAttemptStatus.AMBIGUOUS,
                }:
                    raise ArtifactConflictError("mutation attempt is already terminal")
                if attempt.status is MutationAttemptStatus.AMBIGUOUS:
                    # A response may be lost after the candidate journal is
                    # persisted but before the commit result reaches the
                    # caller.  Ambiguous receipts are intentionally
                    # restart-recoverable; re-registering their candidate is
                    # forbidden by the repository because the row is no
                    # longer ``reserved``.  Reuse it only when the durable
                    # candidate identity is exactly the one being committed.
                    if (
                        attempt.candidate_session_id != candidate_session_id
                        or attempt.candidate_artifact_id != candidate_artifact.artifact_id
                        or attempt.candidate_artifact_sha256 != candidate_artifact.sha256
                    ):
                        raise ArtifactConflictError(
                            "ambiguous mutation receipt has a different candidate"
                        )
                    registered_attempt = attempt
                else:
                    try:
                        registered_attempt = await self._service.register_mutation_candidate(
                            document_id=self._document_id,
                            turn_id=self._turn_id,
                            candidate_session_id=candidate_session_id,
                            candidate_artifact_id=candidate_artifact.artifact_id,
                            candidate_artifact_sha256=candidate_artifact.sha256,
                        )
                    except BaseException:
                        try:
                            await asyncio.shield(self._recover_mutation_attempt_identity())
                        except BaseException:  # noqa: BLE001 - preserve original failure
                            pass
                        raise
                mutation_attempt_id = registered_attempt.mutation_attempt_id
                mutation_attempt_tool_use_id = tool_use_id
                self._mutation_attempt_id = mutation_attempt_id
                self._mutation_attempt_tool_use_id = mutation_attempt_tool_use_id
            owned_lease = lease
            release_owned_lease = False
            if owned_lease is None:
                holder_digest = hashlib.sha256(
                    self._turn_id.encode("utf-8")
                ).hexdigest()[:24]
                owned_lease = await self._service.acquire_writer_lease(
                    document_id=self._document_id,
                    holder_id=f"artifact-agent:{actor.actor_id[:128]}:{holder_digest}",
                    ttl_ms=60_000,
                    actor=actor,
                )
                require_lease = True
                release_owned_lease = True
            try:
                result = await self._service.commit_draft_change_set_atomically(
                    change_set_id=self._change_set.change_set_id,
                    expected_change_set_state_revision=self._change_set.state_revision,
                    expected_head_revision_id=self._base_revision_id,
                    expected_document_state_revision=expected_state,
                    expected_candidate_sha256=expected_candidate_sha256,
                    actor=actor,
                    source=source,
                    lease=owned_lease,
                    require_lease=require_lease,
                    mutation_attempt_id=mutation_attempt_id,
                    mutation_attempt_tool_use_id=mutation_attempt_tool_use_id,
                )
            finally:
                if release_owned_lease and owned_lease is not None:
                    try:
                        await self._service.release_writer_lease(
                            lease=owned_lease,
                            actor=actor,
                        )
                    except Exception:
                        # Lease release is best-effort and must not mask the
                        # durable commit result.
                        pass
            self._change_set = result[1]
            self._status = "committed"
            self._verification_token = None
            self._mutation_attempt_id = None
            self._mutation_attempt_tool_use_id = None
            self._candidate_writer_tool_use_id = None
            self._candidate_writer_proposal_sha256 = None
            self._finish_request_tool_use_id = None
            self._discard_blocked_by_other_finish = False
            return result

    async def reconcile(self) -> tuple[CommitResult, ChangeSet] | None:
        """Recover a final commit after a lost tool response or process restart."""

        async with self._lock:
            change_set = await self._service.get_change_set_by_turn(
                document_id=self._document_id,
                turn_id=self._turn_id,
            )
            if change_set is None:
                return None
            if not await self._service.is_candidate_loop_change_set(
                change_set.change_set_id
            ):
                raise ArtifactConflictError(
                    "candidate change set is owned by another workflow"
                )
            if change_set.base_revision_id != self._base_revision_id:
                raise ArtifactConflictError("candidate change set uses another base revision")
            self._change_set = change_set
            self._operations = change_set.operations
            if change_set.status is ChangeSetStatus.APPLIED:
                revision_id = change_set.applied_revision_id
                if revision_id is None:
                    raise ArtifactConflictError("applied candidate has no result revision")
                revision = await self._service.get_revision(revision_id)
                document = await self._service.get_document(self._document_id)
                result = CommitResult(document=document, revision=revision)
                self._candidate_sha256 = revision.artifact_sha256
                self._status = "committed"
                self._verification_token = None
                self._candidate_writer_tool_use_id = None
                self._candidate_writer_proposal_sha256 = None
                self._finish_request_tool_use_id = None
                self._discard_blocked_by_other_finish = False
                return result, change_set
            if change_set.status is ChangeSetStatus.REJECTED:
                self._candidate_sha256 = None
                self._verification_token = None
                self._status = "discarded"
                self._candidate_writer_tool_use_id = None
                self._candidate_writer_proposal_sha256 = None
                self._finish_request_tool_use_id = None
                self._discard_blocked_by_other_finish = False
            elif change_set.status is ChangeSetStatus.DRAFT:
                self._candidate_sha256 = change_set.candidate_artifact_sha256
                # ``state_revision`` starts at one and is incremented for each
                # candidate replacement.  Restore the durable epoch when a
                # process/turn controller is recreated so old grants and
                # browser anchors cannot accidentally be reused after a
                # restart.
                self._candidate_epoch = (
                    max(1, change_set.state_revision - 1)
                    if self._candidate_sha256 is not None
                    else 0
                )
                self._status = (
                    "candidate_staged"
                    if self._candidate_sha256 is not None
                    else "open"
                )
                self._restore_candidate_writer_metadata(change_set)
                # A controller can be reconstructed while a prior finish's
                # reservation is still unresolved (for example, before a
                # Gateway restart has run draft cleanup).  Restore that exact
                # identity so a later discard/finish cannot orphan the row or
                # misclassify a different tool call as the durable attempt.
                try:
                    await self._recover_mutation_attempt_identity()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Recovery metadata restoration is best-effort; a later
                    # reconcile can retry without changing the commit result.
                    pass
            return None

    async def _reject_draft_with_recovery(
        self,
        *,
        change_set_id: str,
        expected_state_revision: int,
        actor: Actor,
        reason: str | None,
    ) -> ChangeSet:
        """Reject a draft with one bounded read/retry recovery pass.

        The reject transaction is deliberately a CAS.  A response can be lost
        after that CAS commits, or another in-process cleanup task can advance
        the draft state before this controller sends its reject.  In either
        case, blindly issuing the same write again is unsafe: the old state
        revision must not be reused.  Reload the exact turn row once, adopt a
        still-DRAFT snapshot, and retry with its new revision.  A terminal
        REJECTED row is treated as idempotent success; APPLIED is surfaced as
        a conflict so callers cannot claim that discard won.

        The single retry is intentional.  It handles response loss and one
        concurrent CAS update while preserving the global loop/deadline fuse
        instead of creating an unbounded cleanup loop.
        """

        expected = expected_state_revision
        for retry in range(2):
            try:
                return await self._service.reject_draft_change_set_and_cleanup(
                    change_set_id=change_set_id,
                    expected_state_revision=expected,
                    actor=actor,
                    reason=reason,
                    require_no_active_mutation_attempt=True,
                )
            except asyncio.CancelledError:
                raise
            except Exception as reject_error:
                # A validation/not-found error cannot be repaired by rereading
                # the row.  Preserve it directly; only persistence/ CAS errors
                # participate in the bounded recovery path.
                if isinstance(reject_error, ArtifactValidationError):
                    raise
                try:
                    latest = await asyncio.shield(
                        self._service.get_change_set_by_turn(
                            document_id=self._document_id,
                            turn_id=self._turn_id,
                        )
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # The original write outcome is unknown.  The outer turn
                    # cleanup/restart reconciler remains the next safe fence.
                    raise reject_error
                if latest is None or latest.change_set_id != change_set_id:
                    raise reject_error
                if latest.status is ChangeSetStatus.REJECTED:
                    # The reject committed before its response was lost (or a
                    # sibling cleanup task won the CAS).  Return the durable
                    # terminal row so receipt/preview cleanup can continue.
                    return latest
                if latest.status is not ChangeSetStatus.DRAFT:
                    # APPLIED/READY/other terminal states must never be
                    # rewritten as REJECTED by recovery.
                    raise reject_error
                if retry == 1:
                    raise reject_error
                # Adopt the fresh CAS snapshot for the one retry.  Keep the
                # last local blob reference when the concurrent writer has
                # already detached its candidate; orphan cleanup is journaled
                # separately and this preserves direct cleanup safety.
                self._change_set = latest
                self._operations = latest.operations
                if latest.candidate_artifact is not None:
                    self._last_candidate_artifact = latest.candidate_artifact
                self._candidate_sha256 = latest.candidate_artifact_sha256
                self._candidate_epoch = (
                    max(1, latest.state_revision - 1)
                    if self._candidate_sha256 is not None
                    else 0
                )
                expected = latest.state_revision
        # The loop always returns or raises; keep a defensive error for type
        # checkers if a future edit changes the retry bound.
        raise ArtifactConflictError("draft reject recovery exhausted")

    async def discard(self, *, actor: Actor, reason: str | None = None) -> ChangeSet:
        """Reject and detach the current draft without changing the head."""

        async with self._lock:
            # ``ensure_draft`` persists the DRAFT before the first candidate
            # bytes are CAS-updated.  A failed first writer can therefore
            # leave an empty DRAFT while this controller is still ``open``.
            # That state is intentionally discardable so cancellation/timeout
            # cleanup cannot strand a durable ChangeSet with no candidate.
            open_empty_draft = (
                self._status == "open"
                and self._change_set is not None
                and self._change_set.status is ChangeSetStatus.DRAFT
            )
            if (
                self._status
                not in {
                    "candidate_staged",
                    "verification_passed",
                    "verification_failed",
                }
                and not open_empty_draft
            ) or self._change_set is None:
                raise ArtifactConflictError("candidate loop is not open")
            await self._ensure_discard_owner()
            change_set_id = self._change_set.change_set_id
            rejected = await self._reject_draft_with_recovery(
                change_set_id=change_set_id,
                expected_state_revision=self._change_set.state_revision,
                actor=actor,
                reason=reason,
            )
            self._change_set = rejected
            self._status = "discarded"
            self._candidate_sha256 = None
            self._verification_token = None
            self._mutation_attempt_id = None
            self._mutation_attempt_tool_use_id = None
            self._candidate_writer_tool_use_id = None
            self._candidate_writer_proposal_sha256 = None
            self._finish_request_tool_use_id = None
            self._discard_blocked_by_other_finish = False
            return rejected

    async def discard_without_candidate(self) -> None:
        """Close an empty loop as a discard without creating durable state.

        A model is allowed to decide that no edit is needed (or that it cannot
        safely start one) before the first writer.  Treating
        ``document_finish(discard)`` as an idempotent no-op in that state keeps
        the loop protocol total without manufacturing an empty ChangeSet.
        Once a draft or candidate exists, callers must use :meth:`discard` so
        the durable reject/CAS and blob cleanup path remains authoritative.
        """

        async with self._lock:
            if self._status == "discarded":
                return
            if (
                self._status != "open"
                or self._change_set is not None
                or self.candidate_artifact is not None
            ):
                raise ArtifactConflictError("candidate loop already has durable state")
            self._status = "discarded"
            self._candidate_sha256 = None
            self._verification_token = None
            self._mutation_attempt_id = None
            self._mutation_attempt_tool_use_id = None
            self._candidate_writer_tool_use_id = None
            self._candidate_writer_proposal_sha256 = None
            self._finish_request_tool_use_id = None
            self._discard_blocked_by_other_finish = False

    async def reject(self, *, actor: Actor, reason: str | None = None) -> ChangeSet:
        """Compatibility alias used by loop-control tool adapters."""

        return await self.discard(actor=actor, reason=reason)

    async def finish(
        self,
        *,
        decision: str,
        actor: Actor,
        expected_candidate_sha256: str | None = None,
        verification_token: str | None = None,
        tool_use_id: str | None = None,
        lease: WriterLease | None = None,
        require_lease: bool = False,
        source: RevisionSource = RevisionSource.AGENT,
        reason: str | None = None,
    ) -> tuple[CommitResult, ChangeSet] | ChangeSet:
        """Apply the model's explicit ``commit`` or ``discard`` decision."""

        if decision == "discard":
            return await self.discard(actor=actor, reason=reason)
        if decision != "commit":
            raise ArtifactValidationError("decision must be commit or discard")
        if expected_candidate_sha256 is None or verification_token is None:
            raise ArtifactValidationError(
                "commit requires expected_candidate_sha256 and verification_token"
            )
        return await self.commit(
            actor=actor,
            expected_candidate_sha256=expected_candidate_sha256,
            verification_token=verification_token,
            tool_use_id=tool_use_id,
            lease=lease,
            require_lease=require_lease,
            source=source,
        )


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
